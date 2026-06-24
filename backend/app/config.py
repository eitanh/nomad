"""Central config — pydantic settings loaded from env / .env (ported from `stocks`).

All money-safety limits live here as defaults; they can be overridden per-env and
(later) live-tuned via the `app_config` table, mirroring the `stocks` policy pattern.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---
    database_url: str = "postgresql://nomad:nomad@postgres:5432/nomad"

    # --- IBKR gateway (data + execution share this one connection) ---
    ibgw_host: str = "127.0.0.1"      # "ibgw" in-cluster; localhost for local docker-compose
    ibgw_port: int = 4002             # 4002 = paper API, 4001 = live (Phase 4 only)
    ib_client_id: int = 1             # fixed; engine is a strict singleton
    trading_mode: str = "paper"       # "paper" | "live" — live is gated behind arm-live (Phase 4)
    ib_account: str = ""              # optional explicit account id; "" = first/default

    # --- Optional fundamentals/backtest source (NOT the live feed) ---
    fmp_api_key: str = ""
    fmp_base_url: str = "https://financialmodelingprep.com/stable"

    # --- Watchlist (small fixed set for the paper MVP) ---
    watchlist: str = "AAPL,MSFT,SPY"  # comma-separated

    # --- Risk limits (money-safety; enforced by the single risk gate) ---
    max_position_usd: float = 5_000.0        # max $ per single position
    max_gross_usd: float = 20_000.0          # max $ gross exposure across all positions
    max_positions: int = 5                   # max concurrent open positions
    stop_loss_pct: float = 0.02              # per-trade hard stop (2%)
    max_daily_drawdown_pct: float = 0.03     # halt all trading if daily PnL < -3%

    # --- API ---
    cors_origins: str = "*"

    @property
    def watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]


settings = Settings()
