import asyncio
import io
import tempfile
import types
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from .test_main_runtime import _install_astrbot_stubs

_install_astrbot_stubs()

from astrbot_plugin_email_assistant.attachment_store import AttachmentStore
from astrbot_plugin_email_assistant.mail_index import MailHeaderIndex
from astrbot_plugin_email_assistant import page_api


class AttachmentPageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.index = MailHeaderIndex(Path(self.temp.name) / "mail.db")
        self.index.initialize()
        self.store = AttachmentStore(self.index, max_total_bytes=1024, retention_days=90)
        self.account = {"account_id": "one", "email": "sender@example.com", "enabled": True}
        self.plugin = types.SimpleNamespace(
            _mail_index=self.index,
            _draft_service=types.SimpleNamespace(locks={}),
            _attachment_store=lambda: self.store,
            _accounts=lambda: [self.account],
            _account_key=lambda value: value["account_id"],
            _validate_account=lambda value: "",
            _folder=lambda value: "INBOX",
        )
        self.api = page_api.EmailAssistantPageApi(self.plugin)
        self.upload_draft = self.index.create_draft("one", subject="待上传", body_text="手动输入的正文")

    async def upload(self, content=b"file bytes", filename="报告.txt"):
        upload = types.SimpleNamespace(
            filename=filename, content_type="text/plain",
            _upload_file=types.SimpleNamespace(file=io.BytesIO(content), size=len(content)),
        )
        request = types.SimpleNamespace(files=AsyncMock(return_value={"file": upload}))
        with patch.object(page_api, "request", request):
            current = self.index.get_draft(self.upload_draft.draft_id)
            result = await self.api.upload_attachment("one", current.draft_id, str(current.revision))
            if result["status"] == "ok":
                return {**result, "data": result["data"]["attachments"][-1]}
            return result

    async def call_json(self, handler, payload):
        with patch.object(self.api, "_json_payload", AsyncMock(return_value=payload)):
            return await handler()

    async def test_upload_preserves_bytes_sanitizes_name_and_hides_path(self):
        result = await self.upload(filename="../../报告.txt")
        self.assertEqual(result["status"], "ok")
        item = result["data"]
        self.assertEqual(item["filename"], "报告.txt")
        self.assertNotIn("path", item)
        self.assertEqual(self.store.get(item["attachment_id"], "one").path.read_bytes(), b"file bytes")
        self.assertEqual(self.store.stats()["total_bytes"], len(b"file bytes"))
        saved = self.index.get_draft(self.upload_draft.draft_id)
        self.assertEqual(saved.body_text, "手动输入的正文")
        self.assertEqual(saved.attachment_ids, (item["attachment_id"],))
        self.assertEqual(self.store.stats()["protected_bytes"], len(b"file bytes"))

    async def test_upload_checks_capability_and_quota(self):
        self.account["send_enabled"] = False
        self.assertEqual((await self.upload())["status"], "error")
        self.account["send_enabled"] = True
        result = await self.upload(b"x" * 1025)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.stats()["total_bytes"], 0)

    async def test_attachment_update_preserves_text_invalidates_approval_and_checks_revision(self):
        item = (await self.upload())["data"]
        created = await self.call_json(self.api.create_draft, {
            "account_id": "one", "to_addrs": "reader@example.com",
            "subject": "主题", "body_text": "尚未丢失的正文", "attachment_ids": [item["attachment_id"]],
        })
        self.assertEqual(created["status"], "ok", created)
        draft = created["data"]
        self.assertEqual(draft["attachments"][0]["filename"], "报告.txt")
        approved = self.index.update_draft(draft["draft_id"], draft["revision"], status="approved")
        result = await self.call_json(self.api.update_draft, {
            "draft_id": approved.draft_id, "revision": approved.revision,
            "body_text": "修改后的正文", "attachment_ids": [],
        })
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["data"]["status"], "editing")
        self.assertEqual(result["data"]["body_text"], "修改后的正文")
        self.assertFalse(result["data"]["attachment_ids"])
        stale = await self.call_json(self.api.update_draft, {
            "draft_id": approved.draft_id, "revision": approved.revision,
            "attachment_ids": [item["attachment_id"]],
        })
        self.assertEqual(stale["status"], "error")
        self.assertFalse(self.index.get_draft(approved.draft_id).attachment_ids)

    async def test_foreign_and_duplicate_attachments_cannot_bind(self):
        item = (await self.upload())["data"]
        self.account["account_id"] = "other"
        result = await self.call_json(self.api.create_draft, {
            "account_id": "other", "attachment_ids": [item["attachment_id"]],
        })
        self.assertEqual(result["status"], "error")
        self.account["account_id"] = "one"
        result = await self.call_json(self.api.create_draft, {
            "account_id": "one", "attachment_ids": [item["attachment_id"], item["attachment_id"]],
        })
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.index.list_drafts()), 1)

    async def test_download_holds_lease_until_completion_and_releases_on_disconnect(self):
        item = (await self.upload())["data"]
        record = self.store.get(item["attachment_id"], "one")
        draft = self.index.get_draft(self.upload_draft.draft_id)
        self.index.update_draft(draft.draft_id, draft.revision, attachment_ids=())
        for disconnect in (False, True):
            response = page_api._AttachmentDownload(self.store, record)
            events = []

            async def send(event):
                self.assertEqual(self.store.stats()["protected_bytes"], record.size_bytes)
                events.append(event)
                if disconnect and event["type"] == "http.response.body":
                    raise ConnectionError("browser disconnected")

            invocation = response(
                {"type": "http", "method": "GET", "headers": []}, AsyncMock(), send
            )
            if disconnect:
                with self.assertRaises(ConnectionError):
                    await invocation
            else:
                await invocation
                self.assertEqual(events[-1]["body"], b"file bytes")
            self.assertEqual(self.store.stats()["protected_bytes"], 0)

    async def test_download_failure_is_http_error_not_json_file(self):
        with patch.object(self.api, "_query", return_value={
            "account_id": "one", "attachment_id": "invalid",
        }):
            response = await self.api.download_attachment()
        self.assertEqual(response.status_code, 400)

    async def test_cancelled_download_acquisition_does_not_leak_lease(self):
        item = (await self.upload())["data"]
        record = self.store.get(item["attachment_id"], "one")
        draft = self.index.get_draft(self.upload_draft.draft_id)
        self.index.update_draft(draft.draft_id, draft.revision, attachment_ids=())
        entered, release = threading.Event(), threading.Event()
        original = self.store.lease

        @contextmanager
        def delayed_lease(*args):
            with original(*args) as records:
                entered.set()
                release.wait(timeout=3)
                yield records

        with patch.object(self.store, "lease", delayed_lease):
            task = asyncio.create_task(page_api._AttachmentDownload(self.store, record)(
                {"type": "http", "method": "GET", "headers": []}, AsyncMock(), AsyncMock()
            ))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.store.stats()["protected_bytes"], 0)

    async def test_message_attachment_list_checks_query_permission_and_folder(self):
        self.plugin._list_mail_attachments = AsyncMock(return_value=[{
            "part_id": "2", "filename": "报告.txt", "content_type": "text/plain", "size_bytes": 10,
        }])
        with patch.object(self.api, "_query", return_value={"account_id": "one", "uid": 4}):
            result = await self.api.list_message_attachments()
            self.assertEqual(result["data"]["items"][0]["part_id"], "2")
            self.account["query_enabled"] = False
            denied = await self.api.list_message_attachments()
        self.assertEqual(denied["status"], "error")
        self.plugin._list_mail_attachments.assert_awaited_once_with(self.account, 4, "INBOX")

    async def test_later_upload_cannot_evict_pending_draft_attachment(self):
        first = (await self.upload(b"a" * 700))["data"]
        result = await self.upload(b"b" * 700)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.get(first["attachment_id"], "one").path.read_bytes(), b"a" * 700)
        self.assertEqual(self.index.get_draft(self.upload_draft.draft_id).attachment_ids, (first["attachment_id"],))

    async def test_upload_rejects_stale_revision_without_importing_file(self):
        await self.upload()
        upload = types.SimpleNamespace(filename="stale.txt", content_type="text/plain", _upload_file=types.SimpleNamespace(file=io.BytesIO(b"stale"), size=5))
        request = types.SimpleNamespace(files=AsyncMock(return_value={"file": upload}))
        with patch.object(page_api, "request", request):
            result = await self.api.upload_attachment("one", self.upload_draft.draft_id, str(self.upload_draft.revision))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.store.stats()["attachment_count"], 1)
