GENERIC = "Could not validate credentials"
BAD_CREDENTIALS = "Incorrect email or password"
LOCKOUT = "Too many failed attempts. Try again later."
FORBIDDEN = "Insufficient permissions"
DUPLICATE_EMAIL = "Email already registered"


class AuthError(Exception):
    def __init__(self, status: int, detail: str, *, reason: str | None = None):
        self.status = status
        self.detail = detail
        self.reason = reason or detail
        super().__init__(detail)
