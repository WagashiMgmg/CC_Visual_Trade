from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Hyperliquid
    hyperliquid_private_key: str = ""
    hyperliquid_account_address: str = ""   # API wallet address (signing key)
    hyperliquid_main_address: str = ""      # Main account address (holds funds / positions)
    testnet: bool = False

    # Trading
    trading_coin: str = "BTC"
    position_size_usd: float = 100.0
    leverage: int = 3

    # Dashboard
    dashboard_port: int = 8080

    # Safety
    dry_run: bool = True

    # Discord
    discord_bot_token: str = ""
    discord_channel_id: str = ""

    # Timing (configurable via .env)
    cycle_interval_minutes: int = 60        # How often Claude analyzes charts
    position_min_hours: int = 2             # Minimum hold time before EXIT is allowed
    position_max_hours: int = 4             # Force-close positions after this many hours

    # Emergency thresholds (configurable via .env)
    emergency_loss_pct: float = 3.0         # Unrealized loss % of size_usd to trigger emergency
    emergency_profit_pct: float = 5.0       # Unrealized profit % of size_usd to trigger emergency
    emergency_price_move_pct: float = 2.0   # Price move % in N minutes to trigger emergency
    emergency_price_move_minutes: int = 5   # Time window for price move detection
    emergency_cooldown_minutes: int = 15    # Minimum interval between emergency cycles

    # Hold reflection (missed opportunity analysis)
    hold_reflection_enabled: bool = True
    hold_reflection_window_hours: int = 4
    hold_reflection_min_pnl_multiplier: float = 3.0  # min hypothetical PnL as multiple of round-trip fees
    hold_reflection_max_daily: int = 2

    # Internals (not from .env)
    candle_count: int = 100
    limit_order_timeout_secs: int = 30
    close_limit_timeout_secs: int = 60

    @property
    def position_max_duration_secs(self) -> int:
        return self.position_max_hours * 3600

    @property
    def api_url(self) -> str:
        from hyperliquid.utils import constants
        return constants.TESTNET_API_URL if self.testnet else constants.MAINNET_API_URL


settings = Settings()
