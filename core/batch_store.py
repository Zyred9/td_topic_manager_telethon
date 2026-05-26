"""进程内批次结果存储(替代 Java 的 Redis 方案)。

服务加群/退群/协议号导入这类短时异步任务的进度与明细。
输出严格匹配前端 BatchResult 形状(web/src/types/common.ts):

  {
    meta: {opType, total, success, failed,
           status: 'running'|'finished'|'interrupted', createAt, finishAt?},
    phones: {[phone]: {status:int, finishAt?, failReason?, chatId?, tdataPath?, note?}}
  }

createAt/finishAt 为毫秒时间戳(int)。重启即清空(内存),惰性 TTL 清理过期批次。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

from config.constants import BatchStatus

# 单号明细状态(前端 BatchPhoneItem.status)
ITEM_PENDING = 0
ITEM_SUCCESS = 1
ITEM_FAILED = 2


def _now_ms() -> int:
    return int(time.time() * 1000)


class BatchStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, op_type: str, total: int, ttl_sec: int = 3600) -> str:
        batch_id = uuid.uuid4().hex
        with self._lock:
            self._data[batch_id] = {
                "meta": {
                    "opType": op_type,
                    "total": total,
                    "success": 0,
                    "failed": 0,
                    "status": BatchStatus.RUNNING,
                    "createAt": _now_ms(),
                    "finishAt": None,
                },
                "phones": {},
                "_ttl": ttl_sec,
            }
        return batch_id

    def set_item(
        self,
        batch_id: str,
        phone: str,
        status: int,
        fail_reason: Optional[str] = None,
        chat_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> None:
        with self._lock:
            batch = self._data.get(batch_id)
            if batch is None:
                return
            item: dict[str, Any] = {"status": status, "finishAt": _now_ms()}
            if fail_reason is not None:
                item["failReason"] = fail_reason
            if chat_id is not None:
                item["chatId"] = chat_id
            if note is not None:
                item["note"] = note

            phones = batch["phones"]
            prev = phones.get(phone)
            phones[phone] = item

            # 维护 meta 计数(覆盖时先回退旧计数)
            meta = batch["meta"]
            if prev is not None:
                if prev["status"] == ITEM_SUCCESS:
                    meta["success"] -= 1
                elif prev["status"] == ITEM_FAILED:
                    meta["failed"] -= 1
            if status == ITEM_SUCCESS:
                meta["success"] += 1
            elif status == ITEM_FAILED:
                meta["failed"] += 1

    def finish(self, batch_id: str) -> None:
        with self._lock:
            batch = self._data.get(batch_id)
            if batch is None:
                return
            batch["meta"]["status"] = BatchStatus.FINISHED
            batch["meta"]["finishAt"] = _now_ms()

    def get(self, batch_id: str) -> Optional[dict[str, Any]]:
        """返回前端 BatchResult 形状(剔除内部 _ttl 字段)。"""
        self._evict_expired()
        with self._lock:
            batch = self._data.get(batch_id)
            if batch is None:
                return None
            return {"meta": dict(batch["meta"]), "phones": {k: dict(v) for k, v in batch["phones"].items()}}

    def _evict_expired(self) -> None:
        now_ms = _now_ms()
        with self._lock:
            expired = [
                bid for bid, b in self._data.items()
                if now_ms - b["meta"]["createAt"] > b["_ttl"] * 1000
            ]
            for bid in expired:
                self._data.pop(bid, None)


# 全局单例
batch_store = BatchStore()
