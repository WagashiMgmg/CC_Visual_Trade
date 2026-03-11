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

    # Testnet-specific credentials (used when TESTNET=true)
    test_hyperliquid_private_key: str = ""
    test_hyperliquid_account_address: str = ""
    test_hyperliquid_main_address: str = ""

    # Trading
    trading_coin: str = "BTC"
    position_size_usd: float = 100.0  # fallback when API/ATR unavailable
    leverage: int = 3

    # Dynamic position sizing (ATR-based)
    max_risk_pct: float = 2.0        # Max risk per trade (% of equity)
    atr_multiplier: float = 2.0      # Assumed adverse move (multiple of ATR)
    min_position_usd: float = 20.0   # Position size floor
    max_position_usd: float = 500.0  # Position size ceiling

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

    # Fee
    fee_rate_fallback: float = 0.00045  # API取得失敗時のフォールバック (0.045% taker)

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

    @property
    def active_private_key(self) -> str:
        """Return testnet key when TESTNET=true and TEST_ key is set, else production key."""
        if self.testnet and self.test_hyperliquid_private_key:
            return self.test_hyperliquid_private_key
        return self.hyperliquid_private_key

    @property
    def active_main_address(self) -> str:
        """Return testnet main address when TESTNET=true and TEST_ address is set, else production."""
        if self.testnet and self.test_hyperliquid_main_address:
            return self.test_hyperliquid_main_address
        return self.hyperliquid_main_address


settings = Settings()


_EMPTY_SPOT_META = {"tokens": [], "universe": []}


def _safe_spot_meta():
    """Fetch spot_meta, falling back to empty on testnet SDK bug."""
    from hyperliquid.info import Info
    try:
        info = Info(settings.api_url, skip_ws=True)
        return None  # SDK fetched it fine
    except (IndexError, KeyError):
        return _EMPTY_SPOT_META


def make_info():
    """Create hyperliquid Info instance, bypassing testnet spot_meta bug."""
    from hyperliquid.info import Info
    try:
        return Info(settings.api_url, skip_ws=True)
    except (IndexError, KeyError):
        return Info(settings.api_url, skip_ws=True, spot_meta=_EMPTY_SPOT_META)


def make_exchange(account, account_address=None):
    """Create hyperliquid Exchange instance, bypassing testnet spot_meta bug."""
    from hyperliquid.exchange import Exchange
    try:
        return Exchange(account, settings.api_url,
                        account_address=account_address or settings.hyperliquid_main_address)
    except (IndexError, KeyError):
        return Exchange(account, settings.api_url,
                        account_address=account_address or settings.hyperliquid_main_address,
                        spot_meta=_EMPTY_SPOT_META)
