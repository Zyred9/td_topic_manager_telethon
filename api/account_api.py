"""登录小号 /account。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, UploadFile

from api.deps import BizError, CurrentUser, get_current_user, result
from api.schemas import CodeReq, PhoneLoginReq, ProfileReq
from core.batch_store import batch_store
from core.lifecycle import task_tracker
from services import account_service, import_service
from services.account_service import AccountError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/list")
async def list_accounts(
    page: int = 1, size: int = 10, keyword: str | None = None, status: str | None = None,
    deadFilter: str = "alive", deadSort: str = "none",
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    # status 收字符串而非 int:前端清空筛选后可能传空串 ?status=,用 int 类型会触发
    # 422 校验失败致列表整页空白。统一在 service 层把空串/非法值当作"不过滤"。
    # deadFilter: alive(默认,只看正常号) / all(全部) / dead(只看死号)
    # deadSort:   none(默认,login_time DESC) / first(死号靠前) / last(死号靠后)
    data = await account_service.list_accounts(page, size, keyword, status, deadFilter, deadSort)
    return result(data)


@router.get("/{phone}/groups")
async def list_groups(
    phone: str, page: int = 1, size: int = 10, _: CurrentUser = Depends(get_current_user),
) -> dict:
    """该小号已加入的群(分页)。"""
    return result(await account_service.list_groups(phone, page, size))


@router.post("/{phone}/leave-group")
async def leave_group(
    phone: str, body: dict, _: CurrentUser = Depends(get_current_user),
) -> dict:
    """该小号退出指定群。body: {chatId}"""
    chat_id = body.get("chatId")
    if chat_id is None:
        raise BizError("缺少 chatId")
    try:
        await account_service.leave_group(phone, int(chat_id))
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.post("/login")
async def phone_login(req: PhoneLoginReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await account_service.phone_login(req.phone, req.email, req.password)
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.post("/code")
async def submit_code(req: CodeReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await account_service.submit_code(req.phone, req.code)
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.post("/password")
async def submit_2fa(req: CodeReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    # 前端 submit2FAPassword 复用 {phone, code},code 即 2FA 密码
    try:
        await account_service.submit_2fa(req.phone, req.code)
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.post("/{phone}/offline")
async def offline(phone: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    await account_service.offline(phone)
    return result()


@router.put("/{phone}/profile")
async def update_profile(phone: str, req: ProfileReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await account_service.update_profile(phone, req.firstName, req.lastName, req.username, req.bio)
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.post("/{phone}/avatar")
async def upload_avatar(
    phone: str, avatar: UploadFile = File(...), _: CurrentUser = Depends(get_current_user),
) -> dict:
    content = await avatar.read()
    try:
        await account_service.update_avatar(phone, content, avatar.filename or "avatar.jpg")
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.post("/import/zip")
async def import_zip(zip: UploadFile = File(...), _: CurrentUser = Depends(get_current_user)) -> dict:
    content = await zip.read()
    zip_path = import_service.save_upload(content)
    extract_root = zip_path.with_suffix("")  # 同名无后缀目录作解压根
    try:
        total = await _count(zip_path, extract_root)
    except Exception as exc:
        raise BizError(f"解压失败: {exc}") from exc
    batch_id = batch_store.create("import", total, ttl_sec=24 * 3600)
    task_tracker.create_task(import_service.run_import(zip_path, batch_id, extract_root))
    return result(batch_id)


async def _count(zip_path, extract_root) -> int:
    from infra.db import run_db
    return await run_db(import_service.count_phone_dirs, zip_path, extract_root)


@router.get("/import-result/{batch_id}")
async def import_result(batch_id: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    data = batch_store.get(batch_id)
    if data is None:
        raise BizError("批次不存在或已过期", code=404)
    return result(data)


# ---------- 死号 / 发送日志 ----------
# 注意:静态前缀路由必须放在 `/{phone}`、`DELETE /{phone}` 这种 catch-all 之前,
# 否则会被前者拦截。
@router.get("/dead-list")
async def list_dead_accounts(
    page: int = 1, size: int = 20, keyword: str | None = None,
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    """死号列表分页。"""
    data = await account_service.list_dead_accounts(page, size, keyword)
    return result(data)


@router.post("/{phone}/revive")
async def revive_account(
    phone: str, _: CurrentUser = Depends(get_current_user),
) -> dict:
    """复活死号(搬回主列表,待重新登录恢复使用)。"""
    try:
        await account_service.revive_dead(phone)
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.get("/{phone}/send-logs")
async def list_send_logs(
    phone: str, page: int = 1, size: int = 20,
    onlyFailed: bool = False, hours: int | None = None,
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    """单号发送日志分页。onlyFailed 只看失败,hours 最近 N 小时。"""
    data = await account_service.list_send_logs(phone, page, size, onlyFailed, hours)
    return result(data)


@router.delete("/{phone}")
async def delete_account(phone: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await account_service.delete_account(phone)
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()
