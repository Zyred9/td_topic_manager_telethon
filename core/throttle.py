"""进程内节流(替代 Java 的 Redis 方案),全部按 phone 全局收口。

单进程多 client 共享一个 event loop,asyncio 单线程协作,普通 dict 即可,
无需分布式锁。三个维度:
- 同号最小发言间隔(默认 1.5s)
- 同号每分钟全网发言上限(默认 8 条,跨话题/功能累计)
- 应答真人抢锁(默认 2s,同号多话题同秒命中只放一个)
"""

from __future__ import annotations

import asyncio
import time

from config.settings import get_settings


class Throttle:
    def __init__(self) -> None:
        self._last_send_at: dict[str, float] = {}
        # phone -> (window_start_epoch_sec, count)
        self._minute: dict[str, tuple[float, int]] = {}
        self._reply_lock: dict[str, float] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}

    def send_lock(self, phone: str) -> asyncio.Lock:
        """同一小号的完整发送流程串行执行。"""
        lock = self._send_locks.get(phone)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[phone] = lock
        return lock

    # ---------- 最小间隔 ----------
    def can_send_now(self, phone: str) -> bool:
        """距上次发言是否已超过最小间隔。"""
        min_interval = get_settings().risk.min_interval_ms / 1000.0
        last = self._last_send_at.get(phone)
        if last is None:
            return True
        return (time.monotonic() - last) >= min_interval

    def seconds_until_can_send(self, phone: str) -> float:
        min_interval = get_settings().risk.min_interval_ms / 1000.0
        last = self._last_send_at.get(phone)
        if last is None:
            return 0.0
        remain = min_interval - (time.monotonic() - last)
        return max(0.0, remain)

    def mark_sent(self, phone: str) -> None:
        self._last_send_at[phone] = time.monotonic()

    # ---------- 每分钟配额 ----------
    def minute_quota_available(self, phone: str) -> bool:
        """当前分钟窗口内是否还有配额(不自增,仅查)。"""
        limit = get_settings().risk.max_per_min_per_phone
        now = time.time()
        start, count = self._minute.get(phone, (now, 0))
        if now - start >= 60:
            return True  # 窗口已过期,等价重置
        return count < limit

    def incr_minute(self, phone: str) -> None:
        """记一次发言到当前分钟窗口。"""
        now = time.time()
        start, count = self._minute.get(phone, (now, 0))
        if now - start >= 60:
            self._minute[phone] = (now, 1)
        else:
            self._minute[phone] = (start, count + 1)

    # ---------- 应答真人抢锁 ----------
    def try_acquire_reply_lock(self, phone: str) -> bool:
        """抢到返回 True;TTL 内重复抢返回 False。"""
        ttl = get_settings().risk.reply_lock_ttl_sec
        now = time.monotonic()
        last = self._reply_lock.get(phone)
        if last is not None and (now - last) < ttl:
            return False
        self._reply_lock[phone] = now
        return True

    # ---------- 清理(删除小号时) ----------
    def clear_phone(self, phone: str) -> None:
        self._last_send_at.pop(phone, None)
        self._minute.pop(phone, None)
        self._reply_lock.pop(phone, None)


# 全局单例
throttle = Throttle()
