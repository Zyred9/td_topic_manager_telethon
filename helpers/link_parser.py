"""群链接解析:支持 3 种格式,分流到不同的加群方式。

| 格式 | 示例 | 处理 |
|---|---|---|
| 公开用户名 | https://t.me/xxx / @xxx | join_public(username) |
| 旧邀请链接 | https://t.me/joinchat/HASH | join_invite(hash) |
| 新邀请链接 | https://t.me/+HASH | join_invite(hash) |
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedLink:
    kind: str          # "public" | "invite"
    value: str         # username(不带@) 或 invite hash


class LinkParseError(Exception):
    pass


def parse(link: str) -> ParsedLink:
    raw = (link or "").strip()
    if not raw:
        raise LinkParseError("群链接为空")

    # @username 形式
    if raw.startswith("@"):
        return ParsedLink("public", raw[1:])

    # 去掉协议头与域名
    body = raw
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/",
                   "http://telegram.me/", "t.me/", "telegram.me/"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break

    body = body.strip("/")

    # 新邀请链接 https://t.me/+HASH
    if body.startswith("+"):
        return ParsedLink("invite", body[1:])

    # 旧邀请链接 joinchat/HASH
    if body.startswith("joinchat/"):
        return ParsedLink("invite", body[len("joinchat/"):])

    # 其余视为公开用户名(可能带 query,如 ?startgroup)
    username = body.split("?", 1)[0].split("/", 1)[0]
    if not username:
        raise LinkParseError(f"无法解析群链接: {link}")
    return ParsedLink("public", username)
