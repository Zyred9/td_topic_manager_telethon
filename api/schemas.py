"""请求体 Pydantic 模型。

字段名与前端 api/*.ts 完全一致(camelCase)。响应统一走 deps.result 包装,
不在此定义响应模型,避免与前端 VO 双重维护。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# ========== auth ==========
class LoginReq(BaseModel):
    username: str
    password: str


class PasswordChangeReq(BaseModel):
    oldPwd: str
    newPwd: str


# ========== admin ==========
class AdminCreateReq(BaseModel):
    username: str
    password: str
    role: int


class AdminUpdateReq(BaseModel):
    role: Optional[int] = None
    enabled: Optional[int] = None


class AdminResetPwdReq(BaseModel):
    newPwd: str


# ========== account ==========
class PhoneLoginReq(BaseModel):
    phone: str
    email: Optional[str] = None
    password: Optional[str] = None


class CodeReq(BaseModel):
    phone: str
    code: str


class ProfileReq(BaseModel):
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    username: Optional[str] = None
    bio: Optional[str] = None


# ========== group-op ==========
class GroupOpReq(BaseModel):
    phones: List[str]
    groupLink: Optional[str] = None
    chatId: Optional[int] = None


# ========== schedule ==========
class ScheduleStartReq(BaseModel):
    chatIds: List[int]
    content: str
    intervalMin: int


# ========== keyword ==========
class KeywordReq(BaseModel):
    keyword: str
    replyText: str


# ========== topic ==========
class TopicCreateReq(BaseModel):
    topicName: str
    topicPrompt: str
    phones: List[str]
    chatId: Optional[int] = None
    speakMinSec: Optional[int] = None
    speakMaxSec: Optional[int] = None
    replyUserProb: Optional[int] = None
    maxPerMin: Optional[int] = None
    replyMinChars: Optional[int] = None
    replyMaxChars: Optional[int] = None
    fillerEnabled: Optional[int] = None
    fillerProb: Optional[int] = None


class TopicUpdateReq(TopicCreateReq):
    version: int = Field(...)
    # 更新时 topicName/topicPrompt/phones 可选
    topicName: Optional[str] = None  # type: ignore[assignment]
    topicPrompt: Optional[str] = None  # type: ignore[assignment]
    phones: Optional[List[str]] = None  # type: ignore[assignment]


class TransferOwnerReq(BaseModel):
    newOwnerPhone: str
    password: str


# ========== persona ==========
class PersonaModel(BaseModel):
    name: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    tone: Optional[List[str]] = None
    interests: Optional[List[str]] = None
    catchphrases: Optional[List[str]] = None
    style: Optional[str] = None
    emojiLevel: Optional[str] = None
    replyLenRange: Optional[List[int]] = None
    extraPrompt: Optional[str] = None


class PersonaPresetReq(BaseModel):
    presetName: str
    preset: PersonaModel


class PersonaPreviewReq(BaseModel):
    topicPrompt: Optional[str] = None


class PersonaConfigReq(BaseModel):
    """性格/兴趣/emoji 标签库增改,value 为标签文本或 emoji 字符。"""
    value: str
