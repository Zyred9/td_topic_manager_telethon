"""小号实体(t_account)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Account:
    id: Optional[int]
    phone: str
    tg_user_id: Optional[int]
    tg_first_name: Optional[str]
    tg_last_name: Optional[str]
    tg_username: Optional[str]
    bio: Optional[str]
    avatar_path: Optional[str]
    import_type: int           # 1手机号 2协议号
    session_path: str
    api_id: Optional[int]
    api_hash: Optional[str]
    device_model: Optional[str]
    app_version: Optional[str]
    lang_pack: Optional[str]
    two_fa: Optional[str]
    status: int
    login_time: Optional[datetime]
    persona_json: Optional[str]
    create_time: Optional[datetime]
    update_time: Optional[datetime]

    @classmethod
    def from_row(cls, row: dict) -> "Account":
        return cls(
            id=row.get("id"),
            phone=row["phone"],
            tg_user_id=row.get("tg_user_id"),
            tg_first_name=row.get("tg_first_name"),
            tg_last_name=row.get("tg_last_name"),
            tg_username=row.get("tg_username"),
            bio=row.get("bio"),
            avatar_path=row.get("avatar_path"),
            import_type=int(row["import_type"]),
            session_path=row["session_path"],
            api_id=row.get("api_id"),
            api_hash=row.get("api_hash"),
            device_model=row.get("device_model"),
            app_version=row.get("app_version"),
            lang_pack=row.get("lang_pack"),
            two_fa=row.get("two_fa"),
            status=int(row["status"]),
            login_time=row.get("login_time"),
            persona_json=row.get("persona_json"),
            create_time=row.get("create_time"),
            update_time=row.get("update_time"),
        )
