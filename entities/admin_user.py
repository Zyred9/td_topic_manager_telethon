"""后台账号实体(t_admin_user)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class AdminUser:
    id: Optional[int]
    username: str
    password: str  # bcrypt 哈希
    role: int      # 1超级管理员 2普通运营
    enabled: int
    last_login: Optional[datetime]
    create_time: Optional[datetime]
    update_time: Optional[datetime]

    @classmethod
    def from_row(cls, row: dict) -> "AdminUser":
        return cls(
            id=row.get("id"),
            username=row["username"],
            password=row["password"],
            role=int(row["role"]),
            enabled=int(row["enabled"]),
            last_login=row.get("last_login"),
            create_time=row.get("create_time"),
            update_time=row.get("update_time"),
        )
