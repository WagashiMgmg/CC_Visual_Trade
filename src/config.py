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

    # Internals (not from .env)
    candle_count: int = 100
    limit_order_timeout_secs: int = 30
    close_limit_timeout_secs: int = 60
    position_max_duration_secs: int = 3600  # 1 hour

    @property
    def api_url(self) -> str:
        from hyperliquid.utils import constants
        return constants.TESTNET_API_URL if self.testnet else constants.MAINNET_API_URL


settings = Settings()
