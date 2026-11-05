import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.attachment_store import AttachmentStore
from astrbot_plugin_email_assistant.draft_service import EmailDraftService, DraftDeliveryError
from astrbot_plugin_email_assistant.mail_index import MailHeaderIndex
from astrbot_plugin_email_assistant.mail_parser import ParsedMail


ACCOUNT = {"id": "one", "email": "bot@example.com", "password": "test-password"}


class DraftAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.index = MailHeaderIndex(Path(self.directory.name) / "index.db")
        self.index.initialize()
        self.store = AttachmentStore(self.index, max_total_bytes=4096, retention_days=90)
        source = Path(self.directory.name) / "source.bin"
        source.write_bytes(bytes(range(256)))
        self.record = self.store.import_file("one", source, filename="附件.bin", owner_umo="private:one", owner_sender_id="one")
        self.other = self.store.import_file("one", source, filename="other.bin", owner_umo="private:two", owner_sender_id="two")
        original = ParsedMail(5, "Original", "Sender", "sender@example.com", "reply@example.com", "", 0, "body", True, "<original@example.com>", "")
        self.plugin = SimpleNamespace(
            config={}, _mail_index=self.index,
            _attachment_store=lambda: self.store,
            _account_key=lambda account: account["id"],
            _fetch_remote_detail=AsyncMock(return_value=original),
            _folder=lambda account: "INBOX", _timeout=lambda: 20,
            _log_mail_operation=Mock(), _status={},
        )
        self.service = EmailDraftService(self.plugin)

    async def create(self, **kwargs):
        return await self.service.create_bot_draft(
            ACCOUNT, recipient="to@example.com", subject="Subject", body="Body",
            owner_umo="private:one", owner_sender_id="one",
            attachment_ids=kwargs.get("attachment_ids", [self.record.attachment_id]),
        )

    async def send(self, draft, code, service=None):
        return await (service or self.service).send_confirmed_draft(
            draft.draft_id, code, "private:one", "one", lambda key: ACCOUNT,
        )

    async def test_missing_or_changed_attachment_does_not_consume_confirmation(self):
        for failure in ("changed", "missing"):
            with self.subTest(failure=failure):
                draft, code, _ = await self.create()
                if failure == "changed":
                    self.record.path.chmod(0o600)
                    self.record.path.write_bytes(b"x" * self.record.size_bytes)
                else:
                    self.record.path.unlink()
                with patch("astrbot_plugin_email_assistant.draft_service.send_draft_message") as smtp:
                    with self.assertRaises(ValueError):
                        await self.send(draft, code)
                    smtp.assert_not_called()
                unchanged = self.index.get_draft(draft.draft_id)
                self.assertEqual(unchanged.status, "pending_review")
                self.assertEqual(unchanged.revision, draft.revision)
                with self.index._connection() as connection:
                    consumed = connection.execute("SELECT consumed_at FROM mail_send_confirmations WHERE draft_id = ?", (draft.draft_id,)).fetchone()[0]
                self.assertIsNone(consumed)
                self.record.path.write_bytes(bytes(range(256)))

    async def test_owner_scope_rejected_for_new_and_reply_drafts(self):
        with self.assertRaises(PermissionError):
            await self.create(attachment_ids=[self.other.attachment_id])
        with self.assertRaises(PermissionError):
            await self.service.create_bot_reply_draft(
                ACCOUNT, folder="INBOX", uid=5, body="Reply",
                owner_umo="private:one", owner_sender_id="one",
                attachment_ids=[self.other.attachment_id],
            )
        self.assertEqual(self.index.list_drafts(), [])

    async def test_reply_contains_only_explicit_attachment_ids(self):
        for attachment_ids in ((), (self.record.attachment_id,)):
            draft, _, _ = await self.service.create_bot_reply_draft(
                ACCOUNT, folder="INBOX", uid=5, body="Reply",
                owner_umo="private:one", owner_sender_id="one", attachment_ids=attachment_ids,
            )
            self.assertEqual(draft.attachment_ids, attachment_ids)
            self.assertEqual(draft.reply_message_id, "<original@example.com>")

    async def test_attachment_mutation_invalidates_confirmation_before_send(self):
        draft, code, _ = await self.create()
        updated = self.index.update_draft(draft.draft_id, draft.revision, attachment_ids=())
        self.assertGreater(updated.revision, draft.revision)
        with patch("astrbot_plugin_email_assistant.draft_service.send_draft_message") as smtp:
            with self.assertRaises((PermissionError, RuntimeError)):
                await self.send(draft, code)
            smtp.assert_not_called()

    async def test_attachment_change_during_claim_cannot_send_old_preview(self):
        draft, code, _ = await self.create()
        original_claim = self.index.claim_confirmed_draft_send

        def update_then_claim(*args):
            self.index.update_draft(draft.draft_id, draft.revision, attachment_ids=())
            return original_claim(*args)

        with patch.object(self.index, "claim_confirmed_draft_send", side_effect=update_then_claim), patch("astrbot_plugin_email_assistant.draft_service.send_draft_message") as smtp:
            with self.assertRaises((PermissionError, RuntimeError)):
                await self.send(draft, code)
            smtp.assert_not_called()

    async def test_duplicate_confirmation_across_services_sends_once(self):
        draft, code, _ = await self.create()
        with patch("astrbot_plugin_email_assistant.draft_service.send_draft_message") as smtp:
            results = await asyncio.gather(
                self.send(draft, code),
                self.send(draft, code, EmailDraftService(self.plugin)),
                return_exceptions=True,
            )
            smtp.assert_called_once()
            self.assertEqual(smtp.call_args.kwargs["attachments"][0].attachment_id, self.record.attachment_id)
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        self.assertEqual(self.index.get_draft(draft.draft_id).status, "sent")
        self.assertGreaterEqual(self.plugin._status["one"]["last_smtp_duration_ms"], 0)

    async def test_cancelled_file_validation_releases_acquired_lease(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        integrity = self.store._integrity

        def blocking_integrity(record):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test validation timed out")
            integrity(record)

        with patch.object(self.store, "_integrity", side_effect=blocking_integrity):
            task = asyncio.create_task(self.create())
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.store._leases[self.record.attachment_id], 0)
        self.assertEqual(self.index.list_drafts(), [])

    async def test_cancelled_creation_or_confirmation_cleans_unreturned_draft(self):
        for method_name in ("create_draft", "create_draft_confirmation"):
            with self.subTest(method=method_name):
                started, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                original = getattr(self.index, method_name)

                def blocking_operation(*args, **kwargs):
                    result = original(*args, **kwargs)
                    started.set()
                    if not release.wait(5):
                        raise RuntimeError("test database operation timed out")
                    return result

                with patch.object(self.index, method_name, side_effect=blocking_operation):
                    task = asyncio.create_task(self.create())
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    task.cancel()
                    await asyncio.sleep(0)
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertEqual(self.index.list_drafts(), [])
                self.assertEqual(self.store._leases[self.record.attachment_id], 0)

    async def test_cancelled_claim_marks_unsent_draft_failed_without_smtp(self):
        for mode in ("confirmed", "approved"):
            with self.subTest(mode=mode):
                draft, code, _ = await self.create()
                if mode == "approved":
                    draft = self.index.update_draft(draft.draft_id, draft.revision, status="approved")
                started, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                method_name = f"claim_{mode}_draft_send"
                original = getattr(self.index, method_name)

                def blocking_claim(*args, **kwargs):
                    result = original(*args, **kwargs)
                    started.set()
                    if not release.wait(5):
                        raise RuntimeError("test claim timed out")
                    return result

                with patch.object(self.index, method_name, side_effect=blocking_claim), patch("astrbot_plugin_email_assistant.draft_service.send_draft_message") as smtp:
                    sending = self.send(draft, code) if mode == "confirmed" else self.service.send_approved_draft(draft.draft_id, draft.revision, lambda key: ACCOUNT)
                    task = asyncio.create_task(sending)
                    self.assertTrue(await asyncio.to_thread(started.wait, 2))
                    task.cancel()
                    await asyncio.sleep(0)
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    smtp.assert_not_called()
                failed = self.index.get_draft(draft.draft_id)
                self.assertEqual(failed.status, "failed")
                self.assertIn("尚未尝试 SMTP", failed.last_error)
                self.assertEqual(self.store._leases[self.record.attachment_id], 0)

    async def test_cancellation_during_result_commit_keeps_sent_state(self):
        draft, code, _ = await self.create()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.index.finish_draft_send

        def blocking_finish(*args, **kwargs):
            result = original(*args, **kwargs)
            started.set()
            if not release.wait(5):
                raise RuntimeError("test finish timed out")
            return result

        with patch.object(self.index, "finish_draft_send", side_effect=blocking_finish), patch("astrbot_plugin_email_assistant.draft_service.send_draft_message") as smtp:
            task = asyncio.create_task(self.send(draft, code))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            smtp.assert_called_once()
        self.assertEqual(self.index.get_draft(draft.draft_id).status, "sent")
        self.assertEqual(self.store._leases[self.record.attachment_id], 0)

    async def test_cancellation_keeps_lease_until_smtp_worker_finishes(self):
        draft, code, _ = await self.create()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocking_send(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test sender timed out")
            self.assertTrue(kwargs["attachments"][0].path.exists())

        with patch("astrbot_plugin_email_assistant.draft_service.send_draft_message", side_effect=blocking_send):
            task = asyncio.create_task(self.send(draft, code))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(self.store._leases[self.record.attachment_id], 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.store._leases[self.record.attachment_id], 0)
        self.assertEqual(self.index.get_draft(draft.draft_id).status, "sent")

    async def test_smtp_failure_is_uncertain_and_never_automatically_retried(self):
        draft, code, _ = await self.create()
        with patch("astrbot_plugin_email_assistant.draft_service.send_draft_message", side_effect=OSError("connection closed")) as smtp:
            with self.assertRaises(DraftDeliveryError):
                await self.send(draft, code)
            with self.assertRaises(PermissionError):
                await self.send(draft, code)
            smtp.assert_called_once()
        self.assertEqual(self.index.get_draft(draft.draft_id).status, "failed")


if __name__ == "__main__":
    unittest.main()
