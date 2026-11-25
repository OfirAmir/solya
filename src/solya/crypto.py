from cryptography.fernet import Fernet


class TokenCipher:
    """Fernet wrapper for encrypting OAuth tokens at rest."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str:
        return self._fernet.decrypt(blob).decode("utf-8")

    def __repr__(self) -> str:
        return "TokenCipher(<redacted>)"

    __str__ = __repr__
