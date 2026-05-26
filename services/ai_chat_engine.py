"""AI 聊天引擎:人设渲染 + deepseek 调用 + 长度控制 + 复读检测 + 附和短句。

- 人设结构化字段 → 自然语言(渲染规则固定)
- deepseek(OpenAI 兼容)生成回复
- 长度:仅统计汉字,目标 N=random(min,max) 软约束,硬上限 50 截断
- 复读检测:与最近上下文过于相似则重生(最多 2 次)
- 附和短句:仅自驱场景,按概率从人设口头禅 + 通用池抽
"""

from __future__ import annotations

import json
import logging
import random
from typing import List, Optional

import httpx

from config.constants import FILLER_POOL, MAX_HAN_CHARS
from config.settings import get_settings
from helpers.han_counter import count_han, truncate_by_han

logger = logging.getLogger(__name__)


# ---------- 人设渲染 ----------
_GENDER = {"male": "男生", "female": "女生"}
_STYLE = {
    "short": "说话风格:短句多,20 字以内为主",
    "long": "说话风格:长句为主,会展开讲",
    "mixed": "说话风格:长短句混合",
}
_EMOJI = {
    "never": "几乎不用 emoji",
    "sometimes": "偶尔用 emoji,每条不超过 1 个",
    "often": "经常用 emoji,但不过度",
}


def render_persona(persona: dict) -> str:
    parts: List[str] = []
    name = persona.get("name")
    if name:
        parts.append(f"昵称:{name}")
    gender = _GENDER.get(persona.get("gender") or "")
    age = persona.get("age")
    if gender and age:
        parts.append(f"{gender},{age}岁")
    elif gender:
        parts.append(gender)
    if persona.get("tone"):
        parts.append("性格:" + "、".join(persona["tone"]))
    if persona.get("interests"):
        parts.append("兴趣:" + "、".join(persona["interests"]))
    if persona.get("catchphrases"):
        parts.append("口头禅:" + "、".join(persona["catchphrases"]) + "(可以偶尔用,不要每句都带)")
    style = _STYLE.get(persona.get("style") or "")
    if style:
        parts.append(style)
    emoji = _EMOJI.get(persona.get("emojiLevel") or "")
    if emoji:
        parts.append(emoji)
    if persona.get("extraPrompt"):
        parts.append("额外要求:" + persona["extraPrompt"])
    return ";".join(parts) if parts else "一个普通的群成员"


def _target_len(persona: dict, topic_min: int, topic_max: int) -> int:
    rng = persona.get("replyLenRange")
    if rng and len(rng) == 2:
        lo, hi = int(rng[0]), int(rng[1])
    else:
        lo, hi = topic_min, topic_max
    lo = max(1, min(lo, MAX_HAN_CHARS))
    hi = max(lo, min(hi, MAX_HAN_CHARS))
    return random.randint(lo, hi)


def _build_system_prompt(persona: dict, scene: str, target_n: int) -> str:
    return (
        "你是一个真实的 Telegram 群用户,正在和其他人聊天。\n"
        f"【你的人设】{render_persona(persona)}\n"
        f"【当前话题/场景】{scene}\n"
        "【输出规则】\n"
        "- 口语化、自然,像真人随口说话\n"
        "- 不要暴露 AI 身份,不要用「作为一个 AI」之类措辞\n"
        "- 不要复读别人的话,不要客套\n"
        f"- 本次回复目标 ≤ {target_n} 个汉字(emoji/英文/标点不计),自然控制长度,不要硬凑也不要解释字数\n"
        "- 只输出聊天内容本身,不要加引号、不要加前缀"
    )


# ---------- deepseek 调用 ----------
async def _chat_completion(messages: list[dict]) -> str:
    cfg = get_settings().llm
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    payload = {"model": cfg.model, "messages": messages, "temperature": 1.0, "max_tokens": 200}
    async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def _too_similar(text: str, recent: List[str]) -> bool:
    """简单复读检测:与最近任一条编辑距离 < 5 视为太像。"""
    for r in recent:
        if _edit_distance(text, r) < 5:
            return True
    return False


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    m, n = len(a), len(b)
    if m == 0 or n == 0:
        return max(m, n)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[n]


# ---------- 对外:生成回复 ----------
async def generate_reply(
    persona: dict,
    scene: str,
    context_msgs: List[str],
    reply_target: Optional[str],
    topic_min: int,
    topic_max: int,
) -> str:
    """生成一条回复。context_msgs 为最近消息("发送者: 内容"),reply_target 为要回应的真人发言。"""
    target_n = _target_len(persona, topic_min, topic_max)
    system = _build_system_prompt(persona, scene, target_n)
    messages = [{"role": "system", "content": system}]
    for c in context_msgs:
        messages.append({"role": "user", "content": c})
    if reply_target:
        messages.append({"role": "user", "content": f"请回复这条消息(简短自然):{reply_target}"})

    text = ""
    for attempt in range(3):  # 1 次正常 + 最多 2 次复读重生
        try:
            text = await _chat_completion(messages)
        except Exception as exc:
            logger.error("deepseek 调用失败: %s", exc)
            return ""
        text = text.strip().strip('"').strip("「」")
        if count_han(text) > MAX_HAN_CHARS:
            text = truncate_by_han(text, MAX_HAN_CHARS)
        if not _too_similar(text, context_msgs):
            break
    return text


async def generate_sample(persona: dict, scene: str) -> str:
    """试一句预览。"""
    return await generate_reply(persona, scene, [], None, topic_min=8, topic_max=25)


def pick_filler(persona: dict) -> str:
    """附和短句:人设口头禅 ∪ 通用池。"""
    pool = list(FILLER_POOL)
    cps = persona.get("catchphrases") or []
    pool.extend(cps)
    return random.choice(pool)
