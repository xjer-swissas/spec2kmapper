class AmosCentralError(Exception):
    """Base error. Messages never include credentials or message bodies."""


class ProtocolError(AmosCentralError, ValueError):
    pass


class EncryptionError(AmosCentralError):
    pass


class RestError(AmosCentralError):
    def __init__(self, status, operation):
        self.status = status
        super().__init__(f"AMOScentral {operation} failed (HTTP {status})")


class UnauthorizedError(RestError):
    pass


class ForbiddenError(RestError):
    pass
