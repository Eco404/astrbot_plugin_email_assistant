import tempfile
import sys
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.mail_index import MailHeaderIndex
from astrbot_plugin_email_assistant.mail_parser import ParsedMail


def mail(uid: int, timestamp: float, subject: str = "主题") -> ParsedMail:
    return ParsedMail(
        uid,
        subject,
        "发件人",
        "sender@example.com",
        "reply@example.com",
        datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        timestamp,
        "",
        False,
        f"<{uid}@example.com>",
        "",
    )


class MailHeaderIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.index = MailHeaderIndex(Path(self.temp_dir.name) / "mail_headers.db")
        self.index.initialize()

    def test_apply_sync_and_query_headers(self):
        first = datetime(2026, 7, 1).timestamp()
        self.index.apply_sync(
            "one",
            "INBOX",
            10,
            2,
            [mail(1, first), mail(2, first + 60)],
            history_before_uid=1,
            history_complete=True,
        )
        results = self.index.query_since(
            "one", "INBOX", datetime(2026, 7, 1), 10
        )
        self.assertEqual([item.uid for item in results], [2, 1])
        state = self.index.get_state("one", "INBOX")
        self.assertEqual(state.uidvalidity, 10)
        self.assertEqual(state.last_synced_uid, 2)
        self.assertEqual(state.history_before_uid, 1)
        self.assertTrue(state.history_complete)

        unchanged = self.index.apply_sync(
            "one", "INBOX", 10, 2, [mail(1, first), mail(2, first + 60)]
        )
        self.assertEqual(unchanged.header_changes, 0)

    def test_folder_catalog_marks_removed_folder_missing(self):
        self.index.replace_folders(
            "one",
            [
                {"name": "INBOX", "display_name": "INBOX", "special_use": "inbox"},
                {"name": "Archive", "display_name": "Archive", "special_use": ""},
            ],
        )
        self.assertEqual([item.name for item in self.index.list_folders("one")], ["INBOX", "Archive"])
        self.index.replace_folders(
            "one",
            [{"name": "INBOX", "display_name": "INBOX", "special_use": "inbox"}],
        )
        self.assertEqual([item.name for item in self.index.list_folders("one")], ["INBOX"])
        all_items = self.index.list_folders("one", active_only=False)
        self.assertEqual(
            next(item.remote_state for item in all_items if item.name == "Archive"),
            "remote_missing",
        )

    def test_ai_cache_isolated_by_content_and_language(self):
        timestamp = datetime(2026, 7, 1).timestamp()
        self.index.apply_sync("one", "INBOX", 10, 1, [mail(1, timestamp)])
        self.index.cache_ai_result(
            "one", "INBOX", 10, 1, "hash-a", "translate:v1", "简体中文", "译文", "provider"
        )
        hit = self.index.get_ai_result(
            "one", "INBOX", 10, 1, "hash-a", "translate:v1", "简体中文"
        )
        self.assertEqual(hit.result_text, "译文")
        self.assertIsNone(
            self.index.get_ai_result(
                "one", "INBOX", 10, 1, "hash-b", "translate:v1", "简体中文"
            )
        )
        self.assertIsNone(
            self.index.get_ai_result(
                "one", "INBOX", 10, 1, "hash-a", "translate:v1", "English"
            )
        )

        self.index.apply_sync(
            "one", "INBOX", 10, 1, [mail(1, timestamp, "变更后的主题")]
        )
        self.assertIsNone(
            self.index.get_ai_result(
                "one", "INBOX", 10, 1, "hash-a", "translate:v1", "简体中文"
            )
        )

    def test_initialize_migrates_existing_index_history_cursor(self):
        path = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(path)
        try:
            with connection:
                connection.executescript(
                    """
                CREATE TABLE mailboxes (
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    last_synced_uid INTEGER NOT NULL DEFAULT 0,
                    last_sync_at REAL NOT NULL DEFAULT 0,
                    last_reconcile_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, folder)
                );
                CREATE TABLE mail_headers (
                    account_id TEXT NOT NULL, folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
                    subject TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '',
                    from_addr TEXT NOT NULL DEFAULT '', reply_to TEXT NOT NULL DEFAULT '',
                    date_text TEXT NOT NULL DEFAULT '', date_ts REAL NOT NULL DEFAULT 0,
                    has_attachments INTEGER NOT NULL DEFAULT 0,
                    message_id TEXT NOT NULL DEFAULT '', references_text TEXT NOT NULL DEFAULT '',
                    remote_state TEXT NOT NULL DEFAULT 'active', last_seen_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, folder, uidvalidity, uid)
                );
                INSERT INTO mailboxes VALUES ('one', 'INBOX', 10, 20, 0, 0);
                INSERT INTO mail_headers (
                    account_id, folder, uidvalidity, uid, remote_state
                ) VALUES ('one', 'INBOX', 10, 12, 'active');
                """
                )
        finally:
            connection.close()
        migrated = MailHeaderIndex(path)
        migrated.initialize()
        state = migrated.get_state("one", "INBOX")
        self.assertEqual(state.history_before_uid, 12)
        self.assertFalse(state.history_complete)

    def test_reconcile_hides_cloud_deleted_header(self):
        timestamp = datetime(2026, 7, 1).timestamp()
        result = self.index.apply_sync(
            "one",
            "INBOX",
            10,
            2,
            [mail(1, timestamp), mail(2, timestamp + 60)],
            remote_uids={2},
        )
        self.assertEqual(result.header_changes, 2)
        self.assertEqual(result.remote_state_changes, 1)
        results = self.index.query_since(
            "one", "INBOX", datetime(2026, 7, 1), 10
        )
        self.assertEqual([item.uid for item in results], [2])
        self.assertEqual(self.index.stats("one", "INBOX")["remote_missing"], 1)

        unchanged = self.index.apply_sync(
            "one", "INBOX", 10, 2, [], remote_uids={2}
        )
        self.assertEqual(unchanged.remote_state_changes, 0)

    def test_uidvalidity_change_hides_old_generation(self):
        timestamp = datetime(2026, 7, 1).timestamp()
        self.index.apply_sync(
            "one", "INBOX", 10, 5, [mail(5, timestamp, "旧邮件")]
        )
        changed = self.index.apply_sync(
            "one", "INBOX", 11, 1, [mail(1, timestamp + 60, "新邮件")]
        )
        self.assertTrue(changed)
        results = self.index.query_since(
            "one", "INBOX", datetime(2026, 7, 1), 10
        )
        self.assertEqual([(item.uid, item.subject) for item in results], [(1, "新邮件")])

    def test_mark_remote_missing_removes_latest(self):
        timestamp = datetime(2026, 7, 1).timestamp()
        self.index.apply_sync(
            "one", "INBOX", 10, 3, [mail(2, timestamp), mail(3, timestamp + 60)]
        )
        self.index.mark_remote_missing("one", "INBOX", 10, 3)
        latest = self.index.latest("one", "INBOX")
        self.assertEqual(latest.uid, 2)

    def test_body_cache_truncates_and_purges_remote_missing(self):
        timestamp = datetime(2026, 7, 1).timestamp()
        self.index.apply_sync(
            "one", "INBOX", 10, 1, [mail(1, timestamp)]
        )
        cached = self.index.cache_body(
            "one", "INBOX", 10, 1, "abcdef", max_item_bytes=5
        )
        self.assertEqual(cached.body_text, "abcde")
        self.assertTrue(cached.truncated)
        self.assertEqual(self.index.get_cached_body("one", "INBOX", 10, 1).body_text, "abcde")
        stats = self.index.stats("one", "INBOX")
        self.assertEqual(stats["cached_bodies"], 1)
        self.index.mark_remote_missing("one", "INBOX", 10, 1)
        self.assertEqual(self.index.purge_remote_missing_bodies("one", "INBOX"), 1)
        self.assertIsNone(self.index.get_cached_body("one", "INBOX", 10, 1))

    def test_uidvalidity_change_purges_old_body_cache(self):
        timestamp = datetime(2026, 7, 1).timestamp()
        self.index.apply_sync("one", "INBOX", 10, 1, [mail(1, timestamp)])
        self.index.cache_body("one", "INBOX", 10, 1, "正文", 1024)
        self.index.apply_sync("one", "INBOX", 11, 1, [mail(1, timestamp)])
        self.assertIsNone(self.index.get_cached_body("one", "INBOX", 10, 1))

    def test_body_cache_prunes_to_total_budget(self):
        timestamp = datetime(2026, 7, 1).timestamp()
        self.index.apply_sync(
            "one", "INBOX", 10, 2,
            [mail(1, timestamp), mail(2, timestamp + 60)]
        )
        self.index.cache_body("one", "INBOX", 10, 1, "1234", 1024)
        self.index.cache_body("one", "INBOX", 10, 2, "5678", 1024)
        self.assertEqual(self.index.prune_body_cache(90, 4), 1)
        self.assertEqual(self.index.stats("one", "INBOX")["cached_bodies"], 1)

    def test_draft_crud_uses_optimistic_revision(self):
        draft = self.index.create_draft(
            "one",
            to_addrs=["reader@example.com"],
            subject="初稿",
            body_text="正文",
            source="bot",
            status="pending_review",
        )
        self.assertEqual(draft.revision, 1)
        self.assertEqual(draft.to_addrs, ("reader@example.com",))
        updated = self.index.update_draft(
            draft.draft_id,
            draft.revision,
            subject="修改后的主题",
            status="approved",
        )
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.status, "approved")
        with self.assertRaisesRegex(RuntimeError, "已被其他操作修改"):
            self.index.update_draft(
                draft.draft_id, draft.revision, subject="过期修改"
            )
        listed = self.index.list_drafts("one", status="approved")
        self.assertEqual([item.draft_id for item in listed], [draft.draft_id])
        self.assertTrue(
            self.index.delete_draft(draft.draft_id, expected_revision=2)
        )
        self.assertIsNone(self.index.get_draft(draft.draft_id))

    def test_header_page_uses_keyset_cursor_and_searches_sender_or_subject(self):
        timestamp = datetime(2026, 7, 1).timestamp()
        self.index.apply_sync(
            "one",
            "INBOX",
            10,
            4,
            [
                mail(1, timestamp, "较早邮件"),
                mail(2, timestamp + 60, "项目通知"),
                mail(3, timestamp + 120, "项目进展"),
                mail(4, timestamp + 180, "最新邮件"),
            ],
        )
        self.index.cache_body("one", "INBOX", 10, 3, "缓存正文", 1024)
        first, has_more = self.index.list_headers_page(
            "one", "INBOX", limit=2
        )
        self.assertEqual([item.uid for item in first], [4, 3])
        self.assertTrue(has_more)
        self.assertTrue(first[1].body_cached)
        second, has_more = self.index.list_headers_page(
            "one",
            "INBOX",
            limit=2,
            before_date_ts=first[-1].date_ts,
            before_uid=first[-1].uid,
        )
        self.assertEqual([item.uid for item in second], [2, 1])
        self.assertFalse(has_more)
        searched, _ = self.index.list_headers_page(
            "one", "INBOX", keyword="项目", limit=10
        )
        self.assertEqual([item.uid for item in searched], [3, 2])


if __name__ == "__main__":
    unittest.main()
