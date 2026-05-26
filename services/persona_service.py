"""AI 人设:读写、预置模板(5 内置 + 自定义)、试一句预览。"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from config.constants import BUILTIN_PRESETS
from infra.db import run_db
from repositories import account_repo, persona_preset_repo

logger = logging.getLogger(__name__)


class PersonaError(Exception):
    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _normalize(phone: str) -> str:
    p = (phone or "").strip()
    return p if p.startswith("+") else f"+{p}"


# ---------- 预置模板 ----------
async def list_presets() -> List[dict]:
    """5 内置(常量)+ 自定义(DB)。前端 PersonaPreset{id,presetName,builtin,preset}。"""
    presets = [dict(p) for p in BUILTIN_PRESETS]
    rows = await run_db(persona_preset_repo.list_all)
    for r in rows:
        presets.append({
            "id": str(r["id"]),
            "presetName": r["preset_name"],
            "builtin": False,
            "preset": json.loads(r["preset_json"]),
        })
    return presets


async def add_preset(preset_name: str, preset: dict) -> str:
    if not preset_name.strip():
        raise PersonaError("模板名不能为空")
    preset_id = await run_db(persona_preset_repo.insert, preset_name.strip(), preset)
    return str(preset_id)


async def delete_preset(preset_id: str) -> None:
    if preset_id.startswith("builtin-"):
        raise PersonaError("内置模板不可删除")
    try:
        pid = int(preset_id)
    except ValueError as exc:
        raise PersonaError("无效的模板 ID") from exc
    await run_db(persona_preset_repo.delete, pid)


# ---------- 小号人设 ----------
async def get_persona(phone: str) -> Optional[dict]:
    phone = _normalize(phone)
    acc = await run_db(account_repo.find_by_phone, phone)
    if acc is None or not acc.persona_json:
        return None
    try:
        return json.loads(acc.persona_json)
    except json.JSONDecodeError:
        return None


async def save_persona(phone: str, persona: dict) -> None:
    phone = _normalize(phone)
    acc = await run_db(account_repo.find_by_phone, phone)
    if acc is None:
        raise PersonaError("小号不存在")
    # clamp replyLenRange 到 [1,50]
    rng = persona.get("replyLenRange")
    if rng and len(rng) == 2:
        lo = max(1, min(int(rng[0]), 50))
        hi = max(lo, min(int(rng[1]), 50))
        persona["replyLenRange"] = [lo, hi]
    await run_db(account_repo.update_persona, phone, json.dumps(persona, ensure_ascii=False))
    logger.info("保存人设 phone=%s", phone)


async def preview(phone: str, persona: Optional[dict], topic_prompt: Optional[str]) -> str:
    """试一句:用草稿人设(前端传)或已存人设生成一句,不落库不发送。"""
    from services.ai_chat_engine import generate_sample
    phone = _normalize(phone)
    if persona is None:
        persona = await get_persona(phone) or {}
    scene = topic_prompt or "一群朋友在群里随便聊天,你随便说一句"
    return await generate_sample(persona, scene)
