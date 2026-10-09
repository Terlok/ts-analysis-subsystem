"""Configuration (environment variables with prefix TSA_, or backend/.env)."""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tsa_core.levels import Grid
from tsa_core.pipeline import ChannelParams

SEC = 1_000_000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TSA_", env_file=".env", extra="ignore")

    # --- QuestDB (archive) ---
    questdb_host: str = "localhost"
    questdb_pg_port: int = 8812  # PostgreSQL wire protocol, used for queries
    questdb_http_port: int = 9000  # HTTP: ILP ingestion and /exec
    questdb_user: str = "admin"
    questdb_password: str = "quest"
    questdb_database: str = "qdb"
    questdb_https: bool = False
    # HTTP basic auth for ILP/HTTP (empty for open-source QuestDB with default config)
    questdb_http_user: str = ""
    questdb_http_password: str = ""
    questdb_ilp_auto_flush_rows: int = 10_000
    questdb_pool_size: int = 8

    # --- PostgreSQL (registry, events, alarms) ---
    postgres_dsn: str = "postgresql+psycopg://tsa:tsa@localhost:5432/tsa"

    # --- Redis (hot window, message bus, tile cache) ---
    redis_url: str = "redis://localhost:6379/0"
    stream_key: str = "telem:in"
    stream_maxlen: int = 200_000  # approximate trimming of the ingest stream
    hot_window_s: int = 600  # T_hot
    hot_batch_max_span_s: int = 10  # max time span of a batch stored in the hot window
    tile_ttl_s: int = 7 * 24 * 3600  # TTL of closed tiles (bounds Redis memory)

    # --- Time grid of aggregates: Delta_l = delta0 * base^l ---
    agg_delta0_ms: int = 1000
    agg_base: int = 4
    agg_levels: int = 8
    tile_buckets: int = 1024  # K
    late_ms: int = 30_000  # Delta_late: max allowed lateness of data
    agg_flush_interval_ms: int = 1000

    # --- Data selection ---
    detail_c: float = 2.0  # c in m = min(N, ceil(c * W_px))
    preselect_rho: float = 2.0  # rho in MinMaxLTTB
    max_width_px: int = 8000
    max_raw_points: int = 2_000_000  # limit of /raw and export

    # --- Live streaming over WebSocket ---
    live_rate_hz: float = 10.0
    live_max_window_s: int = 3600

    # --- Analytics defaults (overridable per channel in the registry) ---
    hampel_window: int = 15
    hampel_kappa: float = 3.0
    feature_window: int = 30
    residual_k: float = 3.0
    episode_gap_ms: int = 5000
    models_dir: str = "models"
    analytics_batch: int = 200  # stream entries per read

    # --- API ---
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    api_title: str = "Time Series Analysis Subsystem"

    @property
    def grid(self) -> Grid:
        return Grid(self.agg_delta0_ms * 1000, self.agg_base, self.agg_levels, self.tile_buckets)

    @property
    def late_us(self) -> int:
        return self.late_ms * 1000

    @property
    def questdb_pg_conninfo(self) -> str:
        return (
            f"host={self.questdb_host} port={self.questdb_pg_port} user={self.questdb_user} "
            f"password={self.questdb_password} dbname={self.questdb_database}"
        )

    @property
    def questdb_ilp_conf(self) -> str:
        proto = "https" if self.questdb_https else "http"
        conf = f"{proto}::addr={self.questdb_host}:{self.questdb_http_port};auto_flush_rows={self.questdb_ilp_auto_flush_rows};"
        if self.questdb_http_user:
            conf += f"username={self.questdb_http_user};password={self.questdb_http_password};"
        return conf

    def default_channel_params(self) -> ChannelParams:
        return ChannelParams(
            hampel_window=self.hampel_window,
            hampel_kappa=self.hampel_kappa,
            feature_window=self.feature_window,
            residual_k=self.residual_k,
            episode_gap_us=self.episode_gap_ms * 1000,
        )


def bypass_proxy_for(hosts: list[str]) -> None:
    """Exclude our own services from HTTP(S)_PROXY.

    HTTP clients (the QuestDB ILP client, httpx, websockets) honour proxy variables from
    the environment; behind a corporate proxy even requests to localhost would be sent
    to the proxy and fail. Hosts are appended to NO_PROXY / no_proxy.
    """
    for var in ("NO_PROXY", "no_proxy"):
        current = [h.strip() for h in os.environ.get(var, "").split(",") if h.strip()]
        merged = current + [h for h in hosts if h and h not in current]
        os.environ[var] = ",".join(merged)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    redis_host = urlparse(s.redis_url).hostname or ""
    bypass_proxy_for(["localhost", "127.0.0.1", "::1", s.questdb_host, redis_host])
    return s
