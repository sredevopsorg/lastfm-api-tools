"""Application configuration.

Everything environment-specific comes from env vars (12-factor). Nothing is
baked into the image, and secrets are never logged.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # Optional. An API key carries no user identity, and Jellyfin's single-item endpoint
    # requires a user context, so one is discovered from /Users when this is empty.
    # Set it to pin the choice on a server where the "first" user is not the right one.
    jellyfin_user_id: str = Field(default="", alias="JELLYFIN_USER_ID")

    # ---- Last.fm ---------------------------------------------------------
    lastfm_api_key: SecretStr = Field(default=SecretStr(""), alias="LASTFM_API_KEY")
    lastfm_api_root: str = LASTFM_API_ROOT
    lastfm_user_agent: str = "metaedit/0.0.5 (+https://github.com/sredevopsorg/metaedit)"
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
    # Both ratios are fractions of archive_soft_cap_bytes, and the warning must
    # not fire after the refusal -- see _ratios_are_ordered below.
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
        if not 0 < v <= 1:
            msg = "ratio must be greater than 0 and at most 1 (a fraction of the cap)"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _ratios_are_ordered(self) -> Settings:
        """The warning must be able to fire before the refusal.

        These two thresholds guard the Last.fm storage cap, and the warning is
        what gives an operator time to act. If warn sits at or beyond refuse, the
        warning is unreachable dead code: the only signal is a hard failure on the
        next write. That misconfiguration is silently accepted by a per-field
        range check, so it is rejected here instead.
        """
        if self.archive_warn_ratio > self.archive_refuse_ratio:
            msg = (
                "archive_warn_ratio must not exceed archive_refuse_ratio, "
                "otherwise the warning never fires before data is refused "
                f"(warn={self.archive_warn_ratio}, refuse={self.archive_refuse_ratio})"
            )
            raise ValueError(msg)
        return self

    @field_validator("archive_soft_cap_bytes")
    @classmethod
    def _cap_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "archive_soft_cap_bytes must be positive"
            raise ValueError(msg)
        return v

    @property
    def jellyfin_base_url(self) -> str:
        """The server root, with a scheme.

        A bare ``host:port`` is a natural way to write a LAN address -- Jellyfin's own
        docs use it -- but httpx then fails with "Request URL is missing an 'http://' or
        'https://' protocol", which names neither the setting nor the fix. Normalising to
        http (Jellyfin's default) turns a confusing transport error into a working
        connection, and the alternative of refusing to start is unfriendly for a value
        that has exactly one sensible reading.
        """
        url = self.jellyfin_url.strip().rstrip("/")
        if url and "://" not in url:
            url = f"http://{url}"
        return url

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
