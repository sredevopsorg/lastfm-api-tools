"""Application configuration.

Everything environment-specific comes from env vars (12-factor). Nothing is
baked into the image, and secrets are never logged.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LASTFM_API_ROOT = "http://ws.audioscrobbler.com/2.0/"

# The Last.fm API Terms of Service "Reasonable Usage Cap": a maximum of 100 MB
# of Last.fm Data stored in total. 104857600 = 100 MiB.
LASTFM_TOS_CAP_BYTES = 100 * 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- HTTP server -----------------------------------------------------
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    log_level: str = "INFO"
    log_json: bool = True

    # ---- Jellyfin --------------------------------------------------------
    jellyfin_url: str = Field(default="http://localhost:8096", alias="JELLYFIN_URL")
    jellyfin_api_key: SecretStr = Field(default=SecretStr(""), alias="JELLYFIN_API_KEY")
    jellyfin_timeout_s: float = 10.0
    jellyfin_max_retries: int = 2

    # ---- Last.fm ---------------------------------------------------------
    lastfm_api_key: SecretStr = Field(default=SecretStr(""), alias="LASTFM_API_KEY")
    lastfm_api_root: str = LASTFM_API_ROOT
    lastfm_user_agent: str = "metaedit/0.1.0 (+https://github.com/metaedit)"
    lastfm_timeout_s: float = 10.0
    lastfm_max_rps: float = 4.0
    lastfm_burst: int = 8
    lastfm_max_retries: int = 3

    # ---- Database --------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://metaedit:metaedit@localhost:5432/metaedit",
        alias="DATABASE_URL",
    )
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False

    # ---- Archive ---------------------------------------------------------
    archive_enabled: bool = True
    archive_log_requests: bool = True
    archive_soft_cap_bytes: int = LASTFM_TOS_CAP_BYTES
    archive_warn_ratio: float = 0.8
    archive_refuse_ratio: float = 1.0
    # How long a stored Last.fm response may be served without a refresh.
    archive_freshness_ttl_s: int = 24 * 60 * 60
    # artist.getSimilar changes slowly; 7 days is plenty.
    archive_similar_freshness_ttl_s: int = 7 * 24 * 60 * 60
    archive_partition_months_ahead: int = 3

    # ---- Mapping policy --------------------------------------------------
    lastfm_min_tag_count: int = 0
    lastfm_genre_limit: int = 5
    lastfm_style_limit: int = 10
    max_tags_per_item: int = 30
    max_tag_length: int = 100
    # Comma-separated; parsed by the tag policy as the blacklist.
    tag_blacklist_extra: str = ""

    # ---- Frontend --------------------------------------------------------
    web_dist_dir: str = ""

    @field_validator("archive_warn_ratio", "archive_refuse_ratio")
    @classmethod
    def _ratio_in_range(cls, v: float) -> float:
        if not 0 < v <= 10:
            msg = "ratio must be greater than 0 and no more than 10"
            raise ValueError(msg)
        return v

    @field_validator("archive_soft_cap_bytes")
    @classmethod
    def _cap_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "archive_soft_cap_bytes must be positive"
            raise ValueError(msg)
        return v

    @property
    def jellyfin_base_url(self) -> str:
        return self.jellyfin_url.rstrip("/")

    @property
    def lastfm_base_url(self) -> str:
        return self.lastfm_api_root.rstrip("/") + "/"

    @property
    def archive_warn_bytes(self) -> int:
        return int(self.archive_soft_cap_bytes * self.archive_warn_ratio)

    @property
    def archive_refuse_bytes(self) -> int:
        return int(self.archive_soft_cap_bytes * self.archive_refuse_ratio)

    @property
    def extra_tag_blacklist(self) -> frozenset[str]:
        if not self.tag_blacklist_extra.strip():
            return frozenset()
        parts = (p.strip().lower() for p in self.tag_blacklist_extra.split(","))
        return frozenset(p for p in parts if p)

    def jellyfin_key(self) -> str:
        return self.jellyfin_api_key.get_secret_value()

    def lastfm_key(self) -> str:
        return self.lastfm_api_key.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide settings. Tests call ``get_settings.cache_clear()``."""
    return Settings()
