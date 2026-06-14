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
import time
from typing import List, Optional

import httpx

from config.constants import FILLER_POOL, MAX_HAN_CHARS
from config.settings import get_settings
from helpers.han_counter import count_han, truncate_by_han

logger = logging.getLogger(__name__)

# ---------- emoji 库(带内存缓存,避免每条回复查库) ----------
_emoji_cache: tuple[float, List[str]] = (0.0, [])
_EMOJI_CACHE_TTL = 60.0
# emojiLevel → 回复后拼 emoji 的概率
_EMOJI_PROB = {"never": 0.0, "sometimes": 0.3, "often": 0.7}


async def _load_emojis() -> List[str]:
    global _emoji_cache
    now = time.monotonic()
    if _emoji_cache[1] and _emoji_cache[0] > now:
        return _emoji_cache[1]
    from infra.db import run_db
    from repositories import persona_config_repo
    try:
        values = await run_db(persona_config_repo.list_values, "emoji")
    except Exception as exc:
        logger.warning("加载 emoji 库失败: %s", exc)
        values = []
    _emoji_cache = (now + _EMOJI_CACHE_TTL, values)
    return values


async def _append_emoji(text: str, persona: dict) -> str:
    """按人设 emojiLevel 概率从 emoji 库随机抽一个拼到回复末尾。"""
    if not text:
        return text
    level = persona.get("emojiLevel") or "never"
    prob = _EMOJI_PROB.get(level, 0.0)
    if prob <= 0 or random.random() >= prob:
        return text
    emojis = await _load_emojis()
    if not emojis:
        return text
    return text + random.choice(emojis)


# ---------- 人设渲染 ----------
_GENDER = {"male": "男生", "female": "女生"}
_STYLE = {
    "short": "说话风格短句多,20字以内为主",
    "long": "说话风格长句为主,会展开讲",
    "mixed": "说话风格长短句混合",
}


def render_persona(persona: dict) -> str:
    parts: List[str] = []
    name = persona.get("name")
    if name:
        parts.append(f"你叫{name}")
    gender = _GENDER.get(persona.get("gender") or "")
    age = persona.get("age")
    if gender and age:
        parts.append(f"你是一个{age}岁的{gender}")
    elif gender:
        parts.append(f"你是一个{gender}")
    if persona.get("tone"):
        parts.append(f"性格{'、'.join(persona['tone'])}")
    if persona.get("interests"):
        parts.append(f"对{'、'.join(persona['interests'])}比较了解,聊天时可以自然提到这些话题")
    style = _STYLE.get(persona.get("style") or "")
    if style:
        parts.append(style)
    if persona.get("catchphrases"):
        parts.append(f"偶尔会说{'、'.join(persona['catchphrases'])}这样的口头禅,但不要每句都用")
    if persona.get("extraPrompt"):
        parts.append(persona["extraPrompt"])
    return "。".join(parts) + "。" if parts else "你是一个普通的群成员。"


# ---------- 情绪标签(P4:每次发言 30% 概率注入,让同一个号在不同时间口气不同) ----------
_MOODS = [
    "你现在心情挺不错",
    "你刚被某件小事惹烦了一点",
    "你有点困了,反应慢",
    "你刚吃完饭,心情松弛",
    "你最近迷上了某件事,聊到容易跑偏",
    "你今天刷到一个有意思的东西想分享",
    "你刚被人怼了一下,有点不爽",
    "你心情平平,没什么特别的",
]
_MOOD_PROB = 0.30


def _pick_mood() -> Optional[str]:
    if random.random() < _MOOD_PROB:
        return random.choice(_MOODS)
    return None


# ---------- 话题漂移(P4:15% 概率允许临时跑题) ----------
_DRIFT_PROB = 0.15


def _allow_drift() -> bool:
    return random.random() < _DRIFT_PROB


def _target_len(persona: dict, topic_min: int, topic_max: int) -> tuple[int, int, int]:
    """返回 (lo, hi, target):lo/hi 为配置的字数区间,target 为本次随机目标。

    优先级:话题级 topic_min/topic_max 永远生效;人设级 replyLenRange 仅作为
    话题级缺失(None / 非法值)时的兜底。
    运营在话题编辑页改字数是他能直接看到的操作,必须立刻反映到 LLM,不被人设覆盖。
    lo/hi 即严格区间上下界(均 clamp 到 [1, MAX_HAN_CHARS]),供生成后硬约束使用;
    target 为区间内随机单点,仅用于在 prompt 里给 LLM 一个具体目标值。
    """
    if topic_min and topic_max and topic_min <= topic_max:
        lo, hi = int(topic_min), int(topic_max)
    else:
        rng = persona.get("replyLenRange")
        if rng and len(rng) == 2:
            lo, hi = int(rng[0]), int(rng[1])
        else:
            lo, hi = 10, 30
    lo = max(1, min(lo, MAX_HAN_CHARS))
    hi = max(lo, min(hi, MAX_HAN_CHARS))
    target = random.randint(lo, hi)
    return lo, hi, target


def _build_system_prompt(
    persona: dict,
    scene: str,
    lo: int,
    hi: int,
    target_n: int,
    mood: Optional[str],
    allow_drift: bool,
) -> str:
    persona_text = render_persona(persona)
    mood_line = f"\n{mood}。" if mood else ""
    # 严格字数:明确给区间 + 本次目标值,且强调只算汉字、不准超不准少。
    # LLM 仍可能越界,生成后由 generate_reply 硬约束兜底(超 hi 截断、不足 lo 重生)。
    length_rule = (
        f"这次只说 {lo}-{hi} 个汉字(目标 {target_n} 字左右),"
        f"必须落在这个区间内,不要少于 {lo} 字也不要多于 {hi} 字;"
        "字数只算汉字,标点、表情、英文不算"
    )
    drift_rule = (
        " 如果你刚好想到别的事(吃的、天气、最近遇到的、突然冒出来的念头),可以临时岔开聊,不必每句都扣题。"
        if allow_drift else ""
    )
    return (
        f"{persona_text}{mood_line}\n"
        f"当前群聊: {scene}\n"
        f"用你的语气接着聊,就像平时聊天那样。{drift_rule}{length_rule}。"
        "不用点名(如「老王」「兄弟」),不用客套。直接说,不加引号不解释。"
    )


# ---------- deepseek 调用 ----------
async def _chat_completion(messages: list[dict]) -> str:
    cfg = get_settings().llm
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    payload = {"model": cfg.model, "messages": messages, "temperature": 1.0, "max_tokens": 200}
    async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.error("deepseek API 非 200: status=%d body=%.500s", resp.status_code, resp.text)
            raise Exception(f"deepseek API error {resp.status_code}")
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
_MAX_ATTEMPTS = 3  # 1 次正常 + 最多 2 次重生(复读 / 字数不达标共用此预算)


async def generate_reply(
    persona: dict,
    scene: str,
    context_msgs: List[str],
    reply_target: Optional[str],
    topic_min: int,
    topic_max: int,
    strict_len: bool = True,
) -> str:
    """生成一条回复。context_msgs 为最近消息("发送者: 内容"),reply_target 为要回应的真人发言。

    严格字数(strict_len=True,正式发送默认):汉字数硬约束在配置区间 [lo, hi] 内 —
      超 hi 直接截断到 hi;不足 lo 触发重生(共享 _MAX_ATTEMPTS 预算),重试用尽
      仍不足 lo 则返回 "" 丢弃这条(上层 ``if not text: return`` 不发送)。
    非严格(strict_len=False,前端「试一句」预览用):超 hi 仍截断,但不足 lo 不丢弃,
      尽量返回内容,避免预览空白。
    """
    lo, hi, target_n = _target_len(persona, topic_min, topic_max)
    mood = _pick_mood()
    allow_drift = _allow_drift()
    system = _build_system_prompt(persona, scene, lo, hi, target_n, mood, allow_drift)
    messages = [{"role": "system", "content": system}]
    for c in context_msgs:
        messages.append({"role": "user", "content": c})
    if reply_target:
        messages.append({"role": "user", "content": f"上面这条是别人刚发的,你接一句:{reply_target}"})

    text = ""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            raw = await _chat_completion(messages)
        except Exception as exc:
            logger.error("deepseek 调用失败(第%d次): %s", attempt + 1, exc)
            if attempt < _MAX_ATTEMPTS - 1:
                continue
            return ""
        text = raw.strip().strip('"').strip("「」")

        # 1) 超上限:截断到配置 hi(优先句末标点),保证不超区间
        han = count_han(text)
        if han > hi:
            text = truncate_by_han(text, hi)
            han = count_han(text)

        # 2) 复读:与最近上下文过于相似 → 重生(还有预算时)
        if _too_similar(text, context_msgs) and attempt < _MAX_ATTEMPTS - 1:
            # 把上一轮输出(assistant)+ 反馈(user)入历史,LLM 才知道在改哪句
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "这句和前面太像了,换个说法重说一句。"})
            continue

        # 3) 不足下限:重生(还有预算时);用尽预算后严格模式丢弃,非严格放行
        if han < lo:
            if attempt < _MAX_ATTEMPTS - 1:
                # 把"太短"反馈回去,否则 LLM 大概率重复同样长度的输出,重生形同虚设
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": f"刚才那句太短了(只有 {han} 个汉字),展开多说点,凑够 {lo}-{hi} 个汉字再说一遍。",
                })
                continue
            if strict_len:
                logger.debug("字数不达标丢弃: 实得 %d 字 < 下限 %d", han, lo)
                return ""

        break

    # 按 emojiLevel 概率从 emoji 库随机拼一个到末尾(emoji 不计汉字,不影响区间约束)
    text = await _append_emoji(text, persona)
    return text


async def generate_sample(persona: dict, scene: str) -> str:
    """试一句预览。strict_len=False:不足下限也尽量返回,避免预览空白。"""
    return await generate_reply(persona, scene, [], None, topic_min=8, topic_max=25, strict_len=False)


def render_system_prompt_preview(persona: dict, scene: str, lo: int, hi: int, target_n: int) -> str:
    """供调试/前端预览用:固定参数生成一个 prompt 样本(不引入随机情绪/漂移)。"""
    return _build_system_prompt(persona, scene, lo, hi, target_n, mood=None, allow_drift=False)


def pick_filler(persona: dict) -> str:
    """附和短句：人设口头禅加权 ×2,减少通用池占比。"""
    pool = list(FILLER_POOL)
    cps = persona.get("catchphrases") or []
    pool.extend(cps * 2)
    return random.choice(pool)


# ---------- 话题场景自动生成(创建话题时 topic_prompt 留空则调) ----------
_TOPIC_PROMPT_SYSTEM = (
    "你是 Telegram 群聊场景设计师。根据用户给的话题关键词,写一段 150-250 字的中文群聊场景描述,"
    "用于指导 5 个 AI 小号扮演真人在群里聊天。"
    "要求:\n"
    "1. 先说这群人是谁(车友/同行/邻居/玩家/同学...)、为什么聚在一起;\n"
    "2. 列 3-4 个具体子话题方向,要落到型号/事件/细节,不要抽象概念;\n"
    "3. 制造立场分歧(有人支持/有人观望/有人对立),让对话有张力;\n"
    "4. 口语化,像运营内部剧本说明,不是营销文案;\n"
    "5. 不要每句都喊话题主词,真人聊天不会这样;\n"
    "6. 直接输出场景描述本身,不要前缀、不要标题、不要分点编号、不要引号。"
)


async def generate_topic_prompt(topic_name: str) -> str:
    """根据话题名生成完整的 topic_prompt(场景描述)。

    失败抛 RuntimeError,由 service 层包成 TopicError 让前端看到具体原因。
    """
    if not topic_name or not topic_name.strip():
        raise RuntimeError("话题名为空,无法生成场景")
    messages = [
        {"role": "system", "content": _TOPIC_PROMPT_SYSTEM},
        {"role": "user", "content": f"话题关键词:{topic_name.strip()}"},
    ]
    try:
        text = await _chat_completion(messages)
    except Exception as exc:
        logger.error("生成话题场景失败 topic_name=%s err=%s", topic_name, exc)
        raise RuntimeError(f"LLM 调用失败: {exc}") from exc
    text = text.strip().strip('"').strip("「」")
    if not text:
        raise RuntimeError("LLM 返回空内容")
    return text
