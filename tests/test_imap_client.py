import unittest
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.imap_client import (
    ImapMailbox,
    MailboxChangedError,
    decode_mailbox_name,
    encode_mailbox_name,
    fetch_after_uid,
    fetch_detail_checked,
    fetch_latest,
    query_since,
    parse_folder_list_item,
    sync_headers,
    transfer_message,
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


class FakeHistoryMailbox(FakeMailbox):
    uidvalidity = 77
    uidnext = 11
    uids = [1, 3, 5, 7, 8, 10]

    def search_uids(self, criterion):
        if criterion == "ALL":
            return list(self.uids)
        if criterion.startswith("SINCE"):
            return [8, 10]
        if criterion.startswith("UID"):
            bounds = criterion.split()[1]
            lower_text, upper_text = bounds.split(":", 1)
            lower = int(lower_text)
            upper = max(self.uids) if upper_text == "*" else int(upper_text)
            return [uid for uid in self.uids if lower <= uid <= upper]
        return []

    def fetch_uid(self, uid, *, headers_only=False):
        return mail(uid)


class FakeTransferConnection:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.calls = []

    def uid(self, *args):
        self.calls.append(args)
        return "OK", [b""]


class FakeTransferMailbox:
    capabilities = (b"MOVE", b"UIDPLUS")
    last_instance = None

    def __init__(self, account, timeout, **kwargs):
        self.conn = FakeTransferConnection(self.capabilities)
        self.uidvalidity = 55
        type(self).last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def ensure_uidvalidity(self, expected):
        if expected != self.uidvalidity:
            raise MailboxChangedError(expected, self.uidvalidity)

    def fetch_uid(self, uid, *, headers_only=False):
        return mail(uid)


class IMAPClientTests(unittest.TestCase):
    def test_folder_list_uses_rfc_compatible_default_arguments(self):
        class Connection:
            def __init__(self):
                self.calls = []

            def list(self, *args):
                self.calls.append(args)
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

        mailbox = object.__new__(ImapMailbox)
        mailbox.connection = Connection()
        folders = mailbox.list_folders()
        self.assertEqual(mailbox.connection.calls, [()])
        self.assertEqual([item.name for item in folders], ["INBOX"])

    def test_modified_utf7_folder_round_trip_and_list_parsing(self):
        encoded = encode_mailbox_name("项目/归档 & 2026")
        self.assertEqual(decode_mailbox_name(encoded), "项目/归档 & 2026")
        list_item = (
            f'(\\HasNoChildren \\Sent) "/" "{encode_mailbox_name("项目/Sent")}"'
        ).encode("ascii")
        parsed = parse_folder_list_item(list_item)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.name, "项目/Sent")
        self.assertEqual(parsed.special_use, "sent")
        self.assertTrue(parsed.selectable)

    def test_move_uses_uid_move_and_refuses_unsafe_fallback(self):
        with patch(
            "astrbot_plugin_email_assistant.imap_client.ImapMailbox",
            FakeTransferMailbox,
        ):
            result = transfer_message({}, "INBOX", "Archive", 7, 55, move=True)
        self.assertTrue(result.used_uid_move)
        self.assertEqual(FakeTransferMailbox.last_instance.conn.calls[0][0], "MOVE")

        class UnsafeMailbox(FakeTransferMailbox):
            capabilities = ()

        with patch(
            "astrbot_plugin_email_assistant.imap_client.ImapMailbox", UnsafeMailbox
        ):
            with self.assertRaisesRegex(RuntimeError, "安全"):
                transfer_message({}, "INBOX", "Archive", 7, 55, move=True)

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
        self.assertEqual(result.history_before_uid, 4)
        self.assertFalse(result.history_complete)

    def test_incremental_sync_and_reconcile(self):
        with patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", FakeSyncMailbox):
            result = sync_headers(
                {}, 55, 4, datetime(2026, 7, 1), reconcile_all=True,
                known_history_complete=True
            )
        self.assertEqual([item.uid for item in result.headers], [7])
        self.assertEqual(result.remote_uids, {2, 4, 7})
        self.assertFalse(result.uidvalidity_changed)

    def test_history_headers_are_backfilled_until_complete(self):
        with patch(
            "astrbot_plugin_email_assistant.imap_client.ImapMailbox",
            FakeHistoryMailbox,
        ):
            initial = sync_headers(
                {}, None, 0, datetime(2026, 7, 1), initial_limit=2
            )
            first_backfill = sync_headers(
                {}, 77, 10, datetime(2026, 7, 1), batch_limit=2,
                known_history_before_uid=initial.history_before_uid or 0,
                known_history_complete=bool(initial.history_complete),
            )
            final_backfill = sync_headers(
                {}, 77, 10, datetime(2026, 7, 1), batch_limit=10,
                known_history_before_uid=first_backfill.history_before_uid or 0,
                known_history_complete=bool(first_backfill.history_complete),
            )
        self.assertEqual([item.uid for item in initial.headers], [8, 10])
        self.assertEqual([item.uid for item in first_backfill.headers], [5, 7])
        self.assertEqual(first_backfill.history_before_uid, 5)
        self.assertFalse(first_backfill.history_complete)
        self.assertEqual([item.uid for item in final_backfill.headers], [1, 3])
        self.assertEqual(final_backfill.history_before_uid, 1)
        self.assertTrue(final_backfill.history_complete)

    def test_checked_detail_rejects_uidvalidity_change(self):
        with patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", FakeSyncMailbox):
            with self.assertRaises(MailboxChangedError):
                fetch_detail_checked({}, 7, 54)


if __name__ == "__main__":
    unittest.main()
