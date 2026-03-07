from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    polymarket_host: str = Field(default='https://clob.polymarket.com', alias='POLYMARKET_HOST')
    polymarket_gamma_url: str = Field(default='https://gamma-api.polymarket.com', alias='POLYMARKET_GAMMA_URL')
    polymarket_data_url: str = Field(default='https://data-api.polymarket.com', alias='POLYMARKET_DATA_URL')
    chain_id: int = Field(default=137, alias='CHAIN_ID')

    private_key: str = Field(default='', alias='PRIVATE_KEY')
    funder_address: str = Field(default='', alias='FUNDER_ADDRESS')
    tracking_user_address: str = Field(default='', alias='TRACKING_USER_ADDRESS')
    signature_type: int = Field(default=1, alias='SIGNATURE_TYPE')

    live_trading: bool = Field(default=False, alias='LIVE_TRADING')
    paper_balance: float = Field(default=100.0, alias='PAPER_BALANCE')
    run_once: bool = Field(default=False, alias='RUN_ONCE')

    seconds_before_resolution: int = Field(default=120, alias='SECONDS_BEFORE_RESOLUTION')
    min_confidence_price: float = Field(default=0.80, alias='MIN_CONFIDENCE_PRICE')
    max_worst_price: float = Field(default=0.97, alias='MAX_WORST_PRICE')
    trade_size_usd: float = Field(default=10.0, alias='TRADE_SIZE_USD')
    stop_loss_price: float = Field(default=0.50, alias='STOP_LOSS_PRICE')
    min_liquidity_on_best_level: float = Field(default=25.0, alias='MIN_LIQUIDITY_ON_BEST_LEVEL')
    poll_interval_seconds: float = Field(default=0.5, alias='POLL_INTERVAL_SECONDS')
    only_one_trade_per_market: bool = Field(default=True, alias='ONLY_ONE_TRADE_PER_MARKET')
    skip_seconds_delayed_markets: bool = Field(default=True, alias='SKIP_SECONDS_DELAYED_MARKETS')
    binance_stale_after_seconds: float = Field(default=5.0, alias='BINANCE_STALE_AFTER_SECONDS')
    binance_volatility_lookback_seconds: int = Field(default=120, alias='BINANCE_VOLATILITY_LOOKBACK_SECONDS')

    auto_settle_paper: bool = Field(default=True, alias='AUTO_SETTLE_PAPER')
    auto_settle_live: bool = Field(default=True, alias='AUTO_SETTLE_LIVE')
    report_every_loop: bool = Field(default=True, alias='REPORT_EVERY_LOOP')

    touchlog_enabled: bool = Field(default=True, alias='TOUCHLOG_ENABLED')
    touchlog_path: str = Field(default='reports/touches.json', alias='TOUCHLOG_PATH')

    journal_path: str = Field(default='journal.json', alias='JOURNAL_PATH')
    reports_dir: str = Field(default='reports', alias='REPORTS_DIR')

    @property
    def effective_tracking_address(self) -> str:
        return self.tracking_user_address or self.funder_address


settings = Settings()
