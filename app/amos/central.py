"""Single application inbox, backed by the reusable AMOScentral client."""
import logging
import threading

from amoscentral import AmosCentralClient, Poller
from sqlalchemy import text

from app.amos.central_config import load_config
from app.amos.central_inbox import BrokerInbox
from app.config import settings
from app.database import SessionLocal, engine

logger = logging.getLogger(__name__)
LOCK_ID = 250052104


class CentralReceiver:
    def __init__(self):
        self.routing = None
        self.client = None
        self.poller = None
        self.thread = None
        self.stop_event = threading.Event()
        self.state = 'DISABLED'
        self.last_error = None

    def failed(self, detail):
        self.state = 'RETRYING'
        self.last_error = detail

    def healthy(self):
        self.state = 'LISTENING'
        self.last_error = None

    def start(self):
        if not settings.AMOSCENTRAL_CONFIG_FILE:
            return
        if self.thread and self.thread.is_alive():
            return
        try:
            config, self.routing = load_config(settings.AMOSCENTRAL_CONFIG_FILE)
        except Exception as exc:
            self.state = 'CONFIGURATION_ERROR'
            self.last_error = f'Configuration could not be loaded ({type(exc).__name__})'
            logger.error('AMOScentral external configuration could not be loaded')
            return
        self.stop_event.clear()
        self.state = 'CONNECTING'
        self.thread = threading.Thread(target=self.run, args=(config,), name='amoscentral-supervisor', daemon=True)
        self.thread.start()

    def run(self, config):
        while not self.stop_event.is_set():
            try:
                # Hold a dedicated PostgreSQL connection for the advisory-lock lifetime.
                with engine.connect() as connection:
                    acquired = connection.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': LOCK_ID}).scalar_one()
                    connection.commit()
                    if not acquired:
                        self.state = 'STANDBY'
                        self.stop_event.wait(5)
                        continue
                    try:
                        with AmosCentralClient(config) as self.client:
                            self.client.connect()
                            inbox = BrokerInbox(SessionLocal, self.routing, self.client)
                            def handle(message):
                                accepted = inbox(message)
                                if accepted is not False:
                                    self.state = 'LISTENING'
                                return accepted
                            self.poller = Poller(self.client, handle,
                                                 error_handler=self.failed,
                                                 success_handler=self.healthy,
                                                 event_handler=inbox.event)
                            self.state = 'LISTENING'
                            self.poller.start()
                            try:
                                while not self.stop_event.wait(5):
                                    if not self.poller.thread.is_alive():
                                        raise RuntimeError('Receiver thread stopped')
                                    # Detect a lost advisory-lock connection before accepting more data.
                                    connection.execute(text('SELECT 1'))
                                    connection.commit()
                            finally:
                                self.poller.stop()
                    finally:
                        if self.poller:
                            self.poller.stop()
                        if not connection.invalidated:
                            connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK_ID})
                            connection.commit()
            except Exception as exc:
                self.state = 'RETRYING'
                self.last_error = f'Connection unavailable ({type(exc).__name__})'
                logger.warning('AMOScentral connection unavailable (%s)', type(exc).__name__)
                self.stop_event.wait(30)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(50)
        self.state = 'STOPPED'

    def tenant_status(self, address=''):
        return {'state': self.state, 'sender_address': address, 'last_error': self.last_error}


receiver = CentralReceiver()
