"""业务枚举与常量。"""

from __future__ import annotations

from enum import IntEnum


class AccountStatus(IntEnum):
    """小号登录状态(对应 t_account.status 与前端 statusDesc)。"""

    NOT_LOGGED_IN = 0
    INITIALIZING = 1
    WAIT_CODE = 2
    LOGGED_IN = 3
    OFFLINE = 4
    NEED_RELOGIN = 5
    WAIT_2FA = 6

    @property
    def desc(self) -> str:
        return _ACCOUNT_STATUS_DESC[self]


_ACCOUNT_STATUS_DESC = {
    AccountStatus.NOT_LOGGED_IN: "未登录",
    AccountStatus.INITIALIZING: "初始化中",
    AccountStatus.WAIT_CODE: "等待验证码",
    AccountStatus.LOGGED_IN: "已登录",
    AccountStatus.OFFLINE: "离线",
    AccountStatus.NEED_RELOGIN: "需重新登录",
    AccountStatus.WAIT_2FA: "等待 2FA 密码",
}


def account_status_desc(status: int) -> str:
    try:
        return AccountStatus(status).desc
    except ValueError:
        return "未知"


class ImportType(IntEnum):
    """小号来源。"""

    PHONE = 1
    PROTOCOL = 2


class AdminRole(IntEnum):
    """后台账号角色。"""

    SUPER_ADMIN = 1
    OPERATOR = 2

    @property
    def desc(self) -> str:
        return "超级管理员" if self is AdminRole.SUPER_ADMIN else "普通运营"


def role_desc(role: int) -> str:
    try:
        return AdminRole(role).desc
    except ValueError:
        return "未知"


class TaskStatus(IntEnum):
    """定时任务 / 话题状态。"""

    STOPPED = 0
    RUNNING = 1


class SendSource(IntEnum):
    """发送来源,决定配额耗尽时的丢弃/排队策略。

    优先级(数字越小越高):关键字 > 应答真人 > 自驱 > 定时。
    关键字/应答真人即时性优先,配额满直接丢弃;自驱/定时下一周期补偿。
    """

    KEYWORD = 1
    AI_REPLY = 2
    AI_SELF = 3
    SCHEDULE = 4


class BatchStatus:
    """批次任务状态(前端 BatchMeta.status 字面量)。"""

    RUNNING = "running"
    FINISHED = "finished"
    INTERRUPTED = "interrupted"


# AI 回复汉字硬上限(系统级,运营不可调)
MAX_HAN_CHARS = 50

# 附和短句通用池(自驱场景按概率抽)
FILLER_POOL = ["嗯", "哈哈", "对", "草", "牛", "可以", "啊这", "笑死", "好家伙"]

# AI 互聊上下文窗口
CONTEXT_WINDOW = 10

# 内置 5 个人设预置模板(id 以 builtin- 前缀,不可删)。
# preset 字段对齐前端 Persona(camelCase)。
BUILTIN_PRESETS = [
    {
        "id": "builtin-humor-boy",
        "presetName": "幽默大男孩",
        "builtin": True,
        "preset": {
            "name": "", "gender": "male", "age": 25,
            "tone": ["幽默", "活泼"], "interests": ["游戏", "数码"],
            "catchphrases": ["哈哈哈", "兄弟"], "style": "short",
            "emojiLevel": "sometimes", "replyLenRange": None, "extraPrompt": "",
        },
    },
    {
        "id": "builtin-gentle-girl",
        "presetName": "温柔小姐姐",
        "builtin": True,
        "preset": {
            "name": "", "gender": "female", "age": 24,
            "tone": ["温柔", "活泼"], "interests": ["美食", "影视"],
            "catchphrases": ["啊这", "笑死"], "style": "mixed",
            "emojiLevel": "often", "replyLenRange": None, "extraPrompt": "",
        },
    },
    {
        "id": "builtin-steady-uncle",
        "presetName": "沉稳大叔",
        "builtin": True,
        "preset": {
            "name": "", "gender": "male", "age": 38,
            "tone": ["稳重", "抬杠"], "interests": ["股票", "职场"],
            "catchphrases": ["其实吧", "说真的"], "style": "long",
            "emojiLevel": "never", "replyLenRange": None, "extraPrompt": "",
        },
    },
    {
        "id": "builtin-shy-student",
        "presetName": "社恐学生",
        "builtin": True,
        "preset": {
            "name": "", "gender": "male", "age": 19,
            "tone": ["社恐", "佛系"], "interests": ["二次元", "游戏"],
            "catchphrases": ["嗯", "好"], "style": "short",
            "emojiLevel": "sometimes", "replyLenRange": None, "extraPrompt": "",
        },
    },
    {
        "id": "builtin-gossip-girl",
        "presetName": "八卦少女",
        "builtin": True,
        "preset": {
            "name": "", "gender": "female", "age": 22,
            "tone": ["活泼", "毒舌"], "interests": ["八卦", "穿搭"],
            "catchphrases": ["卧槽", "真的假的"], "style": "mixed",
            "emojiLevel": "often", "replyLenRange": None, "extraPrompt": "",
        },
    },
]
