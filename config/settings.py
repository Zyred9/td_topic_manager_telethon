"""应用配置。

从 .env 读取(python-dotenv),全部收口到 ``Settings`` 单例。
敏感配置(DB 密码、LLM key、api_id 等)不硬编码,走环境变量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 项目根目录(本文件位于 config/ 下)
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# 服务前缀:多服务部署时,敏感配置走 TDTM_ 开头的系统环境变量避免冲突。
# 详见 _get_prefixed,只对 8 个敏感项启用,其它沿用旧 key。
_ENV_PREFIX = "TDTM_"


def _get(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return value if value is not None and value != "" else default


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


def _get_prefixed(key: str, default: str = "") -> str:
    """敏感配置:优先读 TDTM_<KEY> 防多服务串扰,回退 <KEY> 兼容旧部署。"""
    value = os.getenv(_ENV_PREFIX + key)
    if value is not None and value != "":
        return value
    return _get(key, default)


def _get_prefixed_int(key: str, default: int) -> int:
    try:
        return int(_get_prefixed(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    root_path: str


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    # 连接池:钳住峰值连接数(同时压低 DB 侧 FD 占用),空闲连接复用减少建连开销
    pool_max: int = 16
    pool_min: int = 2


@dataclass(frozen=True)
class TelegramConfig:
    """手机号新登录用的全局凭证;协议号登录时由各号自带 json 覆盖。"""

    api_id: int
    api_hash: str
    device_model: str
    app_version: str
    system_lang: str


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str
    timeout_sec: int


@dataclass(frozen=True)
class AuthConfig:
    jwt_secret: str
    jwt_expire_min: int
    init_admin_user: str
    init_admin_password: str


@dataclass(frozen=True)
class RiskConfig:
    """风控参数(对应需求文档第九节)。"""

    min_interval_ms: int
    max_per_min_per_phone: int
    reply_lock_ttl_sec: int
    join_jitter_min_sec: int
    join_jitter_max_sec: int
    new_account_daily_max: int
    owner_offline_threshold_sec: int


@dataclass(frozen=True)
class WatchConfig:
    """小号健康巡检参数:周期检测在线/授权状态并同步实时资料。"""

    interval_min: int


@dataclass(frozen=True)
class ProxyConfig:
    """SOCKS5 代理,暂不接,留空即直连。"""

    host: Optional[str]
    port: Optional[int]

    @property
    def enabled(self) -> bool:
        return bool(self.host) and bool(self.port)


@dataclass(frozen=True)
class Settings:
    server: ServerConfig
    db: DbConfig
    telegram: TelegramConfig
    llm: LlmConfig
    auth: AuthConfig
    risk: RiskConfig
    watch: WatchConfig
    proxy: ProxyConfig
    base_dir: Path = field(default=BASE_DIR)

    @property
    def sessions_dir(self) -> Path:
        return self.base_dir / "sessions"

    @property
    def upload_dir(self) -> Path:
        return self.base_dir / "data" / "upload"

    @property
    def avatar_dir(self) -> Path:
        return self.base_dir / "data" / "avatar"

    @property
    def media_dir(self) -> Path:
        return self.base_dir / "data" / "media"

    def ensure_dirs(self) -> None:
        """确保运行期目录存在。"""
        for path in (self.sessions_dir, self.upload_dir, self.avatar_dir, self.media_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    proxy_port_raw = os.getenv("TD_PROXY_PORT")
    proxy_port = int(proxy_port_raw) if proxy_port_raw else None

    return Settings(
        server=ServerConfig(
            host=_get("SERVER_HOST", "0.0.0.0"),
            port=_get_prefixed_int("SERVER_PORT", 8813),
            root_path=_get("ROOT_PATH", "/collect"),
        ),
        db=DbConfig(
            host=_get("DB_HOST", "127.0.0.1"),
            port=_get_int("DB_PORT", 3306),
            user=_get_prefixed("DB_USER", "root"),
            password=_get_prefixed("DB_PASSWORD", ""),
            database=_get("DB_NAME", "td_topic_manager"),
            pool_max=_get_int("DB_POOL_MAX", 16),
            pool_min=_get_int("DB_POOL_MIN", 2),
        ),
        telegram=TelegramConfig(
            api_id=_get_prefixed_int("TG_API_ID", 0),
            api_hash=_get_prefixed("TG_API_HASH", ""),
            device_model=_get("TG_DEVICE_MODEL", "Desktop"),
            app_version=_get("TG_APP_VERSION", "1.0.1"),
            system_lang=_get("TG_SYSTEM_LANG", "zh"),
        ),
        llm=LlmConfig(
            base_url=_get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            api_key=_get_prefixed("LLM_API_KEY", ""),
            model=_get("LLM_MODEL", "deepseek-chat"),
            timeout_sec=_get_int("LLM_TIMEOUT_SEC", 30),
        ),
        auth=AuthConfig(
            jwt_secret=_get_prefixed("JWT_SECRET", "change-me"),
            jwt_expire_min=_get_int("JWT_EXPIRE_MIN", 480),
            init_admin_user=_get("INIT_ADMIN_USER", "admin"),
            init_admin_password=_get_prefixed("INIT_ADMIN_PASSWORD", "admin123"),
        ),
        risk=RiskConfig(
            min_interval_ms=_get_int("SEND_MIN_INTERVAL_MS", 1500),
            max_per_min_per_phone=_get_int("SEND_MAX_PER_MIN_PER_PHONE", 8),
            reply_lock_ttl_sec=_get_int("SEND_REPLY_LOCK_TTL_SEC", 2),
            join_jitter_min_sec=_get_int("JOIN_BATCH_JITTER_MIN_SEC", 5),
            join_jitter_max_sec=_get_int("JOIN_BATCH_JITTER_MAX_SEC", 15),
            new_account_daily_max=_get_int("NEW_ACCOUNT_DAILY_MAX", 3),
            owner_offline_threshold_sec=_get_int("TOPIC_OWNER_OFFLINE_THRESHOLD_SEC", 300),
        ),
        watch=WatchConfig(
            interval_min=_get_int("ACCOUNT_WATCH_INTERVAL_MIN", 15),
        ),
        proxy=ProxyConfig(
            host=os.getenv("TD_PROXY_HOST") or None,
            port=proxy_port,
        ),
    )
