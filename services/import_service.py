"""协议号 zip 导入。

zip 结构(每号一目录):
  +<phone>/
    +<phone>.session   ← Telethon SQLite session,原生可连
    +<phone>.json      ← 自带 app_id/app_hash/device/app_version/lang/twoFA
    2fa.txt            ← 二级密码(= json.twoFA)
    tdata/             ← TDLib 残留,忽略

流程:存 zip → 异步解压扫描 → 每号提取 .session + .json → 复制 session 到 sessions/ →
upsert(import_type=2,存自带凭证)→ 用自带凭证 connect 校验授权 →
授权 OK status=3;需 2FA 且有 two_fa 自动补;否则 status=2/6。进度写 batch_store。
"""

from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from telethon import errors

from config.constants import AccountStatus
from config.settings import get_settings
from core import update_router  # noqa: F401
from core.batch_store import ITEM_FAILED, ITEM_SUCCESS, batch_store
from core.client_manager import client_manager
from infra import telethon_factory
from infra.db import run_db
from repositories import account_repo

logger = logging.getLogger(__name__)

# 目录名 = 手机号(允许 phone_ / phone= 前缀,8~15 位数字)
_DIR_RE = re.compile(r"^(?:phone[_=])?(\+?\d{8,15})$")


def save_upload(file_bytes: bytes) -> Path:
    settings = get_settings()
    import uuid
    path = settings.upload_dir / f"{uuid.uuid4().hex}.zip"
    path.write_bytes(file_bytes)
    return path


def scan_phone_dirs(extract_root: Path) -> list[tuple[str, Path]]:
    """扫描解压根下的号目录,返回 [(规范化phone, 目录)]。"""
    found: list[tuple[str, Path]] = []
    for child in extract_root.iterdir():
        if not child.is_dir():
            continue
        m = _DIR_RE.match(child.name)
        if not m:
            continue
        phone = m.group(1)
        if not phone.startswith("+"):
            phone = f"+{phone}"
        found.append((phone, child))
    return found


def count_phone_dirs(zip_path: Path, extract_root: Path) -> int:
    """先解压并统计号目录数(用于 batch total)。"""
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_root)
    return len(scan_phone_dirs(extract_root))


async def run_import(zip_path: Path, batch_id: str, extract_root: Path) -> None:
    """异步执行导入(已解压)。逐号处理并写 batch_store。"""
    settings = get_settings()
    dirs = scan_phone_dirs(extract_root)
    if not dirs:
        logger.warning("[导入] batch=%s 未扫描到任何号目录", batch_id)
        batch_store.finish(batch_id)
        return

    for phone, dir_path in dirs:
        try:
            await _import_one(phone, dir_path, settings.sessions_dir, batch_id)
        except Exception as exc:
            logger.exception("[导入] %s 失败", phone)
            batch_store.set_item(batch_id, phone, ITEM_FAILED, fail_reason=str(exc))

    batch_store.finish(batch_id)
    # 清理解压临时目录与 zip
    shutil.rmtree(extract_root, ignore_errors=True)
    try:
        zip_path.unlink(missing_ok=True)
    except Exception:
        pass
    logger.info("[导入] batch=%s 完成,共 %d 个号", batch_id, len(dirs))


async def _import_one(phone: str, dir_path: Path, sessions_dir: Path, batch_id: str) -> None:
    # 找 .session 与 .json
    session_src = _find_file(dir_path, ".session")
    if session_src is None:
        batch_store.set_item(batch_id, phone, ITEM_FAILED, fail_reason="目录内缺少 .session 文件")
        return
    json_src = _find_file(dir_path, ".json")
    meta = telethon_factory.parse_account_json(json_src) if json_src else {}

    # 2fa.txt 兜底
    two_fa: Optional[str] = meta.get("two_fa")
    if not two_fa:
        txt = dir_path / "2fa.txt"
        if txt.exists():
            two_fa = txt.read_text(encoding="utf-8", errors="ignore").strip() or None

    # 冲突检查:已登录则跳过
    existing = await run_db(account_repo.find_by_phone, phone)
    if existing is not None and existing.status == int(AccountStatus.LOGGED_IN):
        batch_store.set_item(batch_id, phone, ITEM_FAILED,
                             fail_reason="该号已登录,如需替换请先在小号列表删除")
        return

    # 复制 session 到 sessions/
    session_dst = sessions_dir / f"{phone}.session"
    shutil.copyfile(session_src, session_dst)

    api_id = meta.get("api_id")
    api_hash = meta.get("api_hash")
    await run_db(
        account_repo.upsert_protocol, phone, str(session_dst),
        api_id, api_hash, meta.get("device_model"),
        meta.get("app_version"), meta.get("lang_pack"), two_fa,
    )

    # 用自带凭证连接校验(凭证缺失则回退全局)
    if api_id and api_hash:
        client = telethon_factory.build_client_from_credentials(
            str(session_dst), api_id, api_hash,
            meta.get("device_model"), meta.get("app_version"), meta.get("lang_pack"),
        )
    else:
        client = telethon_factory.build_client_for_phone(str(session_dst))

    await client.connect()
    registered = False
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            await client_manager.register_ready_client(phone, client)
            registered = True  # client 已交给池接管,后续不可在此 disconnect
            await run_db(
                account_repo.mark_logged_in, phone,
                getattr(me, "id", None), getattr(me, "first_name", None),
                getattr(me, "last_name", None), getattr(me, "username", None),
            )
            batch_store.set_item(batch_id, phone, ITEM_SUCCESS, note="已登录")
            logger.info("[导入] %s 登录成功", phone)
        else:
            # session 失效,需走手机号补登
            await run_db(account_repo.update_status, phone, int(AccountStatus.NEED_RELOGIN))
            batch_store.set_item(batch_id, phone, ITEM_FAILED,
                                 fail_reason="会话已失效,请改走手机号登录")
    except errors.FloodWaitError as exc:
        await run_db(account_repo.update_status, phone, int(AccountStatus.NEED_RELOGIN))
        batch_store.set_item(batch_id, phone, ITEM_FAILED,
                             fail_reason=f"频率限制,请等待 {exc.seconds} 秒后重试")
    except Exception:
        # 任何其它异常(连接错误/RPC 错误)也要标记失败,避免 client 泄漏。
        # 记完整堆栈便于排查,但不向上抛(已写明细),避免上层用英文异常覆盖中文 reason。
        logger.exception("[导入] %s 连接校验异常", phone)
        await run_db(account_repo.update_status, phone, int(AccountStatus.NEED_RELOGIN))
        batch_store.set_item(batch_id, phone, ITEM_FAILED, fail_reason="连接校验失败,请改走手机号登录")
    finally:
        # 未进池的 client 一律断开,防 TCP 连接与读循环泄漏
        if not registered:
            try:
                await client.disconnect()
            except Exception:
                pass


def _find_file(dir_path: Path, suffix: str) -> Optional[Path]:
    for p in dir_path.iterdir():
        if p.is_file() and p.name.lower().endswith(suffix):
            return p
    return None
