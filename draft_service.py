from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from collections.abc import Iterable
from typing import Any, Callable

from .config_utils import config_get
from .mail_index import MailDraft
from .smtp_client import build_draft_message, send_draft_message


_CONFIRMATION_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class DraftDeliveryError(RuntimeError):
    """SMTP was attempted, so delivery may be uncertain even when an error surfaced."""


def normalize_confirmation_code(value: Any) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if len(compact) != 8:
        return ""
    return f"{compact[:4]}-{compact[4:]}"


def confirmation_token_hash(value: Any) -> str:
    normalized = normalize_confirmation_code(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def confirmation_code_from_user_message(message: Any) -> str:
    text = str(message or "").strip()
    match = re.fullmatch(
        r"确认发送\s*[:：]?\s*([23456789ABCDEFGHJKLMNPQRSTUVWXYZ-]+)\s*[。.!！]?",
        text,
        flags=re.IGNORECASE,
    )
    return normalize_confirmation_code(match.group(1)) if match else ""


def confirmation_present_in_user_message(message: Any, code: Any) -> bool:
    normalized = normalize_confirmation_code(code)
    return bool(normalized) and confirmation_code_from_user_message(message) == normalized


class EmailDraftService:
    """Shared draft workflow used by both the plugin page and LLM tools."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, draft_id: str) -> asyncio.Lock:
        return self.locks.setdefault(str(draft_id), asyncio.Lock())

    def _index(self):
        index = self.plugin._mail_index
        if index is None:
            raise RuntimeError("本地邮件索引未启用，无法使用安全草稿工作流。")
        return index

    @asynccontextmanager
    async def _lease_attachments(
        self,
        attachment_ids: Iterable[str],
        account_id: str,
        *,
        owner_umo: str | None = None,
        owner_sender_id: str | None = None,
    ):
        ids = tuple(attachment_ids)
        if not ids:
            yield ()
            return
        manager = self.plugin._attachment_store().lease(ids, account_id)
        acquisition = asyncio.create_task(asyncio.to_thread(manager.__enter__))
        cancelled = False
        while True:
            try:
                records = await asyncio.shield(acquisition)
                break
            except asyncio.CancelledError:
                if acquisition.cancelled():
                    raise
                cancelled = True
        try:
            if cancelled:
                raise asyncio.CancelledError
            for record in records:
                if owner_umo is not None and (
                    (record.owner_umo and record.owner_umo != owner_umo)
                    or (record.owner_sender_id and record.owner_sender_id != owner_sender_id)
                ):
                    raise PermissionError("不能使用其他用户上传的附件。")
            yield records
        finally:
            await asyncio.shield(asyncio.to_thread(manager.__exit__, None, None, None))

    def _confirmation_ttl_minutes(self) -> int:
        try:
            value = int(config_get(self.plugin.config, "llm_send_confirmation_ttl_minutes", 10))
        except (TypeError, ValueError):
            value = 10
        return max(1, min(60, value))

    @staticmethod
    async def _index_operation(function, *args, **kwargs):
        """Wait for a SQLite worker to finish before reacting to cancellation."""
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True

    @staticmethod
    def _new_confirmation_code() -> str:
        compact = "".join(secrets.choice(_CONFIRMATION_ALPHABET) for _ in range(8))
        return f"{compact[:4]}-{compact[4:]}"

    async def _issue_confirmation(
        self, draft: MailDraft, owner_umo: str, owner_sender_id: str
    ) -> tuple[str, float]:
        code = self._new_confirmation_code()
        expires_at = time.time() + self._confirmation_ttl_minutes() * 60
        _, cancelled = await self._index_operation(
            self._index().create_draft_confirmation,
            draft.draft_id,
            draft.revision,
            confirmation_token_hash(code),
            owner_umo,
            owner_sender_id,
            expires_at,
        )
        if cancelled:
            raise asyncio.CancelledError
        return code, expires_at

    async def _create_confirmed_draft(
        self, account_id: str, owner_umo: str, owner_sender_id: str, **fields,
    ) -> tuple[MailDraft, str, float]:
        draft = None
        try:
            draft, cancelled = await self._index_operation(
                self._index().create_draft,
                account_id,
                source="bot",
                owner_umo=owner_umo,
                owner_sender_id=owner_sender_id,
                status="pending_review",
                **fields,
            )
            if cancelled:
                raise asyncio.CancelledError
            code, expires_at = await self._issue_confirmation(
                draft, owner_umo, owner_sender_id
            )
        except BaseException:
            if draft is not None:
                try:
                    await self._index_operation(
                        self._index().delete_draft, draft.draft_id, draft.revision
                    )
                except Exception:
                    # A separately edited draft must keep its current content.
                    pass
            raise
        return draft, code, expires_at

    async def create_bot_draft(
        self,
        account: dict[str, Any],
        *,
        recipient: str,
        subject: str,
        body: str,
        owner_umo: str,
        owner_sender_id: str,
        attachment_ids: Iterable[str] = (),
    ) -> tuple[MailDraft, str, float]:
        ids = tuple(attachment_ids)
        account_id = self.plugin._account_key(account)
        async with self._lease_attachments(ids, account_id, owner_umo=owner_umo, owner_sender_id=owner_sender_id):
            build_draft_message(account, (recipient,), (), (), subject, body)
            return await self._create_confirmed_draft(
                account_id,
                owner_umo,
                owner_sender_id,
                to_addrs=(recipient,),
                subject=str(subject),
                body_text=str(body),
                attachment_ids=ids,
            )

    async def create_bot_reply_draft(
        self,
        account: dict[str, Any],
        *,
        folder: str,
        uid: int,
        body: str,
        owner_umo: str,
        owner_sender_id: str,
        attachment_ids: Iterable[str] = (),
    ) -> tuple[MailDraft, str, float]:
        original = await self.plugin._fetch_remote_detail(account, uid, folder or None)
        recipient = original.reply_to or original.from_addr
        if not recipient:
            raise ValueError("原邮件没有可用的回复地址。")
        subject = original.subject.strip() or "(无主题)"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        ids = tuple(attachment_ids)
        account_id = self.plugin._account_key(account)
        async with self._lease_attachments(ids, account_id, owner_umo=owner_umo, owner_sender_id=owner_sender_id):
            build_draft_message(
                account, (recipient,), (), (), subject, body, original=original
            )
            return await self._create_confirmed_draft(
                account_id,
                owner_umo,
                owner_sender_id,
                to_addrs=(recipient,),
                subject=subject,
                body_text=str(body),
                reply_folder=folder or self.plugin._folder(account),
                reply_uid=int(uid),
                reply_message_id=original.message_id,
                attachment_ids=ids,
            )

    async def _load_original(
        self, draft: MailDraft, account: dict[str, Any]
    ):
        if draft.reply_uid is None:
            return None
        if not account.get("query_enabled", True):
            raise PermissionError("回复草稿发送前需要启用邮件查询功能。")
        return await self.plugin._fetch_remote_detail(
            account, draft.reply_uid, draft.reply_folder or None
        )

    async def _deliver_claimed(
        self,
        claimed: MailDraft,
        account: dict[str, Any],
        original: Any,
        operation: str,
        attachments: Iterable[Any] = (),
    ) -> MailDraft:
        self.plugin._log_mail_operation(
            operation,
            account,
            uid=claimed.reply_uid,
            detail=f"draft={claimed.draft_id[:12]}",
        )
        started = time.monotonic()
        try:
            delivery_task = asyncio.create_task(asyncio.to_thread(
                send_draft_message,
                account,
                claimed.to_addrs,
                claimed.cc_addrs,
                claimed.bcc_addrs,
                claimed.subject,
                claimed.body_text,
                self.plugin._timeout(),
                original=original,
                attachments=attachments,
            ))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(delivery_task)
                    break
                except asyncio.CancelledError:
                    # A worker thread can still be reading the leased files.
                    if delivery_task.cancelled():
                        raise
                    cancelled = True
        except Exception as exc:
            try:
                await self._index_operation(
                    self._index().finish_draft_send,
                    claimed.draft_id,
                    claimed.revision,
                    success=False,
                    error=str(exc),
                )
            except Exception:
                pass
            raise DraftDeliveryError(
                "SMTP 发送尝试失败，投递结果可能不确定；系统不会自动重试，请先检查已发送文件夹。"
            ) from exc
        finally:
            status = getattr(self.plugin, "_status", None)
            if isinstance(status, dict):
                status.setdefault(claimed.account_id, {})["last_smtp_duration_ms"] = round(
                    (time.monotonic() - started) * 1000
                )
        try:
            sent, finish_cancelled = await self._index_operation(
                self._index().finish_draft_send,
                claimed.draft_id,
                claimed.revision,
                success=True,
            )
        except Exception as exc:
            raise DraftDeliveryError(
                "SMTP 已完成但本地发送状态保存失败，投递结果不确定；"
                "系统不会自动重试，请检查已发送文件夹。"
            ) from exc
        self.plugin._log_mail_operation(
            f"{operation}_success",
            account,
            uid=claimed.reply_uid,
            detail=f"draft={claimed.draft_id[:12]}",
        )
        if cancelled or finish_cancelled:
            raise asyncio.CancelledError
        return sent

    async def _abort_before_smtp(self, claimed: MailDraft) -> None:
        await self._index_operation(
            self._index().finish_draft_send,
            claimed.draft_id,
            claimed.revision,
            success=False,
            error="发送请求已取消，尚未尝试 SMTP。",
        )

    async def send_approved_draft(
        self,
        draft_id: str,
        expected_revision: int,
        resolve_account: Callable[[str], dict[str, Any]],
        *,
        operation: str = "web_send_draft",
    ) -> MailDraft:
        async with self.lock_for(draft_id):
            draft = await asyncio.to_thread(self._index().get_draft, draft_id)
            if draft is None:
                raise KeyError("草稿不存在。")
            if draft.revision != int(expected_revision):
                raise RuntimeError("草稿已被其他操作修改，请刷新后重试。")
            if draft.status != "approved":
                raise PermissionError("草稿必须先审核通过才能发送。")
            account = resolve_account(draft.account_id)
            original = await self._load_original(draft, account)
            async with self._lease_attachments(draft.attachment_ids, draft.account_id) as attachments:
                build_draft_message(
                    account,
                    draft.to_addrs,
                    draft.cc_addrs,
                    draft.bcc_addrs,
                    draft.subject,
                    draft.body_text,
                    original=original,
                )
                claimed, cancelled = await self._index_operation(
                    self._index().claim_approved_draft_send,
                    draft_id,
                    expected_revision,
                )
                if cancelled:
                    await self._abort_before_smtp(claimed)
                    raise asyncio.CancelledError
                return await self._deliver_claimed(
                    claimed, account, original, operation, attachments
                )

    async def send_confirmed_draft(
        self,
        draft_id: str,
        confirmation_code: str,
        owner_umo: str,
        owner_sender_id: str,
        resolve_account: Callable[[str], dict[str, Any]],
    ) -> MailDraft:
        async with self.lock_for(draft_id):
            draft = await asyncio.to_thread(self._index().get_draft, draft_id)
            if draft is None:
                raise KeyError("草稿不存在。")
            if (
                draft.owner_umo != owner_umo
                or draft.owner_sender_id != owner_sender_id
            ):
                raise PermissionError("不能发送其他用户的草稿。")
            account = resolve_account(draft.account_id)
            original = await self._load_original(draft, account)
            async with self._lease_attachments(
                draft.attachment_ids, draft.account_id,
                owner_umo=owner_umo, owner_sender_id=owner_sender_id,
            ) as attachments:
                build_draft_message(
                    account,
                    draft.to_addrs,
                    draft.cc_addrs,
                    draft.bcc_addrs,
                    draft.subject,
                    draft.body_text,
                    original=original,
                )
                claimed, cancelled = await self._index_operation(
                    self._index().claim_confirmed_draft_send,
                    draft_id,
                    confirmation_token_hash(confirmation_code),
                    owner_umo,
                    owner_sender_id,
                )
                if cancelled:
                    await self._abort_before_smtp(claimed)
                    raise asyncio.CancelledError
                return await self._deliver_claimed(
                    claimed, account, original, "llm_confirmed_send", attachments
                )

    async def cancel_owned_draft(
        self, draft_id: str, owner_umo: str, owner_sender_id: str
    ) -> MailDraft:
        async with self.lock_for(draft_id):
            return await asyncio.to_thread(
                self._index().cancel_owned_draft,
                draft_id,
                owner_umo,
                owner_sender_id,
            )
