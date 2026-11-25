from cryptography.fernet import Fernet

from solya.crypto import TokenCipher


def test_crypto_roundtrip():
    cipher = TokenCipher(Fernet.generate_key().decode())
    blob = cipher.encrypt("refresh-token-123")
    assert isinstance(blob, bytes)
    assert b"refresh-token-123" not in blob
    assert cipher.decrypt(blob) == "refresh-token-123"


def test_cipher_repr_redacted():
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    assert repr(cipher) == "TokenCipher(<redacted>)"
    assert key not in repr(cipher)
    assert key not in str(cipher)
