from __future__ import annotations

import shlex
from typing import Any

from .config_utils import config_get


def private_user_id(value: Any) -> str:
    """Normalize a plain private user ID or extract it from a private UMO."""
    text = str(value or "").strip()
    marker = ":FriendMessage:"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    return text


def account_owner_user_id(account: dict[str, Any]) -> str:
    owner_id = private_user_id(account.get("owner_user_id"))
    if owner_id:
        return owner_id
    return private_user_id(account.get("owner_umo"))


def account_target_platform(account: dict[str, Any]) -> str:
    configured = str(account.get("target_platform") or "").strip()
    if configured:
        return configured
    legacy_umo = str(account.get("owner_umo") or "").strip()
    if ":FriendMessage:" in legacy_umo:
        return legacy_umo.split(":", 1)[0].strip()
    return "aiocqhttp"


def enabled_accounts(config: dict[str, Any]) -> list[dict[str, Any]]:
    accounts = config_get(config, "mail_accounts", [])
    return [item for item in accounts if isinstance(item, dict) and item.get("enabled", True)]


def is_admin(sender_id: str, config: dict[str, Any]) -> bool:
    admins = config_get(config, "admin_uids", [])
    return str(sender_id or "").strip() in {str(item).strip() for item in admins if str(item).strip()}


def visible_accounts(
    config: dict[str, Any], *, umo: str, sender_id: str
) -> list[dict[str, Any]]:
    accounts = enabled_accounts(config)
    if is_admin(sender_id, config):
        return accounts
    sender_id = private_user_id(sender_id)
    visible: list[dict[str, Any]] = []
    for item in accounts:
        owner_id = account_owner_user_id(item)
        if owner_id == sender_id or (
            not owner_id
            and str(item.get("owner_umo") or "").strip() == str(umo or "").strip()
        ):
            visible.append(item)
    return visible


def resolve_account(accounts: list[dict[str, Any]], selector: str) -> tuple[dict[str, Any] | None, str]:
    selector = str(selector or "").strip()
    if not selector:
        if len(accounts) == 1:
            return accounts[0], ""
        return None, "请指定邮箱账户。"
    id_matches = [item for item in accounts if str(item.get("account_id") or "").strip() == selector]
    if len(id_matches) == 1:
        return id_matches[0], ""
    if len(id_matches) > 1:
        return None, "存在重复的 account_id，请先修正插件配置。"
    name_matches = [item for item in accounts if str(item.get("name") or "").strip() == selector]
    if len(name_matches) == 1:
        return name_matches[0], ""
    if len(name_matches) > 1:
        return None, "存在同名账户，请改用唯一的 account_id。"
    return None, f'未找到邮箱账户“{selector}”。'


def command_payload(message: str, subcommand: str) -> str:
    tokens = shlex.split(str(message or ""))
    wanted = subcommand.lower()
    for index, token in enumerate(tokens):
        if token.lower() == wanted:
            return " ".join(tokens[index + 1 :]).strip()
    return ""


def parse_send_payload(payload: str) -> tuple[str, str, str, str]:
    head, separator, body = payload.partition("|")
    if not separator:
        raise ValueError("主题与正文之间缺少 | 分隔符。")
    parts = shlex.split(head)
    if len(parts) < 3:
        raise ValueError("需要账户、收件人和主题。")
    account, recipient = parts[0], parts[1]
    subject = " ".join(parts[2:]).strip()
    if not subject or not body.strip():
        raise ValueError("主题和正文不能为空。")
    return account, recipient, subject, body.strip()


def parse_reply_payload(payload: str) -> tuple[str, int, str]:
    parts = payload.split(maxsplit=2)
    if len(parts) < 3:
        raise ValueError("需要账户、邮件 UID 和回复正文。")
    try:
        uid = int(parts[1])
    except ValueError as exc:
        raise ValueError("邮件 UID 必须是正整数。") from exc
    if uid <= 0 or not parts[2].strip():
        raise ValueError("邮件 UID 和回复正文无效。")
    return parts[0], uid, parts[2].strip()
