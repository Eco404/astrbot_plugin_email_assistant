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


if __name__ == "__main__":
    unittest.main()
