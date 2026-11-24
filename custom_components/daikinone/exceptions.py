class DaikinServiceException(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class DaikinAuthError(DaikinServiceException):
    """Daikin rejected the account credentials or access."""
