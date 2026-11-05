import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.attachment_store import AttachmentQuotaError, AttachmentStore
from astrbot_plugin_email_assistant.mail_index import MailHeaderIndex


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.index = MailHeaderIndex(Path(self.directory.name) / "mail_headers.db")
        self.index.initialize()
        self.store = AttachmentStore(self.index, max_total_bytes=10, retention_days=90)

    def receive(self, content=b"abcd", account="account", **kwargs):
        def writer(path, max_bytes):
            self.assertFalse(path.exists())
            if len(content) > max_bytes:
                raise AttachmentQuotaError("附件超出可用容量。")
            path.write_bytes(content)
        return self.store.receive_file(
            account, writer, filename="file.txt", expected_size=len(content), **kwargs,
        )

    def age(self, attachment_id):
        with self.index._connection() as connection:
            connection.execute(
                "UPDATE mail_attachments SET last_accessed_at = 1 WHERE attachment_id = ?",
                (attachment_id,),
            )

    def test_managed_copy_is_immutable_and_metadata_has_no_local_path(self):
        source = Path(self.directory.name) / "source.txt"
        source.write_bytes(b"original")
        item = self.store.import_file("account", source, filename="../../test.txt")
        source.write_bytes(b"changed")
        self.assertEqual(item.path.read_bytes(), b"original")
        self.assertEqual(item.filename, "test.txt")
        self.assertNotIn("path", item.payload())
        self.assertEqual(item.path.stat().st_mode & 0o777, 0o400)
        with self.assertRaises(ValueError):
            self.store.get(item.attachment_id, "other")
        with self.assertRaises(ValueError):
            self.store.get("../source.txt", "account")

    def test_quota_evicts_lru_across_accounts_and_preserves_pending_draft(self):
        first = self.receive(account="first")
        draft = self.index.create_draft("first", attachment_ids=[first.attachment_id])
        second = self.receive(account="second")
        third = self.receive(account="third")
        self.assertTrue(first.path.exists())
        self.assertFalse(second.path.exists())
        self.assertTrue(third.path.exists())
        self.assertEqual(self.store.stats()["total_bytes"], 8)
        self.age(first.attachment_id)
        self.store.prune()
        self.assertTrue(first.path.exists())
        self.index.update_draft(draft.draft_id, draft.revision, status="cancelled")
        self.store.prune()
        self.assertFalse(first.path.exists())

    def test_leases_and_failed_drafts_protect_attachments_until_released(self):
        item = self.receive(b"12345678")
        with self.store.lease([item.attachment_id], "account"):
            self.age(item.attachment_id)
            self.store.prune()
            with self.assertRaises(AttachmentQuotaError):
                self.receive()
        draft = self.index.create_draft("account", attachment_ids=[item.attachment_id], status="failed")
        self.store.prune()
        self.assertTrue(item.path.exists())
        self.index.update_draft(draft.draft_id, draft.revision, status="cancelled")
        self.store.prune()
        self.assertFalse(item.path.exists())

    def test_attachment_mutation_invalidates_confirmation_and_approval(self):
        item = self.receive(owner_umo="chat", owner_sender_id="owner")
        draft = self.index.create_draft(
            "account", source="bot", owner_umo="chat", owner_sender_id="owner",
            status="pending_review",
        )
        self.index.create_draft_confirmation(
            draft.draft_id, draft.revision, "token", "chat", "owner", time.time() + 60,
        )
        updated = self.index.update_draft(
            draft.draft_id, draft.revision, attachment_ids=[item.attachment_id], status="approved",
        )
        self.assertEqual(updated.status, "editing")
        self.assertEqual(updated.attachment_ids, (item.attachment_id,))
        with self.assertRaisesRegex(RuntimeError, "旧确认码"):
            self.index.claim_confirmed_draft_send(draft.draft_id, "token", "chat", "owner")
        approved = self.index.update_draft(updated.draft_id, updated.revision, status="approved")
        self.assertEqual(approved.status, "approved")
        with self.assertRaises(PermissionError):
            self.index.create_draft("account", owner_umo="other", owner_sender_id="owner", attachment_ids=[item.attachment_id])
        with self.assertRaises(ValueError):
            self.index.create_draft("other", attachment_ids=[item.attachment_id])

    def test_integrity_failure_prevents_lease_and_missing_reference_rejected(self):
        item = self.receive()
        os.chmod(item.path, 0o600)
        item.path.write_bytes(b"evil")
        with self.assertRaisesRegex(ValueError, "完整性"):
            with self.store.lease([item.attachment_id], "account"):
                self.fail("corrupt file was leased")
        item.path.unlink()
        with self.assertRaisesRegex(ValueError, "缺失"):
            self.index.create_draft("account", attachment_ids=[item.attachment_id])

    def test_writer_failure_removes_partial_without_metadata(self):
        def writer(path, max_bytes):
            path.write_bytes(b"part")
            raise OSError("connection lost")
        with self.assertRaises(OSError):
            self.store.receive_file("account", writer, filename="broken", expected_size=4)
        self.assertEqual(list(self.store.root.iterdir()), [])
        self.assertEqual(self.store.stats()["attachment_count"], 0)

    def test_encoded_size_estimate_can_exceed_budget_when_decoded_payload_fits(self):
        def writer(path, max_bytes):
            self.assertEqual(max_bytes, 10)
            path.write_bytes(b"decoded")
        record = self.store.receive_file(
            "account", writer, filename="encoded.txt", expected_size=20,
        )
        self.assertEqual(record.size_bytes, 7)

    def test_oversized_local_import_preserves_existing_cache(self):
        item = self.receive()
        source = Path(self.directory.name) / "large.txt"
        source.write_bytes(b"x" * 20)
        with self.assertRaises(AttachmentQuotaError):
            self.store.import_file("account", source, filename="large.txt")
        self.assertTrue(item.path.exists())

    def test_concurrent_upload_reserves_capacity_and_preserves_pinned_file(self):
        pinned = self.receive()
        self.index.create_draft("account", attachment_ids=[pinned.attachment_id])
        entered = threading.Event()
        release = threading.Event()
        second_started = threading.Event()

        def first():
            def writer(path, max_bytes):
                self.assertEqual(max_bytes, 6)
                path.write_bytes(b"1234")
                entered.set()
                self.assertTrue(release.wait(5))
            return self.store.receive_file("first", writer, filename="first", expected_size=4)

        def second():
            second_started.set()
            return self.receive(account="second")

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_first = executor.submit(first)
            self.assertTrue(entered.wait(5))
            future_second = executor.submit(second)
            self.assertTrue(second_started.wait(5))
            release.set()
            first_item = future_first.result(5)
            second_item = future_second.result(5)
            self.assertFalse(first_item.path.exists())
            self.assertTrue(second_item.path.exists())
            self.assertTrue(pinned.path.exists())
            self.assertLessEqual(self.store.stats()["total_bytes"], 10)

    def test_recovery_removes_only_owned_orphans_and_reuses_verified_source(self):
        item = self.receive(source_key="INBOX:1:2:part")
        self.assertEqual(self.store.find_source("account", "INBOX:1:2:part").attachment_id, item.attachment_id)
        self.assertIsNone(self.store.find_source("other", "INBOX:1:2:part"))
        partial = self.store.root / (".partial-" + "a" * 32)
        orphan = self.store.root / ("b" * 32)
        partial.write_bytes(b"1")
        orphan.write_bytes(b"2")
        reloaded = AttachmentStore(self.index, max_total_bytes=10, retention_days=90)
        self.assertFalse(partial.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(reloaded.get(item.attachment_id, "account").sha256, hashlib.sha256(b"abcd").hexdigest())

    def test_crash_during_eviction_recovers_file_when_metadata_is_not_committed(self):
        item = self.receive()
        tomb = self.store.root / f".deleted-{item.attachment_id}"
        item.path.rename(tomb)
        restored = AttachmentStore(self.index, max_total_bytes=10, retention_days=90)
        self.assertFalse(tomb.exists())
        with restored.lease([item.attachment_id], "account") as records:
            self.assertEqual(records[0].path.read_bytes(), b"abcd")

    def test_orphan_recovery_waits_for_active_receive_and_cross_thread_lease_exit(self):
        entered = threading.Event()
        release = threading.Event()
        recovery_started = threading.Event()
        staging = []

        def receive():
            def writer(path, max_bytes):
                path.write_bytes(b"abcd")
                staging.append(path)
                entered.set()
                self.assertTrue(release.wait(5))
            return self.store.receive_file("account", writer, filename="active", expected_size=4)

        def recover():
            recovery_started.set()
            return AttachmentStore(self.index, max_total_bytes=10, retention_days=90)

        with ThreadPoolExecutor(max_workers=2) as executor:
            receiving = executor.submit(receive)
            self.assertTrue(entered.wait(5))
            recovering = executor.submit(recover)
            self.assertTrue(recovery_started.wait(5))
            self.assertFalse(recovering.done())
            self.assertEqual(staging[0].read_bytes(), b"abcd")
            release.set()
            item = receiving.result(5)
            store = recovering.result(5)
            lease = store.lease([item.attachment_id], "account")
            executor.submit(lease.__enter__).result(5)
            self.assertEqual(store.stats()["protected_bytes"], 4)
            lease.__exit__(None, None, None)
            self.assertEqual(store.stats()["protected_bytes"], 0)
