"""时间展示工具:把 DB 里 UTC 朴素 datetime 转成东八区字符串供前端。

DB 里所有 DATETIME 列存的都是 UTC 时间(Telethon event.date 去 tzinfo 后落库,
机器在 vultr 也是 UTC 时区,所以 datetime.now() 写入的也是 UTC 数值)。
展示给前端时统一加 8 小时再格式化成 "YYYY-MM-DD HH:MM:SS"。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

# 与历史 strftime 输出格式保持一致,前端不需要改
_FMT = "%Y-%m-%d %H:%M:%S"
# 东八区相对 UTC 的偏移(小时)
_CN_OFFSET = timedelta(hours=8)


def to_cn_str(dt: Optional[datetime]) -> Optional[str]:
    """把 DB 取出的 UTC 朴素 datetime 加 8h 转东八区,再 strftime 成字符串。

    None / 缺失返回 None,与原 ``.strftime if r.get(...) else None`` 行为对齐。
    """
    if dt is None:
        return None
    # 带 tzinfo 的也兼容:先转 UTC 朴素再加 8h,行为统一
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.replace(tzinfo=None)
    return (dt + _CN_OFFSET).strftime(_FMT)
