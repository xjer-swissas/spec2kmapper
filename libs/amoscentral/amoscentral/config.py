from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from .errors import ProtocolError
from .message import validate_address


@dataclass(repr=False)
class ClientConfig:
    address: str
    password: str = field(repr=False)
    client_id: str
    client_secret: str = field(default="", repr=False)
    base_url: str = "https://central.amos-cloud.com"
    api_version: str = "v3"
    state_dir: str = "/var/lib/amoscentral"
    private_key_file: str = "private-key.pem"
    private_key_password: str = field(default="", repr=False)
    certificate_file: str = ""
    historical_private_key_files: list[str] = field(default_factory=list)
    read_timeout: float = 35
    connect_timeout: float = 5
    max_body_bytes: int = 25 * 1024 * 1024
    system: str = "spec2kmapper"
    system_version: str = "2.0.0"
    system_productive: bool = False

    def __post_init__(self):
        validate_address(self.address)
        url = urlsplit(self.base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ProtocolError("AMOScentral base_url must be an HTTPS URL without credentials")
        if self.api_version not in ("v3", "legacy"):
            raise ProtocolError("api_version must be v3 or legacy")
        if not self.password or not self.client_id:
            raise ProtocolError("AMOScentral password and OAuth client_id are required")
        if not Path(self.state_dir).is_absolute():
            raise ProtocolError("AMOScentral state_dir must be an absolute external directory")
        if self.read_timeout <= 30 or self.connect_timeout <= 0 or not 1 <= self.max_body_bytes <= 100 * 1024 * 1024:
            raise ProtocolError("Invalid AMOScentral timeout or message size limit")

    def state_path(self, value):
        path = Path(value)
        return path if path.is_absolute() else Path(self.state_dir) / path

    @classmethod
    def from_file(cls, path):
        with Path(path).open("rb") as stream:
            data = tomllib.load(stream)
        return cls(**data["amoscentral"])
