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
    page: int = 1, size: int = 10, keyword: str | None = None, status: int | None = None,
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    data = await account_service.list_accounts(page, size, keyword, status)
    return result(data)


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


@router.delete("/{phone}")
async def delete_account(phone: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await account_service.delete_account(phone)
    except AccountError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()
