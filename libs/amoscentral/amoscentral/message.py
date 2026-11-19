"""Wire envelope derived from amc-message-builder 1.3.1 (UTF-8 + EOT + bytes)."""
from dataclasses import dataclass, field
import re
from uuid import uuid4

from .errors import ProtocolError

MAX_HEADER_BYTES = 64000
ADDRESS = re.compile(r"[a-z](?:[a-z0-9_-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)+", re.IGNORECASE)
HEADER = re.compile(r"amc_[a-z0-9][a-z0-9_-]*[a-z0-9]")


def validate_address(value):
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise ProtocolError("Invalid AMOScentral address")
    return value


@dataclass
class Message:
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = field(default=b"", repr=False)

    @property
    def message_id(self):
        return self.headers.get("amc_message_id", "")

    @property
    def sender(self):
        return self.headers.get("amc_sender", "")

    @property
    def recipient(self):
        # Legacy spelling is still documented by AMOScentral.
        return self.headers.get("amc_recipient", self.headers.get("amc_receipient", ""))

    @property
    def correlation_id(self):
        return self.headers.get("amc_correlation_id", "")

    def validate(self):
        for key, value in self.headers.items():
            if not isinstance(key, str) or not HEADER.fullmatch(key):
                raise ProtocolError("Invalid AMOScentral header name")
            if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ProtocolError("Invalid AMOScentral header value")
        for key in ("amc_message_id", "amc_service", "amc_service_version"):
            if not self.headers.get(key):
                raise ProtocolError(f"Missing {key}")
        validate_address(self.sender)
        validate_address(self.recipient)
        validate_address(self.headers["amc_service"])
        version = self.headers["amc_service_version"]
        pattern = r"\d+\.\d+(?:\.\d+)?" if self.headers["amc_service"].lower().startswith("com.swiss-as.amos.aim.") else r"\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?"
        if not re.fullmatch(pattern, version):
            raise ProtocolError("Invalid service version")

    def to_bytes(self):
        self.validate()
        headers = "".join(f"{k}: {v}\n" for k, v in self.headers.items()).encode("utf-8")
        if len(headers) > MAX_HEADER_BYTES:
            raise ProtocolError("Header limit exceeded")
        return headers + b"\x04" + self.body

    @classmethod
    def from_bytes(cls, wire):
        split = wire.find(b"\x04", 0, MAX_HEADER_BYTES + 1)
        if split < 0:
            raise ProtocolError("Missing EOT or header limit exceeded")
        try:
            text = wire[:split].decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("Headers are not UTF-8") from None
        headers = {}
        for line in text.splitlines():
            if not line:
                continue
            key, separator, value = line.partition(":")
            key = key.strip()
            if not separator or key.lower() in {k.lower() for k in headers}:
                raise ProtocolError("Malformed or duplicate header")
            headers[key] = value.strip()
        message = cls(headers, wire[split + 1:])
        message.validate()
        return message


class MessageBuilder:
    """Fluent equivalent of the Java builder. Encoding flags stay off the wire."""
    def __init__(self):
        self.headers = {"amc_service_version": "1.0.0"}
        self.body = b""
        self.encrypted = True
        self.compressed = False

    def add_property(self, key, value):
        if value is None:
            self.headers.pop(key, None)
        else:
            self.headers[key] = str(value).strip()
        return self

    def set_recipient(self, value): return self.add_property("amc_recipient", value)
    def set_sender(self, value): return self.add_property("amc_sender", value)
    def for_service(self, value): return self.add_property("amc_service", value)
    def for_aim_service(self, value): return self.for_service("com.swiss-as.amos.aim." + value.strip().lower())
    def in_version(self, value): return self.add_property("amc_service_version", value)
    def as_content_type(self, value): return self.add_property("amc_content_type", value)
    def set_reply_to(self, value): return self.add_property("amc_reply_to", value)
    def set_user_token(self, value): return self.add_property("amc_user_token", value)
    def set_message_id(self, value): return self.add_property("amc_message_id", value)
    def with_correlation_id(self, value=None): return self.add_property("amc_correlation_id", value or str(uuid4()))
    def will_accept(self, *values): return self.add_property("amc_accept", ";".join(values))

    def set_content(self, value):
        if hasattr(value, "read"):
            value = value.read()
        self.body = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        return self

    def encrypt(self):
        self.encrypted = True
        return self

    def never_encrypt(self):
        self.encrypted = False
        return self

    def compress(self):
        self.compressed = True
        return self

    def build(self):
        self.headers.setdefault("amc_message_id", str(uuid4()))
        return Message(dict(self.headers), self.body)
