import threading
import time
from urllib.parse import urlsplit

import requests

from .errors import AmosCentralError, ForbiddenError, ProtocolError, RestError, UnauthorizedError


class Transport:
    """OAuth password grant and bounded HTTPS transport. No message-body logging."""
    TOKEN_PATH = "api/auth/realms/amc_realm/protocol/openid-connect/token"

    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()
        self._token = None
        self._expires = 0
        self._lock = threading.Lock()

    def close(self):
        self.session.close()

    def _read(self, response, limit):
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise ProtocolError("AMOScentral response exceeds the configured limit")
        return bytes(data)

    @staticmethod
    def _check(status, operation):
        error = UnauthorizedError if status == 401 else ForbiddenError if status == 403 else RestError
        if not 200 <= status < 300:
            raise error(status, operation)

    def _authorize(self):
        with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            try:
                with self.session.post(self.config.base_url.rstrip("/") + "/" + self.TOKEN_PATH,
                                       data={"grant_type": "password", "username": self.config.address,
                                             "password": self.config.password, "client_id": self.config.client_id,
                                             "client_secret": self.config.client_secret},
                                       timeout=(self.config.connect_timeout, self.config.read_timeout),
                                       allow_redirects=False) as response:
                    self._check(response.status_code, "authentication")
                    data = response.json()
                    token = data["access_token"]
                    lifetime = int(data["expires_in"])
                    if not isinstance(token, str) or not token or lifetime <= 0:
                        raise ValueError()
                    self._token = token
                    self._expires = time.monotonic() + max(0, lifetime - min(30, lifetime / 10))
                    return token
            except (KeyError, ValueError, requests.RequestException):
                raise AmosCentralError("AMOScentral authentication failed; check the external configuration") from None

    def request(self, method, path, *, params=None, json=None, data=None, headers=None, missing_ok=False):
        if urlsplit(path).scheme or not path.startswith("api/") or ".." in path:
            raise ProtocolError("REST path must be relative to the AMOScentral API")
        for attempt in range(2):
            token = self._authorize()
            try:
                with self.session.request(method, self.config.base_url.rstrip("/") + "/" + path,
                                          params=params, json=json, data=data,
                                          headers={**(headers or {}), "Authorization": f"Bearer {token}",
                                                   "User-Agent": "amoscentral-python/0.1.0"},
                                          timeout=(self.config.connect_timeout, self.config.read_timeout),
                                          allow_redirects=False, stream=True) as response:
                    if response.status_code == 401 and attempt == 0:
                        with self._lock:
                            self._token = None
                        continue
                    if response.status_code == 204 or (missing_ok and response.status_code == 404):
                        return None
                    self._check(response.status_code, method)
                    return self._read(response, self.config.max_body_bytes + 64001)
            except requests.RequestException:
                # Do not replay a send after an ambiguous timeout.
                raise AmosCentralError("AMOScentral transport failed") from None
