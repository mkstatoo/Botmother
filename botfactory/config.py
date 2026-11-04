"""Application settings (environment / .env)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- mother bot -------------------------------------------------------
    bot_token: str
    admin_ids: Annotated[list[int], NoDecode] = []

    # --- storage ----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///data/botfactory.db"
    data_dir: Path = Path("data")
    fernet_key: str | None = None
    redis_url: str | None = None

    # --- runtime ----------------------------------------------------------
    mode: Literal["polling", "webhook"] = "polling"
    base_url: str = ""  # public https URL of this server (needed for payments / webhook mode)
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    proxy_url: str | None = None  # e.g. socks5://127.0.0.1:1080 or http://host:port
    log_level: str = "INFO"

    # --- limits -----------------------------------------------------------
    max_bots_per_user: int = 3
    broadcast_rate: int = 20  # messages per second

    # --- payment gateways -------------------------------------------------
    zarinpal_base: str = "https://payment.zarinpal.com"
    nowpayments_base: str = "https://api.nowpayments.io/v1"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if v is None or v == "":
            return []
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @field_validator("base_url", "zarinpal_base", "nowpayments_base", mode="after")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @field_validator("proxy_url", "redis_url", "fernet_key", mode="after")
    @classmethod
    def _empty_none(cls, v: str | None) -> str | None:
        return v or None

    @property
    def payments_enabled(self) -> bool:
        """Gateway callbacks need a public URL."""
        return bool(self.base_url)

    @property
    def web_enabled(self) -> bool:
        return self.mode == "webhook" or bool(self.base_url)
