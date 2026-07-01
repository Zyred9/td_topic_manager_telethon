"""协议号 zip 导入。

支持两种 zip 布局,扫描前统一归一为「每号一目录」:

布局 A(扁平,文件名=手机号):
  <root>/
    <phone>.session   <phone>.json      ← 多号并列,无子目录
    ...
  (json 字段同布局 B,session_file 可带或不带 + 前缀)

布局 B(每号一子目录):
  +<phone>/
    +<phone>.session  ← Telethon SQLite session,原生可连
    +<phone>.json     ← 自带 app_id/app_hash/device/app_version/lang/twoFA
    2fa.txt           ← 二级密码(= json.twoFA)
    tdata/            ← TDLib 残留,忽略

流程:存 zip → 异步解压 → 归一化布局 → 扫号目录 → 每号提取 .session + .json →
复制 session 到 sessions/ → upsert(import_type=2,存自带凭证)→ 用自带凭证 connect 校验授权 →
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
from helpers.dead_account import is_dead_error
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


_PHONE_FROM_NAME_RE = re.compile(r"^\+?(\d{8,15})$")


def _normalize_flat_layout(extract_root: Path) -> None:
    """把扁平布局(<phone>.session 直接躺在根目录)归一为「每号一目录」。

    规则:扫根目录下的 *.session 文件,文件主名若是 8~15 位数字(允许 + 前缀),
    则把同主名的所有兄弟文件(.session/.json/2fa.txt 等)移进 `+<phone>/` 子目录。
    已是子目录布局时本函数为 no-op。
    """
    if not extract_root.is_dir():
        return
    # 先收集需要搬运的「主名 → phone」映射,避免边遍历边改
    plans: dict[str, str] = {}
    for child in extract_root.iterdir():
        if not child.is_file():
            continue
        if child.suffix.lower() != ".session":
            continue
        stem = child.stem
        m = _PHONE_FROM_NAME_RE.match(stem)
        if not m:
            continue
        plans[stem] = f"+{m.group(1)}"

    for stem, phone in plans.items():
        target_dir = extract_root / phone
        target_dir.mkdir(exist_ok=True)
        # 同主名的全部文件都搬过去(.session / .json / 任何后缀),
        # 同主名+_2fa.txt 之类副产物也一并带走;不动其他号或目录。
        for sibling in extract_root.iterdir():
            if not sibling.is_file():
                continue
            if sibling.stem != stem and not sibling.name.startswith(f"{stem}."):
                continue
            try:
                sibling.rename(target_dir / sibling.name)
            except OSError as exc:
                logger.warning("[导入] 归一化布局移动失败 src=%s err=%s", sibling, exc)


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
    """先解压并统计号目录数(用于 batch total)。两种布局都支持。"""
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_root)
    _normalize_flat_layout(extract_root)
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
    # 清理解压临时目录,保留原始 zip 供用户重传使用
    shutil.rmtree(extract_root, ignore_errors=True)
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

    # connect() 必须纳入 try:Telethon 读 .session(SQLite)时,若 session 是新版
    # Telethon/第三方工具写的、列数与当前运行版本不一致,会在 connect 阶段抛
    # ValueError("too many values to unpack (...)"),此前在 try 外裸抛会冒泡到
    # run_import 的兜底分支,把英文原文当 failReason 泄给前端,且 client 不 disconnect 泄漏。
    registered = False
    try:
        await client.connect()
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
            # session 已失效(authkey 不被服务端承认):协议号无法靠验证码重登,该号已无用
            # → 判死进死号列表(对齐 client_manager._connect_account 启动口径)。
            await run_db(account_repo.mark_dead, phone,
                         "SessionInvalid", "导入校验时 session 已失效(未授权)")
            batch_store.set_item(batch_id, phone, ITEM_FAILED,
                                 fail_reason="会话已失效,已判为死号(可在死号列表复活或删除)")
    except errors.FloodWaitError as exc:
        await run_db(account_repo.update_status, phone, int(AccountStatus.NEED_RELOGIN))
        batch_store.set_item(batch_id, phone, ITEM_FAILED,
                             fail_reason=f"频率限制,请等待 {exc.seconds} 秒后重试")
    except ValueError as exc:
        # session 文件格式与当前 Telethon 版本不兼容(典型:too many values to unpack)。
        # 这是版本不匹配,不是号本身坏:新版工具写的多列 session 喂给旧版 Telethon 读不了。
        # 给运营能看懂的中文,指向真正的解法(升级 telethon),不再泄英文原文。
        logger.warning("[导入] %s session 格式与当前 Telethon 不兼容: %s", phone, exc)
        await run_db(account_repo.update_status, phone, int(AccountStatus.NEED_RELOGIN))
        batch_store.set_item(
            batch_id, phone, ITEM_FAILED,
            fail_reason="session 格式与当前 Telethon 版本不兼容,请升级服务端 telethon 或改手机号登录",
        )
    except Exception as exc:
        # 任何其它异常(连接错误/RPC 错误)也要标记失败,避免 client 泄漏。
        if is_dead_error(exc):
            # 终态死号异常(session 作废/封号/多 IP 撞车)→ 判死进死号列表。
            logger.warning("[导入] %s 连接校验死号异常 reason=%s", phone, type(exc).__name__)
            await run_db(account_repo.mark_dead, phone, type(exc).__name__, str(exc))
            batch_store.set_item(batch_id, phone, ITEM_FAILED,
                                 fail_reason=f"账号已失效({type(exc).__name__}),已判为死号")
        else:
            # 非终态:记完整堆栈便于排查,降级为需重登,不向上抛(避免英文异常覆盖中文 reason)。
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
            # 导入失败则清理已复制的 session 文件,避免残留垃圾文件影响下次重传
            try:
                session_dst.unlink(missing_ok=True)
            except Exception:
                pass


def _find_file(dir_path: Path, suffix: str) -> Optional[Path]:
    for p in dir_path.iterdir():
        if p.is_file() and p.name.lower().endswith(suffix):
            return p
    return None
