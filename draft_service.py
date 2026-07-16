from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from typing import Any, Callable

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


def confirmation_present_in_user_message(message: Any, code: Any) -> bool:
    normalized = normalize_confirmation_code(code)
    if not normalized:
        return False
    text = str(message or "").strip()
    pattern = rf"^确认发送\s*[:：]?\s*{re.escape(normalized)}\s*[。.!！]?$"
    return re.fullmatch(pattern, text, flags=re.IGNORECASE) is not None


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

    def _confirmation_ttl_minutes(self) -> int:
        try:
            value = int(self.plugin.config.get("llm_send_confirmation_ttl_minutes", 10))
        except (TypeError, ValueError):
            value = 10
        return max(1, min(60, value))

    @staticmethod
    def _new_confirmation_code() -> str:
        compact = "".join(secrets.choice(_CONFIRMATION_ALPHABET) for _ in range(8))
        return f"{compact[:4]}-{compact[4:]}"

    async def _issue_confirmation(
        self, draft: MailDraft, owner_umo: str, owner_sender_id: str
    ) -> tuple[str, float]:
        code = self._new_confirmation_code()
        expires_at = time.time() + self._confirmation_ttl_minutes() * 60
        await asyncio.to_thread(
            self._index().create_draft_confirmation,
            draft.draft_id,
            draft.revision,
            confirmation_token_hash(code),
            owner_umo,
            owner_sender_id,
            expires_at,
        )
        return code, expires_at

    async def create_bot_draft(
        self,
        account: dict[str, Any],
        *,
        recipient: str,
        subject: str,
        body: str,
        owner_umo: str,
        owner_sender_id: str,
    ) -> tuple[MailDraft, str, float]:
        build_draft_message(account, (recipient,), (), (), subject, body)
        draft = await asyncio.to_thread(
            self._index().create_draft,
            self.plugin._account_key(account),
            to_addrs=(recipient,),
            subject=str(subject),
            body_text=str(body),
            source="bot",
            owner_umo=owner_umo,
            owner_sender_id=owner_sender_id,
            status="pending_review",
        )
        try:
            code, expires_at = await self._issue_confirmation(
                draft, owner_umo, owner_sender_id
            )
        except Exception:
            await asyncio.to_thread(
                self._index().delete_draft, draft.draft_id, draft.revision
            )
            raise
        return draft, code, expires_at

    async def create_bot_reply_draft(
        self,
        account: dict[str, Any],
        *,
        folder: str,
        uid: int,
        body: str,
        owner_umo: str,
        owner_sender_id: str,
    ) -> tuple[MailDraft, str, float]:
        original = await self.plugin._fetch_remote_detail(account, uid, folder or None)
        recipient = original.reply_to or original.from_addr
        if not recipient:
            raise ValueError("原邮件没有可用的回复地址。")
        subject = original.subject.strip() or "(无主题)"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        build_draft_message(
            account, (recipient,), (), (), subject, body, original=original
        )
        draft = await asyncio.to_thread(
            self._index().create_draft,
            self.plugin._account_key(account),
            to_addrs=(recipient,),
            subject=subject,
            body_text=str(body),
            reply_folder=folder or self.plugin._folder(account),
            reply_uid=int(uid),
            reply_message_id=original.message_id,
            source="bot",
            owner_umo=owner_umo,
            owner_sender_id=owner_sender_id,
            status="pending_review",
        )
        try:
            code, expires_at = await self._issue_confirmation(
                draft, owner_umo, owner_sender_id
            )
        except Exception:
            await asyncio.to_thread(
                self._index().delete_draft, draft.draft_id, draft.revision
            )
            raise
        return draft, code, expires_at

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
    ) -> MailDraft:
        self.plugin._log_mail_operation(
            operation,
            account,
            uid=claimed.reply_uid,
            detail=f"draft={claimed.draft_id[:12]}",
        )
        try:
            await asyncio.to_thread(
                send_draft_message,
                account,
                claimed.to_addrs,
                claimed.cc_addrs,
                claimed.bcc_addrs,
                claimed.subject,
                claimed.body_text,
                self.plugin._timeout(),
                original=original,
            )
        except Exception as exc:
            try:
                await asyncio.to_thread(
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
        try:
            sent = await asyncio.to_thread(
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
        return sent

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
            build_draft_message(
                account,
                draft.to_addrs,
                draft.cc_addrs,
                draft.bcc_addrs,
                draft.subject,
                draft.body_text,
                original=original,
            )
            claimed = await asyncio.to_thread(
                self._index().claim_approved_draft_send,
                draft_id,
                expected_revision,
            )
            return await self._deliver_claimed(
                claimed, account, original, operation
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
            build_draft_message(
                account,
                draft.to_addrs,
                draft.cc_addrs,
                draft.bcc_addrs,
                draft.subject,
                draft.body_text,
                original=original,
            )
            claimed = await asyncio.to_thread(
                self._index().claim_confirmed_draft_send,
                draft_id,
                confirmation_token_hash(confirmation_code),
                owner_umo,
                owner_sender_id,
            )
            return await self._deliver_claimed(
                claimed, account, original, "llm_confirmed_send"
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
