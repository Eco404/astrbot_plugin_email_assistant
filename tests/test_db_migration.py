import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.db_migration import DatabaseVersionError, MailDatabaseVersion
from astrbot_plugin_email_assistant.mail_index import MailHeaderIndex


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "mail_headers.db"
        fixture = Path(__file__).parent / "fixtures" / "mail_index_v1.sql"
        with sqlite3.connect(self.path) as connection:
            connection.executescript(fixture.read_text())
            connection.execute(
                "INSERT INTO mail_messages (account_id,message_key,identity_kind,identity_value) "
                "VALUES ('account','full','message_id','<full>'), ('account','short','message_id','<short>')"
            )
            connection.execute(
                "INSERT INTO mailboxes (account_id,folder,uidvalidity) VALUES ('account','INBOX',3)"
            )
            connection.execute(
                "INSERT INTO mail_headers (account_id,folder,uidvalidity,uid,message_key) "
                "VALUES ('account','INBOX',3,1,'full'), ('account','INBOX',3,2,'short')"
            )
            connection.execute(
                "INSERT INTO mail_bodies (account_id,message_key,body_text,size_bytes,truncated) "
                "VALUES ('account','full','complete',8,0), ('account','short','truncated',9,1)"
            )
            connection.execute(
                "INSERT INTO mail_ai_cache (account_id,message_key,content_hash,task,result_text,created_at,last_accessed_at) "
                "VALUES ('account','full','hash','summary','summary value',1,1)"
            )
            connection.execute(
                "INSERT INTO mail_drafts (draft_id,account_id,subject,body_text,revision,created_at,updated_at) "
                "VALUES ('draft','account','subject','draft body',6,1,1)"
            )

    def test_released_v1_upgrade_preserves_drafts_index_and_complete_caches(self):
        index = MailHeaderIndex(self.path)
        index.initialize()
        self.assertEqual(index.get_draft("draft").body_text, "draft body")
        self.assertEqual(index.get_draft("draft").revision, 6)
        self.assertEqual(index.get_draft("draft").attachment_ids, ())
        self.assertEqual(index.get_cached_body("account", "INBOX", 3, 1).body_text, "complete")
        self.assertIsNone(index.get_cached_body("account", "INBOX", 3, 2))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(MailDatabaseVersion.current_database_version(connection), "2")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mail_headers").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT result_text FROM mail_ai_cache").fetchone()[0], "summary value")
        backups = list(self.path.parent.glob("*.v1-backup-*.db"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(MailDatabaseVersion.current_database_version(backup), "1")
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM mail_bodies").fetchone()[0], 2)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        index.initialize()
        self.assertEqual(len(list(self.path.parent.glob("*.v1-backup-*.db"))), 1)

    def test_migration_failure_rolls_back_schema_and_keeps_backup(self):
        with patch.object(MailDatabaseVersion, "create_attachment_table", side_effect=RuntimeError("disk failure")):
            with self.assertRaisesRegex(DatabaseVersionError, "已回滚"):
                MailHeaderIndex(self.path).initialize()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(MailDatabaseVersion.current_database_version(connection), "1")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(mail_drafts)")}
            self.assertNotIn("attachment_ids_json", columns)
            self.assertEqual(connection.execute("SELECT body_text FROM mail_drafts").fetchone()[0], "draft body")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mail_bodies").fetchone()[0], 2)
        self.assertEqual(len(list(self.path.parent.glob("*.v1-backup-*.db"))), 1)
        MailHeaderIndex(self.path).initialize()

    def test_future_and_unversioned_schemas_are_not_replaced(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE db_version SET version = 'v99'")
        with self.assertRaises(DatabaseVersionError):
            MailHeaderIndex(self.path).initialize()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT body_text FROM mail_drafts").fetchone()[0], "draft body")
            connection.execute("DROP TABLE db_version")
        with self.assertRaisesRegex(DatabaseVersionError, "未受版本管理"):
            MailHeaderIndex(self.path).initialize()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT body_text FROM mail_drafts").fetchone()[0], "draft body")
        self.assertEqual(list(self.path.parent.glob("*.v1-backup-*.db")), [])
