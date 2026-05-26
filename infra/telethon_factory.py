"""TelegramClient 构造工厂。

凭证来源分流(防封号核心):
- 协议号:用该号自带 .json 的 app_id/app_hash/device/app_version/lang,
  设备指纹与 session 创建时一致,否则易被 Telegram revoke session 或封号。
- 手机号新登录:用 .env 全局 api_id/api_hash。

代理:settings.proxy 配了才走 SOCKS5,否则直连。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from telethon import TelegramClient

from config.settings import get_settings
from entities.account import Account

try:
    import socks  # type: ignore
except Exception:  # pragma: no cover
    socks = None  # type: ignore

logger = logging.getLogger(__name__)


def _build_proxy() -> Optional[tuple]:
    cfg = get_settings().proxy
    if not cfg.enabled or socks is None:
        return None
    return (socks.SOCKS5, cfg.host, cfg.port, True)


def build_client_for_account(account: Account) -> TelegramClient:
    """按已落库的 Account 构造 client。协议号用自带凭证,手机号用全局。"""
    settings = get_settings()
    proxy = _build_proxy()

    if account.import_type == 2 and account.api_id and account.api_hash:
        return TelegramClient(
            account.session_path,
            account.api_id,
            account.api_hash,
            device_model=account.device_model or settings.telegram.device_model,
            app_version=account.app_version or settings.telegram.app_version,
            system_lang_code=account.lang_pack or settings.telegram.system_lang,
            proxy=proxy,
        )

    tg = settings.telegram
    return TelegramClient(
        account.session_path,
        tg.api_id,
        tg.api_hash,
        device_model=tg.device_model,
        app_version=tg.app_version,
        system_lang_code=tg.system_lang,
        proxy=proxy,
    )


def build_client_for_phone(session_path: str) -> TelegramClient:
    """手机号新登录:全局凭证。"""
    tg = get_settings().telegram
    return TelegramClient(
        session_path,
        tg.api_id,
        tg.api_hash,
        device_model=tg.device_model,
        app_version=tg.app_version,
        system_lang_code=tg.system_lang,
        proxy=_build_proxy(),
    )


def build_client_from_credentials(
    session_path: str,
    api_id: int,
    api_hash: str,
    device_model: Optional[str] = None,
    app_version: Optional[str] = None,
    lang_pack: Optional[str] = None,
) -> TelegramClient:
    """协议号导入阶段(尚未落库)用解析出的 json 凭证直接构造。"""
    tg = get_settings().telegram
    return TelegramClient(
        session_path,
        api_id,
        api_hash,
        device_model=device_model or tg.device_model,
        app_version=app_version or tg.app_version,
        system_lang_code=lang_pack or tg.system_lang,
        proxy=_build_proxy(),
    )


def parse_account_json(json_path: Path) -> dict[str, Any]:
    """解析协议号自带 .json,提取建 client 需要的凭证字段。

    缺字段时返回 {} 对应位置为 None,调用方回退全局凭证。
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("解析协议号 json 失败 path=%s err=%s", json_path, exc)
        return {}

    api_id_raw = data.get("app_id")
    try:
        api_id = int(api_id_raw) if api_id_raw is not None else None
    except (TypeError, ValueError):
        api_id = None

    return {
        "api_id": api_id,
        "api_hash": data.get("app_hash"),
        "device_model": data.get("device"),
        "app_version": data.get("app_version"),
        "lang_pack": data.get("lang_pack"),
        "two_fa": data.get("twoFA") or data.get("password") or None,
        "phone": _normalize_phone(data.get("phone")),
    }


def _normalize_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    p = str(phone).strip()
    return p if p.startswith("+") else f"+{p}"
