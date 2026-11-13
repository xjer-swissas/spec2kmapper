import logging
import threading

from .errors import AmosCentralError

logger = logging.getLogger(__name__)


class Poller:
    """Long poll, run a durable handler, then ACK. Failures leave delivery pending."""
    def __init__(self, client, handler, *, error_handler=None, success_handler=None):
        self.client = client
        self.handler = handler
        self.error_handler = error_handler
        self.success_handler = success_handler
        self.stop_event = threading.Event()
        self.thread = None

    def run(self):
        failures = 0
        while not self.stop_event.is_set():
            try:
                # Handlers can route/filter by sender before fetching peer keys/decrypting.
                message = self.client.receive_message(decode=False)
                if message is not None:
                    self.handler(message)
                    self.client.acknowledge(message.message_id, stop_event=self.stop_event)
                else:
                    self.stop_event.wait(0.1)
                failures = 0
                if self.success_handler:
                    self.success_handler()
            except Exception as exc:
                failures += 1
                # Error text/payload is intentionally excluded from logs.
                logger.warning("AMOScentral receive failed (%s); delivery remains unacknowledged", type(exc).__name__)
                if self.error_handler:
                    self.error_handler(type(exc).__name__)
                self.stop_event.wait(min(60, 2 ** min(failures, 6)))

    def start(self):
        if self.thread and self.thread.is_alive():
            raise AmosCentralError("Poller is already running")
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="amoscentral-receiver", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(self.client.config.read_timeout + self.client.config.connect_timeout + 5)
            if self.thread.is_alive():
                raise AmosCentralError("Receiver did not stop within the request timeout")
