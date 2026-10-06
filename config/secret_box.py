"""
Encryption for API keys and tokens stored in the database (app_settings rows of kind="secret").

A secret is stored as {"enc": "<Fernet token>"}; the key that unlocks them is SECRETS_MASTER_KEY in the server's .env,
which never goes into the database. Someone who sees the database, a backup or a table dump sees only ciphertext.

Without SECRETS_MASTER_KEY (a dev laptop, the test suite) secrets are stored as before, in plain text, and the
/keys page says so.
"""
from __future__ import annotations

import base64
import hashlib
import os

ENC = "enc"


def _fernet():
    raw = os.environ.get("SECRETS_MASTER_KEY", "").strip()
    if not raw:
        return None
    from cryptography.fernet import Fernet
    try:
        return Fernet(raw.encode())
    except ValueError:
        # any passphrase works too: stretch it to a valid 32-byte Fernet key
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def available() -> bool:
    try:
        return _fernet() is not None
    except ImportError:
        return False


def seal(value: str):
    """What to store for a secret: {"enc": ...} when a master key exists, else the plain string."""
    f = _fernet() if available() else None
    return {ENC: f.encrypt(value.encode()).decode()} if f else value


def is_sealed(stored) -> bool:
    return isinstance(stored, dict) and ENC in stored


def open_(stored) -> str | None:
    """The plain value of a stored secret, or None if it cannot be opened (wrong or missing master key)."""
    if not is_sealed(stored):
        return stored if isinstance(stored, str) else None
    f = _fernet() if available() else None
    if f is None:
        return None
    try:
        return f.decrypt(stored[ENC].encode()).decode()
    except Exception:  # noqa: BLE001 - InvalidToken: key rotated or wrong
        return None


def new_master_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()
