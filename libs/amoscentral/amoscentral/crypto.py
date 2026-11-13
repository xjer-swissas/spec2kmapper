"""Java-compatible raw P-256 ECDH and ChaCha20-Poly1305, with no HKDF/AAD."""
import base64
import gzip
import io
import os
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .errors import EncryptionError, ProtocolError
from .message import Message


def public_key(data):
    """Read legacy X.509 certificates or V3 SubjectPublicKeyInfo (PEM/DER)."""
    for loader in (serialization.load_der_public_key, serialization.load_pem_public_key,
                   x509.load_der_x509_certificate, x509.load_pem_x509_certificate):
        try:
            key = loader(data)
            if isinstance(key, x509.Certificate):
                key = key.public_key()
            if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
                raise EncryptionError("Only P-256 public keys are supported")
            return key
        except ValueError:
            continue
    raise EncryptionError("Invalid public key or certificate")


def public_der(key):
    if isinstance(key, ec.EllipticCurvePrivateKey):
        key = key.public_key()
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def load_private(path, password=None):
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=password)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise EncryptionError("Only P-256 private keys are supported")
    return key


def create_private(path):
    """Explicit creation only: never overwrite an identity during startup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return key


class MessageCodec:
    def __init__(self, private_keys, peer_keys, max_body_bytes=25 * 1024 * 1024):
        self.private_keys = list(private_keys)
        self.peer_keys = peer_keys  # callable(address) -> current and historical public keys
        self.max_body_bytes = max_body_bytes

    def encode(self, message, *, encrypt=True, compress=False, channel_key=None):
        headers, body = dict(message.headers), message.body
        if len(body) > self.max_body_bytes:
            raise ProtocolError("Message body exceeds the configured limit")
        if headers.get("amc_content_encoding"):
            raise ProtocolError("Message is already encoded")
        encodings = []
        if compress:
            body = gzip.compress(body, mtime=0)
            encodings.insert(0, "gzip")
        if encrypt:
            key = channel_key
            if key is None:
                peers = self.peer_keys(message.recipient)
                if not peers or not self.private_keys:
                    raise EncryptionError("Missing encryption key")
                key = self.private_keys[0].exchange(ec.ECDH(), peers[0])
            nonce = os.urandom(12)
            body = ChaCha20Poly1305(key).encrypt(nonce, body, None)
            headers["amc_nonce"] = base64.b64encode(nonce).decode("ascii")
            encodings.insert(0, "ChaCha20_7539")
        if encodings:
            headers["amc_content_encoding"] = ", ".join(encodings)
        return Message(headers, body)

    def decode(self, message, *, channel_keys=()):
        headers, body = dict(message.headers), message.body
        encodings = [s.strip() for s in headers.get("amc_content_encoding", "").split(",") if s.strip()]
        if encodings not in ([], ["gzip"], ["ChaCha20_7539"], ["ChaCha20_7539", "gzip"]):
            raise ProtocolError("Unsupported content encoding or encoding order")
        if "ChaCha20_7539" in encodings:
            try:
                nonce = base64.b64decode(headers.get("amc_nonce", ""), validate=True)
            except ValueError:
                raise EncryptionError("Invalid nonce") from None
            if len(nonce) != 12:
                raise EncryptionError("Invalid nonce length")
            keys = list(channel_keys)
            if not keys:
                keys = [private.exchange(ec.ECDH(), peer)
                        for peer in self.peer_keys(message.sender) for private in self.private_keys]
            for key in keys:
                try:
                    body = ChaCha20Poly1305(key).decrypt(nonce, body, None)
                    break
                except InvalidTag:
                    continue
            else:
                raise EncryptionError("Message authentication failed with all available keys")
        if "gzip" in encodings:
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                    body = stream.read(self.max_body_bytes + 1)
            except (OSError, EOFError):
                raise ProtocolError("Invalid gzip content") from None
        if len(body) > self.max_body_bytes:
            raise ProtocolError("Decoded body exceeds the configured limit")
        headers.pop("amc_content_encoding", None)
        return Message(headers, body)
