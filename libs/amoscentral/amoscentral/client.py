import base64
import json
import os
from pathlib import Path
import platform
import time
from uuid import uuid4

from .config import ClientConfig
from .crypto import MessageCodec, load_private, public_der, public_key
from .errors import AmosCentralError, EncryptionError, ProtocolError, RestError
from .message import Message, MessageBuilder, validate_address
from .transport import Transport


class AmosCentralClient:
    def __init__(self, config: ClientConfig, *, transport=None):
        self.config = config
        self.transport = transport or Transport(config)
        password = config.private_key_password.encode() if config.private_key_password else None
        self.private_keys = [load_private(config.state_path(path), password) for path in
                             [config.private_key_file, *config.historical_private_key_files]]
        self.codec = MessageCodec(self.private_keys, self.peer_keys, config.max_body_bytes)

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.transport.close()

    def json_request(self, method, path, **kwargs):
        raw = self.transport.request(method, path, **kwargs)
        return json.loads(raw) if raw else None

    def get_address(self, address):
        validate_address(address)
        version = "v3" if self.config.api_version == "v3" else "v2"
        return self.json_request("GET", f"api/address/addresses/{version}/{address}")

    def peer_keys(self, address):
        validate_address(address)
        directory = Path(self.config.state_dir) / "peers" / address
        if self.config.api_version == "v3":
            records = self.json_request("GET", f"api/address/addresses/v3/{address}/key") or []
            # ACTIVE first for sending, GRACE also available for queued messages.
            records = sorted((r for r in records if r["status"] in ("ACTIVE", "GRACE")),
                             key=lambda r: r["status"] != "ACTIVE")
            keys = [public_key(base64.b64decode(r["publicKey"], validate=True)) for r in records]
        else:
            raw = self.transport.request("GET", f"api/addresses/{address}/cert")
            key = public_key(raw)
            directory.mkdir(parents=True, exist_ok=True)
            # Certificate serial numbers are not filesystem paths supplied by peers.
            import hashlib
            target = directory / (hashlib.sha256(public_der(key)).hexdigest() + ".der")
            if not target.exists():
                target.write_bytes(raw)
            keys = [key] + [public_key(p.read_bytes()) for p in sorted(directory.glob("*.der")) if p != target]
        if not keys:
            raise EncryptionError("The peer has no usable encryption key")
        if len(keys) > 32:
            raise EncryptionError("Too many peer keys; prune obsolete key history")
        return keys

    def connect(self):
        """Bind V3 runtime identity without silently rotating a registered key."""
        if self.config.api_version == "legacy":
            raw = self.transport.request("GET", f"api/addresses/{self.config.address}/cert")
            if public_der(public_key(raw)) != public_der(self.private_keys[0]):
                raise EncryptionError("Registered certificate does not match the local private key")
            return
        address = self.config.address
        record = self.json_request("GET", f"api/address/addresses/v3/{address}/key/active", missing_ok=True)
        own = public_der(self.private_keys[0])
        if record:
            if public_der(public_key(base64.b64decode(record["publicKey"], validate=True))) != own:
                raise EncryptionError("Registered key differs from the local identity; restore the correct external key")
        else:
            self.json_request("POST", f"api/address/addresses/v3/{address}/key",
                              json={"publicKey": base64.b64encode(own).decode("ascii")})
        directory = Path(self.config.state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        identity_file = directory / "system-id"
        if not identity_file.exists():
            fd = os.open(identity_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(str(uuid4()))
        self.json_request("PUT", f"api/address/addresses/v3/{address}/connect", json={
            "system": self.config.system, "systemVersion": self.config.system_version,
            "systemOS": platform.system(), "systemID": identity_file.read_text().strip(),
            "systemProductive": self.config.system_productive,
        })

    def get_address_certificate(self, address):
        validate_address(address)
        return self.transport.request("GET", f"api/addresses/{address}/cert")

    def update_certificate(self, address, certificate):
        validate_address(address)
        self.transport.request("PUT", f"api/addresses/{address}/cert", data=certificate,
                               headers={"Content-Type": "application/x-x509-ca-cert"})

    def send_message(self, message, *, encrypt=True, compress=False, channel_key=None):
        if isinstance(message, MessageBuilder):
            encrypt, compress = message.encrypted, message.compressed
            message = message.build()
        if message.sender != self.config.address:
            raise ProtocolError("Message sender differs from the configured address")
        encoded = self.codec.encode(message, encrypt=encrypt, compress=compress, channel_key=channel_key)
        self.transport.request("POST", f"api/messages/{validate_address(message.recipient)}",
                               data=encoded.to_bytes(), headers={"Content-Type": "application/octet-stream"})

    def receive_message(self, correlation_id=None, *, decode=True, channel_keys=()):
        raw = self.transport.request("GET", f"api/messages/{self.config.address}",
                                     params={"correlationId": correlation_id} if correlation_id else None)
        if raw is None:
            return None
        message = Message.from_bytes(raw)
        if message.recipient != self.config.address:
            raise ProtocolError("Message recipient differs from the configured address")
        return self.codec.decode(message, channel_keys=channel_keys) if decode else message

    def acknowledge(self, message_id, *, stop_event=None):
        if not message_id:
            raise ProtocolError("Cannot acknowledge a message without an ID")
        for attempt in range(3):
            try:
                self.transport.request("POST", f"api/messages/ack/{self.config.address}", params={"messageId": message_id})
                return
            except AmosCentralError as exc:
                if (isinstance(exc, RestError) and exc.status in (401, 403)) or attempt == 2:
                    raise
                if stop_event is not None:
                    if stop_event.wait(3):
                        raise
                else:
                    time.sleep(3)

    def call(self, operation_id, *, body=None, **parameters):
        """Call a management operation from the supplied V3 OpenAPI contract."""
        from .operations import OPERATIONS
        try:
            method, path, path_names, query_names = OPERATIONS[operation_id]
        except KeyError:
            raise ProtocolError("Unknown management operation") from None
        from urllib.parse import quote
        for name in path_names:
            if name not in parameters:
                raise ProtocolError(f"Missing path parameter {name}")
            path = path.replace("{" + name + "}", quote(str(parameters.pop(name)), safe=""))
        if set(parameters) - set(query_names):
            raise ProtocolError("Unexpected operation parameter")
        payload = {"data": body} if isinstance(body, bytes) else {"json": body}
        raw = self.transport.request(method, path.lstrip("/"), params=parameters, **payload)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return raw
