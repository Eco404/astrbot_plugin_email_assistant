import unittest
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.imap_client import (
    MailboxChangedError,
    fetch_after_uid,
    fetch_detail_checked,
    fetch_latest,
    query_since,
    sync_headers,
)
from astrbot_plugin_email_assistant.mail_parser import ParsedMail


def mail(uid):
    return ParsedMail(uid, f"subject-{uid}", "", "a@example.com", "a@example.com", "", 0, "", False, "", "")


class FakeMailbox:
    def __init__(self, account, timeout):
        self.account = account

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def search_uids(self, criterion):
        return [1, 2, 3, 4]

    def fetch_uid(self, uid, *, headers_only=False):
        if headers_only:
            self.account.setdefault("header_fetches", []).append(uid)
        if uid == 3:
            raise ValueError("broken message")
        return mail(uid)


class FakeSyncMailbox(FakeMailbox):
    uidvalidity = 55
    uidnext = 8

    def search_uids(self, criterion):
        if criterion == "ALL":
            return [2, 4, 7]
        if criterion.startswith("SINCE"):
            return [2, 4, 7]
        if criterion.startswith("UID"):
            return [uid for uid in [2, 4, 7] if uid > 4]
        return []


class IMAPClientTests(unittest.TestCase):
    def test_incremental_fetch_keeps_parse_failure_as_item(self):
        with patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", FakeMailbox):
            items = fetch_after_uid({}, 1, limit=3)
        self.assertEqual([item.uid for item in items], [2, 3, 4])
        self.assertIsNotNone(items[0].mail)
        self.assertIsNone(items[1].mail)
        self.assertIn("broken", items[1].error)

    def test_query_returns_newest_first_and_skips_broken(self):
        from datetime import datetime

        account = {}
        with patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", FakeMailbox):
            results = query_since(account, datetime(2026, 7, 1), limit=4)
        self.assertEqual([item.uid for item in results], [4, 2, 1])
        self.assertEqual(account["header_fetches"], [4, 3, 2, 1])

    def test_fetch_latest_returns_newest_parseable_message(self):
        with patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", FakeMailbox):
            result = fetch_latest({})
        self.assertIsNotNone(result)
        self.assertEqual(result.uid, 4)

    def test_initial_header_sync_uses_recent_tail(self):
        with patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", FakeSyncMailbox):
            result = sync_headers(
                {}, None, 0, datetime(2026, 7, 1), initial_limit=2
            )
        self.assertEqual(result.uidvalidity, 55)
        self.assertEqual(result.uidnext, 8)
        self.assertEqual([item.uid for item in result.headers], [4, 7])
        self.assertEqual(result.scanned_through_uid, 7)

    def test_incremental_sync_and_reconcile(self):
        with patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", FakeSyncMailbox):
            result = sync_headers(
                {}, 55, 4, datetime(2026, 7, 1), reconcile_all=True
            )
        self.assertEqual([item.uid for item in result.headers], [7])
        self.assertEqual(result.remote_uids, {2, 4, 7})
        self.assertFalse(result.uidvalidity_changed)

    def test_checked_detail_rejects_uidvalidity_change(self):
        with patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", FakeSyncMailbox):
            with self.assertRaises(MailboxChangedError):
                fetch_detail_checked({}, 7, 54)


if __name__ == "__main__":
    unittest.main()
