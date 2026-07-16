from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import asdict
from datetime import datetime
from email.utils import parseaddr
from typing import Any

from astrbot.api import logger

from .imap_client import (
    MailFolder,
    MailNotFoundError,
    create_folder as imap_create_folder,
    transfer_message,
)
from .mail_index import IndexedMailHeader, MailDraft, mail_content_hash
from .smtp_client import build_draft_message

try:  # AstrBot's current Plugin Pages request facade.
    from astrbot.api.web import request
except ImportError:  # Compatibility with AstrBot 4.26 deployments and tests.
    try:
        from quart import request
    except ImportError:  # pragma: no cover - replaced by a request stub in tests.
        request = None


PLUGIN_NAME = "astrbot_plugin_email_assistant"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}"
_DRAFT_STATUSES = {
    "editing", "pending_review", "approved", "sending",
    "sent", "failed", "cancelled",
}


def _one_line(value: Any, limit: int = 180) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class EmailAssistantPageApi:
    """Authenticated AstrBot Plugin Page API for the mailbox dashboard."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._draft_send_locks = plugin._draft_service.locks
        self._ai_locks: dict[str, asyncio.Lock] = {}

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/overview", self.get_overview, ["GET"], "Email Assistant overview"),
            ("/accounts", self.list_accounts, ["GET"], "Email Assistant accounts"),
            ("/messages", self.list_messages, ["GET"], "Email Assistant messages"),
            ("/message", self.get_message, ["GET"], "Email Assistant message detail"),
            ("/message/cached", self.get_cached_message, ["GET"], "Email Assistant cached message detail"),
            ("/message/verify", self.verify_message, ["POST"], "Email Assistant verify message detail"),
            ("/folders", self.list_folders, ["GET"], "Email Assistant folders"),
            ("/folders/refresh", self.refresh_folders, ["POST"], "Email Assistant refresh folders"),
            ("/folders/create", self.create_folder, ["POST"], "Email Assistant create folder"),
            ("/messages/copy", self.copy_message, ["POST"], "Email Assistant copy message"),
            ("/messages/move", self.move_message, ["POST"], "Email Assistant move message"),
            ("/message/summary", self.summarize_message, ["POST"], "Email Assistant summarize message"),
            ("/message/translate", self.translate_message, ["POST"], "Email Assistant translate message"),
            ("/sync", self.sync_accounts, ["POST"], "Email Assistant index sync"),
            ("/drafts", self.list_drafts, ["GET"], "Email Assistant drafts"),
            ("/draft", self.get_draft, ["GET"], "Email Assistant draft detail"),
            ("/drafts/create", self.create_draft, ["POST"], "Email Assistant create draft"),
            ("/drafts/update", self.update_draft, ["POST"], "Email Assistant update draft"),
            ("/drafts/approve", self.approve_draft, ["POST"], "Email Assistant approve draft"),
            ("/drafts/delete", self.delete_draft, ["POST"], "Email Assistant delete draft"),
            ("/drafts/send", self.send_draft, ["POST"], "Email Assistant send draft"),
        ]
        for path, handler, methods, description in routes:
            register(f"{PAGE_API_PREFIX}{path}", handler, methods, description)

    @staticmethod
    def _ok(data: Any = None) -> dict[str, Any]:
        return {"status": "ok", "data": data, "ts": int(time.time())}

    @staticmethod
    def _error(message: Any) -> dict[str, Any]:
        if isinstance(message, BaseException) and message.args:
            message = message.args[0]
        return {
            "status": "error",
            "message": _one_line(message, 300) or "请求失败。",
            "ts": int(time.time()),
        }

    @staticmethod
    def _query() -> Any:
        if request is None:
            return {}
        query = getattr(request, "query", None)
        return query if query is not None else getattr(request, "args", {})

    @staticmethod
    async def _json_payload() -> dict[str, Any]:
        if request is None:
            return {}
        json_method = getattr(request, "json", None)
        if callable(json_method):
            try:
                payload = await json_method(default={})
            except TypeError:
                payload = await json_method()
        else:
            get_json = getattr(request, "get_json", None)
            payload = await get_json(silent=True) if callable(get_json) else {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _require_index(self):
        index = self.plugin._mail_index
        if index is None:
            raise RuntimeError("本地邮件索引未启用或初始化失败，邮件中心暂不可用。")
        return index

    def _resolve_account(
        self, account_id: Any, *, capability: str | None = None
    ) -> dict[str, Any]:
        wanted = _one_line(account_id, 80)
        if not wanted:
            raise ValueError("缺少账户 ID。")
        matches = [
            item
            for item in self.plugin._accounts()
            if self.plugin._account_key(item) == wanted
        ]
        if len(matches) != 1:
            raise KeyError(f"未找到唯一邮箱账户：{wanted}")
        account = matches[0]
        validation_error = self.plugin._validate_account(account)
        if validation_error:
            raise ValueError(validation_error)
        if not account.get("enabled", True):
            raise PermissionError("该邮箱账户已关闭。")
        capability_default = False if capability == "organize_enabled" else True
        if capability and not account.get(capability, capability_default):
            labels = {
                "query_enabled": "查询",
                "send_enabled": "发送",
                "receive_enabled": "接收",
                "organize_enabled": "整理",
            }
            raise PermissionError(f"该邮箱的{labels.get(capability, capability)}功能已关闭。")
        return account

    def _account_payload(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = self.plugin._account_key(account)
        folder = self.plugin._folder(account)
        status = self.plugin._status.get(account_id, {})
        payload: dict[str, Any] = {
            "account_id": account_id,
            "name": self.plugin._display_name(account),
            "email": _one_line(account.get("email"), 160),
            "folder": folder,
            "enabled": bool(account.get("enabled", True)),
            "receive_enabled": bool(account.get("receive_enabled", True)),
            "query_enabled": bool(account.get("query_enabled", True)),
            "send_enabled": bool(account.get("send_enabled", True)),
            "organize_enabled": bool(account.get("organize_enabled", False)),
            "runtime_status": {
                "ok": status.get("ok"),
                "detail": _one_line(status.get("detail"), 180),
                "checked_at": _one_line(status.get("checked_at"), 32),
            },
            "sync_warning": _one_line(
                self.plugin._index_warnings.get(account_id, ""), 180
            ),
        }
        index = self.plugin._mail_index
        if index is not None:
            payload["index"] = index.stats(account_id, folder)
        else:
            payload["index"] = None
        return payload

    def _enabled_accounts(self) -> list[dict[str, Any]]:
        return [item for item in self.plugin._accounts() if item.get("enabled", True)]

    async def get_overview(self) -> dict[str, Any]:
        try:
            accounts = await asyncio.gather(
                *[
                    asyncio.to_thread(self._account_payload, account)
                    for account in self._enabled_accounts()
                ]
            )
            return self._ok(
                {
                    "plugin": {
                        "version": "2.0.0",
                        "index_enabled": self.plugin._mail_index is not None,
                        "body_cache_mode": str(
                            self.plugin.config.get("body_cache_mode") or "on_demand"
                        ),
                    },
                    "accounts": accounts,
                }
            )
        except Exception as exc:
            logger.warning("[EmailAssistantPage] 获取总览失败: %s", _one_line(exc))
            return self._error(exc)

    async def list_accounts(self) -> dict[str, Any]:
        return await self.get_overview()

    @staticmethod
    def _encode_cursor(item: IndexedMailHeader) -> str:
        raw = json.dumps([item.date_ts, item.uid], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(value: Any) -> tuple[float, int] | None:
        text = str(value or "").strip()
        if not text:
            return None
        if len(text) > 256:
            raise ValueError("分页游标无效。")
        try:
            raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            decoded = json.loads(raw.decode())
            if not isinstance(decoded, list) or len(decoded) != 2:
                raise ValueError
            return float(decoded[0]), int(decoded[1])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("分页游标无效。") from exc

    @staticmethod
    def _header_payload(item: IndexedMailHeader) -> dict[str, Any]:
        return asdict(item)

    async def _refresh_account_folders(self, account: dict[str, Any]):
        index = self._require_index()
        await self.plugin._refresh_folder_catalog(account, force=True)
        return await asyncio.to_thread(
            index.list_folders, self.plugin._account_key(account)
        )

    async def _folder(self, account: dict[str, Any], value: Any) -> str:
        wanted = _one_line(value, 512) or self.plugin._folder(account)
        index = self._require_index()
        folders = await asyncio.to_thread(
            index.list_folders, self.plugin._account_key(account)
        )
        if not folders and wanted == self.plugin._folder(account):
            return wanted
        if not folders:
            folders = await self._refresh_account_folders(account)
        match = next(
            (item for item in folders if item.name == wanted and item.selectable), None
        )
        if match is None and wanted == self.plugin._folder(account):
            return wanted
        if match is None:
            raise ValueError("文件夹不存在、不可选择，或本地文件夹清单尚未刷新。")
        return match.name

    @staticmethod
    def _folder_payload(folder, stats=None) -> dict[str, Any]:
        payload = asdict(folder)
        payload["stats"] = stats
        return payload

    async def list_folders(self) -> dict[str, Any]:
        try:
            account = self._resolve_account(
                self._query().get("account_id"), capability="query_enabled"
            )
            index = self._require_index()
            folders = await asyncio.to_thread(
                index.list_folders, self.plugin._account_key(account)
            )
            warning = ""
            if not folders:
                try:
                    folders = await self._refresh_account_folders(account)
                except Exception as exc:
                    warning = f"文件夹清单刷新失败，暂时只显示主文件夹：{_one_line(exc, 160)}"
                    primary = self.plugin._folder(account)
                    folders = [
                        MailFolder(
                            primary,
                            primary,
                            "/",
                            (),
                            True,
                            "inbox" if primary.upper() == "INBOX" else "",
                        )
                    ]
            items = []
            for folder in folders:
                stats = await asyncio.to_thread(
                    index.stats, self.plugin._account_key(account), folder.name
                )
                items.append(self._folder_payload(folder, stats))
            return self._ok(
                {
                    "items": items,
                    "primary_folder": self.plugin._folder(account),
                    "warning": warning,
                }
            )
        except Exception as exc:
            return self._error(exc)

    async def refresh_folders(self) -> dict[str, Any]:
        try:
            payload = await self._json_payload()
            account = self._resolve_account(
                payload.get("account_id"), capability="query_enabled"
            )
            folders = await self._refresh_account_folders(account)
            return self._ok({"items": [self._folder_payload(item) for item in folders]})
        except Exception as exc:
            return self._error(exc)

    async def create_folder(self) -> dict[str, Any]:
        try:
            payload = await self._json_payload()
            account = self._resolve_account(
                payload.get("account_id"), capability="organize_enabled"
            )
            name = str(payload.get("name") or "").strip()
            await asyncio.to_thread(
                imap_create_folder, account, name, self.plugin._timeout()
            )
            await self._refresh_account_folders(account)
            self.plugin._log_mail_operation("web_create_folder", account, detail=name)
            return self._ok({"name": name})
        except Exception as exc:
            return self._error(exc)

    async def list_messages(self) -> dict[str, Any]:
        try:
            query = self._query()
            account = self._resolve_account(
                query.get("account_id"), capability="query_enabled"
            )
            index = self._require_index()
            folder = await self._folder(account, query.get("folder"))
            state = await asyncio.to_thread(
                index.get_state, self.plugin._account_key(account), folder
            )
            if state is None:
                await self.plugin._sync_account_index(account, folder=folder)
            limit = self._int(query.get("limit"), 50, 1, 100)
            keyword = _one_line(query.get("q"), 120)
            since_text = _one_line(query.get("since"), 20)
            since_ts = None
            if since_text:
                try:
                    since_ts = datetime.strptime(since_text, "%Y-%m-%d").timestamp()
                except ValueError as exc:
                    raise ValueError("开始日期格式应为 YYYY-MM-DD。") from exc
            cursor = self._decode_cursor(query.get("cursor"))
            items, has_more = await asyncio.to_thread(
                index.list_headers_page,
                self.plugin._account_key(account),
                folder,
                limit=limit,
                keyword=keyword,
                since_ts=since_ts,
                before_date_ts=cursor[0] if cursor else None,
                before_uid=cursor[1] if cursor else None,
            )
            next_cursor = self._encode_cursor(items[-1]) if has_more and items else ""
            stats = await asyncio.to_thread(
                index.stats, self.plugin._account_key(account), folder
            )
            return self._ok(
                {
                    "items": [self._header_payload(item) for item in items],
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                    "limit": limit,
                    "folder": folder,
                    "index": stats,
                    "sync_warning": _one_line(
                        self.plugin._index_warnings.get(
                            self.plugin._account_key(account), ""
                        ),
                        180,
                    ),
                }
            )
        except Exception as exc:
            return self._error(exc)

    async def get_message(self) -> dict[str, Any]:
        try:
            query = self._query()
            account = self._resolve_account(
                query.get("account_id"), capability="query_enabled"
            )
            uid = self._int(query.get("uid"), 0, 0, 2_147_483_647)
            if uid <= 0:
                raise ValueError("邮件 UID 必须是正整数。")
            folder = await self._folder(account, query.get("folder"))
            self.plugin._log_mail_operation("web_show", account, uid=uid)
            mail = await self.plugin._fetch_remote_detail(account, uid, folder)
            body_limit = self._int(
                self.plugin.config.get("detail_body_max_chars"),
                4000,
                200,
                12000,
            )
            body = mail.body_preview(body_limit)
            return self._ok(
                {
                    "account_id": self.plugin._account_key(account),
                    "folder": folder,
                    "uid": mail.uid,
                    "subject": mail.subject,
                    "from_name": mail.from_name,
                    "from_addr": mail.from_addr,
                    "reply_to": mail.reply_to,
                    "date": mail.date,
                    "date_ts": mail.timestamp,
                    "body": body,
                    "body_truncated": len(body) < len(mail.body),
                    "has_attachments": mail.has_attachments,
                    "message_id": mail.message_id,
                }
            )
        except Exception as exc:
            return self._error(exc)

    async def get_cached_message(self) -> dict[str, Any]:
        try:
            query = self._query()
            account = self._resolve_account(
                query.get("account_id"), capability="query_enabled"
            )
            folder = await self._folder(account, query.get("folder"))
            uid = self._int(query.get("uid"), 0, 0, 2_147_483_647)
            if uid <= 0:
                raise ValueError("邮件 UID 必须是正整数。")
            index = self._require_index()
            account_id = self.plugin._account_key(account)
            state = await asyncio.to_thread(index.get_state, account_id, folder)
            if state is None:
                raise KeyError("该文件夹尚无本地索引。")
            header = await asyncio.to_thread(index.get_header, account_id, folder, uid)
            cached = await asyncio.to_thread(
                index.get_cached_body, account_id, folder, state.uidvalidity, uid
            )
            if header is None or cached is None:
                raise KeyError("这封邮件没有可用的本地正文缓存。")
            body_limit = self._int(
                self.plugin.config.get("detail_body_max_chars"),
                4000,
                200,
                12000,
            )
            body = cached.body_text[:body_limit]
            return self._ok(
                {
                    "account_id": account_id,
                    "folder": folder,
                    "uid": header.uid,
                    "subject": header.subject,
                    "from_name": header.from_name,
                    "from_addr": header.from_addr,
                    "reply_to": header.reply_to,
                    "date": header.date_text,
                    "date_ts": header.date_ts,
                    "body": body,
                    "body_truncated": bool(cached.truncated)
                    or len(body) < len(cached.body_text),
                    "has_attachments": header.has_attachments,
                    "message_id": header.message_id,
                    "from_cache": True,
                }
            )
        except Exception as exc:
            return self._error(exc)

    async def verify_message(self) -> dict[str, Any]:
        try:
            payload = await self._json_payload()
            account = self._resolve_account(
                payload.get("account_id"), capability="query_enabled"
            )
            folder = await self._folder(account, payload.get("folder"))
            uid = self._int(payload.get("uid"), 0, 0, 2_147_483_647)
            if uid <= 0:
                raise ValueError("邮件 UID 必须是正整数。")
            index = self._require_index()
            account_id = self.plugin._account_key(account)
            state = await asyncio.to_thread(index.get_state, account_id, folder)
            header = await asyncio.to_thread(index.get_header, account_id, folder, uid)
            cached = (
                await asyncio.to_thread(
                    index.get_cached_body,
                    account_id,
                    folder,
                    state.uidvalidity,
                    uid,
                )
                if state is not None
                else None
            )
            try:
                remote = await self.plugin._fetch_remote_detail(account, uid, folder)
            except MailNotFoundError:
                return self._ok(
                    {
                        "verification_status": "deleted",
                        "message": "邮件已在云端删除或移出当前文件夹。",
                    }
                )
            changed = bool(
                header is not None
                and cached is not None
                and not self._cached_matches_remote(header, cached, remote)
            )
            return self._ok(
                {
                    "verification_status": "changed" if changed else "current",
                    "message": "云端邮件内容已变化。" if changed else "云端校验通过。",
                }
            )
        except Exception as exc:
            return self._error(exc)

    @staticmethod
    def _cached_matches_remote(header, cached, remote) -> bool:
        metadata_matches = (
            header.subject == remote.subject
            and header.from_name == remote.from_name
            and header.from_addr == remote.from_addr
            and header.reply_to == remote.reply_to
            and header.date_text == remote.date
            and header.has_attachments == remote.has_attachments
            and header.message_id == remote.message_id
            and header.references == remote.references
        )
        body_matches = (
            str(remote.body or "").startswith(cached.body_text)
            if cached.truncated
            else cached.body_text == str(remote.body or "")
        )
        return metadata_matches and body_matches

    async def sync_accounts(self) -> dict[str, Any]:
        try:
            self._require_index()
            payload = await self._json_payload()
            selected_id = _one_line(payload.get("account_id"), 80)
            selected_folder = payload.get("folder")
            accounts = (
                [self._resolve_account(selected_id, capability="query_enabled")]
                if selected_id
                else [
                    item
                    for item in self._enabled_accounts()
                    if item.get("query_enabled", True)
                ]
            )
            results = []
            for account in accounts:
                account_id = self.plugin._account_key(account)
                try:
                    folder = await self._folder(account, selected_folder)
                    self.plugin._log_mail_operation("web_sync", account)
                    await self.plugin._sync_account_index(
                        account, force_reconcile=True, folder=folder
                    )
                    stats = await asyncio.to_thread(
                        self.plugin._mail_index.stats,
                        account_id,
                        folder,
                    )
                    results.append(
                        {"account_id": account_id, "folder": folder, "success": True, "index": stats}
                    )
                except Exception as exc:
                    results.append(
                        {
                            "account_id": account_id,
                            "success": False,
                            "error": _one_line(exc, 220),
                        }
                    )
            return self._ok({"results": results})
        except Exception as exc:
            return self._error(exc)

    async def _transfer(self, *, move: bool) -> dict[str, Any]:
        try:
            payload = await self._json_payload()
            account = self._resolve_account(
                payload.get("account_id"), capability="organize_enabled"
            )
            source = await self._folder(account, payload.get("source_folder"))
            target = await self._folder(account, payload.get("target_folder"))
            uid = self._int(payload.get("uid"), 0, 0, 2_147_483_647)
            if uid <= 0:
                raise ValueError("邮件 UID 必须是正整数。")
            index = self._require_index()
            account_id = self.plugin._account_key(account)
            state = await asyncio.to_thread(index.get_state, account_id, source)
            if state is None:
                await self.plugin._sync_account_index(account, folder=source)
                state = await asyncio.to_thread(index.get_state, account_id, source)
            async with self.plugin._lock_for(account):
                result = await asyncio.to_thread(
                    transfer_message,
                    account,
                    source,
                    target,
                    uid,
                    state.uidvalidity if state else None,
                    move=move,
                    timeout=self.plugin._timeout(),
                )
                if move and state is not None:
                    await asyncio.to_thread(
                        index.mark_remote_missing,
                        account_id,
                        source,
                        state.uidvalidity,
                        uid,
                    )
                    if self.plugin._purge_cached_body_on_remote_delete():
                        await asyncio.to_thread(
                            index.delete_cached_body,
                            account_id,
                            source,
                            state.uidvalidity,
                            uid,
                        )
                    await asyncio.to_thread(
                        index.delete_ai_results,
                        account_id,
                        source,
                        state.uidvalidity,
                        uid,
                    )
            await self.plugin._sync_account_index(account, folder=target)
            if move:
                await self.plugin._sync_account_index(
                    account, folder=source, force_reconcile=True
                )
            operation = "move" if move else "copy"
            self.plugin._log_mail_operation(
                f"web_{operation}", account, uid=uid, detail=f"{source} -> {target}"
            )
            return self._ok(asdict(result))
        except Exception as exc:
            return self._error(exc)

    async def copy_message(self) -> dict[str, Any]:
        return await self._transfer(move=False)

    async def move_message(self) -> dict[str, Any]:
        return await self._transfer(move=True)

    @staticmethod
    def _locale_language(value: Any) -> str:
        locale = _one_line(value, 40).replace("_", "-").lower()
        mappings = {
            "zh-cn": "简体中文",
            "zh-hans": "简体中文",
            "zh-tw": "繁體中文",
            "zh-hant": "繁體中文",
            "en": "English",
            "en-us": "English",
            "ja": "日本語",
            "ja-jp": "日本語",
            "ko": "한국어",
            "ko-kr": "한국어",
        }
        return mappings.get(locale, locale or "简体中文")

    async def _process_message(self, task: str) -> dict[str, Any]:
        try:
            payload = await self._json_payload()
            account = self._resolve_account(
                payload.get("account_id"), capability="query_enabled"
            )
            folder = await self._folder(account, payload.get("folder"))
            uid = self._int(payload.get("uid"), 0, 0, 2_147_483_647)
            if uid <= 0:
                raise ValueError("邮件 UID 必须是正整数。")
            interface_language = self._locale_language(payload.get("locale"))
            configured_language = _one_line(
                self.plugin.config.get("translation_language"), 80
            )
            language = (
                configured_language or interface_language
                if task == "translate"
                else interface_language
            )
            lock_key = f"{self.plugin._account_key(account)}:{folder}:{uid}:{task}:{language}"
            lock = self._ai_locks.setdefault(lock_key, asyncio.Lock())
            async with lock:
                mail = await self.plugin._fetch_remote_detail(account, uid, folder)
                index = self._require_index()
                account_id = self.plugin._account_key(account)
                state = await asyncio.to_thread(index.get_state, account_id, folder)
                if state is None:
                    raise RuntimeError("邮件文件夹尚未完成索引。")
                content_hash = mail_content_hash(mail)
                task_key = self.plugin._mail_processing_cache_key(task)
                cached = await asyncio.to_thread(
                    index.get_ai_result,
                    account_id,
                    folder,
                    state.uidvalidity,
                    uid,
                    content_hash,
                    task_key,
                    language,
                )
                if cached is not None:
                    return self._ok(
                        {
                            "content": cached.result_text,
                            "cached": True,
                            "task": task,
                            "target_language": language,
                        }
                    )
                result, provider_id = await self.plugin._process_mail_content(
                    account,
                    mail,
                    task=task,
                    target_language=language,
                )
                await asyncio.to_thread(
                    index.cache_ai_result,
                    account_id,
                    folder,
                    state.uidvalidity,
                    uid,
                    content_hash,
                    task_key,
                    language,
                    result,
                    provider_id,
                )
                return self._ok(
                    {
                        "content": result,
                        "cached": False,
                        "task": task,
                        "target_language": language,
                    }
                )
        except Exception as exc:
            return self._error(exc)

    async def summarize_message(self) -> dict[str, Any]:
        return await self._process_message("summary")

    async def translate_message(self) -> dict[str, Any]:
        return await self._process_message("translate")

    @staticmethod
    def _addresses(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        values = value if isinstance(value, list) else re.split(r"[,;\n]", str(value))
        return tuple(_one_line(item, 320) for item in values if _one_line(item, 320))

    @staticmethod
    def _draft_payload(draft: MailDraft) -> dict[str, Any]:
        return asdict(draft)

    @staticmethod
    def _draft_summary_payload(draft: MailDraft) -> dict[str, Any]:
        return {
            "draft_id": draft.draft_id,
            "account_id": draft.account_id,
            "to_addrs": draft.to_addrs,
            "subject": draft.subject,
            "source": draft.source,
            "status": draft.status,
            "revision": draft.revision,
            "updated_at": draft.updated_at,
            "sent_at": draft.sent_at,
            "last_error": draft.last_error,
            "reply_uid": draft.reply_uid,
        }

    async def list_drafts(self) -> dict[str, Any]:
        try:
            index = self._require_index()
            query = self._query()
            account_id = _one_line(query.get("account_id"), 80)
            status = _one_line(query.get("status"), 32)
            if account_id:
                self._resolve_account(account_id)
            if status and status not in _DRAFT_STATUSES:
                raise ValueError("草稿状态无效。")
            drafts = await asyncio.to_thread(
                index.list_drafts,
                account_id or None,
                status or None,
                self._int(query.get("limit"), 100, 1, 300),
            )
            return self._ok(
                {"items": [self._draft_summary_payload(item) for item in drafts]}
            )
        except Exception as exc:
            return self._error(exc)

    async def get_draft(self) -> dict[str, Any]:
        try:
            index = self._require_index()
            draft_id = _one_line(self._query().get("draft_id"), 64)
            if not draft_id:
                raise ValueError("缺少草稿 ID。")
            draft = await asyncio.to_thread(index.get_draft, draft_id)
            if draft is None:
                raise KeyError("草稿不存在。")
            self._resolve_account(draft.account_id)
            return self._ok(self._draft_payload(draft))
        except Exception as exc:
            return self._error(exc)

    async def create_draft(self) -> dict[str, Any]:
        try:
            index = self._require_index()
            payload = await self._json_payload()
            account = self._resolve_account(payload.get("account_id"))
            reply_uid = None
            if payload.get("reply_uid") not in (None, ""):
                try:
                    reply_uid = int(payload.get("reply_uid"))
                except (TypeError, ValueError) as exc:
                    raise ValueError("回复邮件 UID 必须是正整数。") from exc
                if reply_uid <= 0:
                    raise ValueError("回复邮件 UID 必须是正整数。")
            draft = await asyncio.to_thread(
                index.create_draft,
                self.plugin._account_key(account),
                to_addrs=self._addresses(payload.get("to_addrs")),
                cc_addrs=self._addresses(payload.get("cc_addrs")),
                bcc_addrs=self._addresses(payload.get("bcc_addrs")),
                subject=str(payload.get("subject") or "")[:998],
                body_text=str(payload.get("body_text") or "")[:500_000],
                reply_folder=_one_line(payload.get("reply_folder"), 180),
                reply_uid=reply_uid,
                source="user",
                status="editing",
            )
            return self._ok(self._draft_payload(draft))
        except Exception as exc:
            return self._error(exc)

    async def update_draft(self) -> dict[str, Any]:
        try:
            index = self._require_index()
            payload = await self._json_payload()
            draft_id = _one_line(payload.get("draft_id"), 64)
            if not draft_id:
                raise ValueError("缺少草稿 ID。")
            lock = self._draft_send_locks.setdefault(draft_id, asyncio.Lock())
            async with lock:
                current = await asyncio.to_thread(index.get_draft, draft_id)
                if current is None:
                    raise KeyError("草稿不存在。")
                self._resolve_account(current.account_id)
                if current.status in {"sending", "sent", "cancelled"}:
                    raise PermissionError("发送中、已发送或已取消的草稿不能继续编辑。")
                changes: dict[str, Any] = {}
                for key in ("to_addrs", "cc_addrs", "bcc_addrs"):
                    if key in payload:
                        changes[key] = self._addresses(payload.get(key))
                if "subject" in payload:
                    changes["subject"] = str(payload.get("subject") or "")[:998]
                if "body_text" in payload:
                    changes["body_text"] = str(payload.get("body_text") or "")[:500_000]
                changes["status"] = "editing"
                changes["last_error"] = ""
                updated = await asyncio.to_thread(
                    index.update_draft,
                    draft_id,
                    self._int(payload.get("revision"), 0, 0, 2_147_483_647),
                    **changes,
                )
            return self._ok(self._draft_payload(updated))
        except Exception as exc:
            return self._error(exc)

    async def approve_draft(self) -> dict[str, Any]:
        try:
            index = self._require_index()
            payload = await self._json_payload()
            draft_id = _one_line(payload.get("draft_id"), 64)
            if not draft_id:
                raise ValueError("缺少草稿 ID。")
            lock = self._draft_send_locks.setdefault(draft_id, asyncio.Lock())
            async with lock:
                draft = await asyncio.to_thread(index.get_draft, draft_id)
                if draft is None:
                    raise KeyError("草稿不存在。")
                if draft.status in {"sending", "sent", "cancelled"}:
                    raise PermissionError("发送中、已发送或已取消的草稿不能重新审核。")
                account = self._resolve_account(
                    draft.account_id, capability="send_enabled"
                )
                build_draft_message(
                    account,
                    draft.to_addrs,
                    draft.cc_addrs,
                    draft.bcc_addrs,
                    draft.subject,
                    draft.body_text,
                )
                approved = await asyncio.to_thread(
                    index.update_draft,
                    draft_id,
                    self._int(payload.get("revision"), 0, 0, 2_147_483_647),
                    status="approved",
                    last_error="",
                )
            return self._ok(self._draft_payload(approved))
        except Exception as exc:
            return self._error(exc)

    async def delete_draft(self) -> dict[str, Any]:
        try:
            index = self._require_index()
            payload = await self._json_payload()
            draft_id = _one_line(payload.get("draft_id"), 64)
            if not draft_id:
                raise ValueError("缺少草稿 ID。")
            lock = self._draft_send_locks.setdefault(draft_id, asyncio.Lock())
            async with lock:
                draft = await asyncio.to_thread(index.get_draft, draft_id)
                if draft is None:
                    raise KeyError("草稿不存在。")
                self._resolve_account(draft.account_id)
                if draft.status == "sending":
                    raise PermissionError("发送中的草稿不能删除。")
                deleted = await asyncio.to_thread(
                    index.delete_draft,
                    draft_id,
                    self._int(payload.get("revision"), 0, 0, 2_147_483_647),
                )
                if not deleted:
                    raise RuntimeError("草稿已被其他操作修改，请刷新后重试。")
            return self._ok({"draft_id": draft_id, "deleted": True})
        except Exception as exc:
            return self._error(exc)

    async def send_draft(self) -> dict[str, Any]:
        payload = await self._json_payload()
        draft_id = _one_line(payload.get("draft_id"), 64)
        expected_revision = self._int(
            payload.get("revision"), 0, 0, 2_147_483_647
        )
        if not draft_id:
            return self._error("缺少草稿 ID。")
        try:
            sent = await self.plugin._draft_service.send_approved_draft(
                draft_id,
                expected_revision,
                lambda account_id: self._resolve_account(
                    account_id, capability="send_enabled"
                ),
            )
            return self._ok(self._draft_payload(sent))
        except Exception as exc:
            logger.warning(
                "[EmailAssistantPage] 草稿发送失败 draft=%s error=%s",
                draft_id[:12],
                _one_line(exc, 220),
            )
            return self._error(f"发送失败：{_one_line(exc, 240)}")
