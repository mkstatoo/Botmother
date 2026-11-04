"""Symmetric encryption for secrets stored in the database (bot tokens, gateway keys)."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str | bytes) -> None:
        self._key = key.encode() if isinstance(key, str) else key
        self._fernet = Fernet(self._key)

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:  # wrong key or corrupted data
            raise ValueError("cannot decrypt secret: wrong FERNET_KEY?") from exc

    def derive(self, purpose: str, length: int = 40) -> str:
        """Stable URL-safe secret derived from the key (used for the mother webhook path)."""
        digest = hmac.new(self._key, purpose.encode(), hashlib.sha256).hexdigest()
        return digest[:length]


def load_or_create_key(fernet_key: str | None, data_dir: Path) -> str:
    """Use FERNET_KEY if given, else a key file in data_dir (generated once)."""
    if fernet_key:
        return fernet_key
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "fernet.key"
    if path.exists():
        return path.read_text().strip()
    key = Fernet.generate_key().decode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key
