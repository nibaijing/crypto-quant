"""核心配置系统 - 基于 Pydantic 的类型安全配置"""

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, Literal
from pathlib import Path


class ExchangeRestConfig(BaseModel):
    base_url: str = "https://www.okx.com"
    testnet_url: str = "https://www.okx.com"
    timeout: int = 30
    max_retries: int = 3
    rate_limit: bool = True


class ExchangeWsConfig(BaseModel):
    public_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    private_url: str = "wss://ws.okx.com:8443/ws/v5/private"
    testnet_public_url: str = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
    testnet_private_url: str = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"
    ping_interval: int = 20


class ExchangeConfig(BaseModel):
    name: str = "okx"
    testnet: bool = True
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    passphrase: Optional[str] = None
    rest: ExchangeRestConfig = ExchangeRestConfig()
    ws: ExchangeWsConfig = ExchangeWsConfig()


class BacktestConfig(BaseModel):
    start_date: str = "2026-01-01"
    end_date: str = "2026-04-28"
    initial_capital: float = 10000
    commission: float = 0.0005
    slippage: float = 0.0001
    funding_rate_interval: int = 8
    default_funding_rate: float = 0.0001


class RiskConfig(BaseModel):
    max_total_position_pct: float = 0.5
    max_single_position_pct: float = 0.2
    max_drawdown_pct: float = 0.15
    max_consecutive_losses: int = 5
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.10


class FuturesConfig(BaseModel):
    default_leverage: int = 3
    max_leverage: int = 10
    margin_mode: Literal["isolated", "cross"] = "isolated"
    funding_rate_threshold: float = 0.001
    liquidation_buffer: float = 0.1


class StorageConfig(BaseModel):
    kline_dir: str = "data/klines"
    backtest_results: str = "data/backtest_results"
    db_path: str = "data/trading.db"


class MonitorConfig(BaseModel):
    report_interval: int = 3600
    telegram_enabled: bool = False
    telegram_chat_id: str = ""


class ProjectConfig(BaseModel):
    name: str = "CryptoQuant"
    version: str = "0.1.0"
    log_level: str = "INFO"


class AppConfig(BaseSettings):
    """全局应用配置 - 从 YAML + 环境变量加载"""
    
    model_config = SettingsConfigDict(
        env_prefix="CQ_",
        env_nested_delimiter="__",
        yaml_file=None,  # 通过工厂方法加载
    )
    
    project: ProjectConfig = ProjectConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    backtest: BacktestConfig = BacktestConfig()
    risk: RiskConfig = RiskConfig()
    futures: FuturesConfig = FuturesConfig()
    storage: StorageConfig = StorageConfig()
    monitor: MonitorConfig = MonitorConfig()
    
    # 运行时配置
    symbol: str = "BTC-USDT-SWAP"        # 合约交易对
    spot_symbol: str = "BTC-USDT"         # 现货币对
    timeframe: str = "1H"                 # 默认K线周期
    
    @property
    def project_root(self) -> Path:
        return Path(__file__).parent.parent
    
    @property
    def kline_path(self) -> Path:
        p = self.project_root / self.storage.kline_dir
        p.mkdir(parents=True, exist_ok=True)
        return p
    
    @property
    def results_path(self) -> Path:
        p = self.project_root / self.storage.backtest_results
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def from_yaml(cls, yaml_path: str | Path = None) -> "AppConfig":
        """从 YAML 文件加载配置"""
        import yaml
        
        if yaml_path is None:
            yaml_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)
        
        # 展平嵌套字典为 Pydantic 期望的格式
        config_data = {}
        for section, values in data.items():
            if isinstance(values, dict):
                config_data[section] = values
            else:
                config_data[section] = values
        
        return cls(**config_data)


# 全局单例
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """获取全局配置单例"""
    global _config
    if _config is None:
        _config = AppConfig.from_yaml()
    return _config


def init_config(yaml_path: str = None) -> AppConfig:
    """初始化配置（可指定 YAML 路径）"""
    global _config
    _config = AppConfig.from_yaml(yaml_path)
    return _config