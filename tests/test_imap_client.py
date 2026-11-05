import unittest
import sys
import base64
import hashlib
import re
import tempfile
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
    list_attachments,
    download_attachment,
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
    def test_gmail_fetch_records_stable_message_id(self):
        raw = (
            b"From: sender@example.com\r\n"
            b"To: reader@example.com\r\n"
            b"Subject: Test\r\n"
            b"Message-ID: <test@example.com>\r\n"
            b"\r\nBody"
        )

        class Connection:
            capabilities = (b"IMAP4REV1", b"X-GM-EXT-1")

            def __init__(self):
                self.calls = []

            def uid(self, *args):
                self.calls.append(args)
                if "BODYSTRUCTURE" in args[2]:
                    return "OK", [b'7 (UID 7 BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 4 1))']
                if "BODY.PEEK[1]" in args[2]:
                    return "OK", [(b"7 (UID 7 BODY[1]<0> {4}", b"Body"), b")"]
                return "OK", [
                    (b"7 (X-GM-MSGID 123456789 RFC822 {120}", raw),
                    b")",
                ]

        mailbox = object.__new__(ImapMailbox)
        mailbox.connection = Connection()
        parsed = mailbox.fetch_uid(7)

        self.assertEqual(parsed.gmail_msgid, "123456789")
        self.assertIn("X-GM-MSGID", mailbox.connection.calls[1][2])

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


class AttachmentConnection:
    capabilities = ()

    def __init__(self, payload=b"binary\x00\xff data", encoding="BASE64"):
        self.payload = payload
        self.encoded = base64.encodebytes(payload) if encoding == "BASE64" else payload
        self.encoding = encoding
        self.calls = []
        self.wrong_part = False
        self.wrong_uid = False
        self.structure = (
            b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 13 1)'
            + b'("APPLICATION" "OCTET-STREAM" NIL NIL NIL "' + encoding.encode() + b'" '
            + str(len(self.encoded)).encode()
            + b' NIL ("ATTACHMENT" ("FILENAME*" "utf-8\'\'%E9%99%84%E4%BB%B6.bin"))) "MIXED")'
        )

    def response(self, name):
        return name, [b"55"]

    def uid(self, *args):
        self.calls.append(args)
        query = args[2]
        if "BODYSTRUCTURE" in query:
            return "OK", [b"7 (UID 7 BODYSTRUCTURE " + self.structure + b")"]
        if "HEADER.FIELDS" in query:
            raw = b"From: a@example.com\r\nSubject: Attachments\r\n\r\n"
            return "OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (FROM SUBJECT)] {52}", raw), b")"]
        match = re.search(r"BODY.PEEK\[([^\]]+)\]<(\d+)\.(\d+)>", query)
        if not match:
            raise AssertionError(query)
        part, offset, count = match.group(1), int(match.group(2)), int(match.group(3))
        content = self.encoded if part == "2" else b"Hello plain\r\n"
        chunk = content[offset:offset + count]
        response_part = "3" if self.wrong_part else part
        uid = 8 if self.wrong_uid else 7
        header = f"7 (UID {uid} BODY[{response_part}]<{offset}> {{{len(chunk)}}}".encode()
        return "OK", [(header, chunk), b")"]


class AttachmentMailbox(ImapMailbox):
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class AttachmentIMAPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name) / "download.bin"
        self.connection = AttachmentConnection()
        self.mailbox = AttachmentMailbox(self.connection)
        self.patch = patch("astrbot_plugin_email_assistant.imap_client.ImapMailbox", return_value=self.mailbox)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_listing_uses_only_structure_and_decodes_unicode_filename(self):
        validity, attachments = list_attachments({}, 7, 55, folder="INBOX")
        self.assertEqual(validity, 55)
        self.assertEqual(attachments[0]["part_id"], "2")
        self.assertEqual(attachments[0]["filename"], "附件.bin")
        self.assertEqual(len(self.connection.calls), 1)
        self.assertIn("BODYSTRUCTURE", self.connection.calls[0][2])

    def test_body_read_fetches_text_without_attachment_payload(self):
        mail = self.mailbox.fetch_uid(7)
        self.assertEqual(mail.body, "Hello plain")
        self.assertTrue(mail.has_attachments)
        self.assertTrue(any("BODY.PEEK[1]" in call[2] for call in self.connection.calls))
        self.assertFalse(any("BODY.PEEK[2]" in call[2] for call in self.connection.calls))
        self.assertFalse(any("RFC822" in call[2] for call in self.connection.calls))

    def test_binary_download_is_chunked_and_hashes_decoded_bytes(self):
        self.connection = AttachmentConnection(bytes(range(256)) * 600)
        self.mailbox.connection = self.connection
        metadata = download_attachment({}, 7, "2", self.destination, 55, max_bytes=len(self.connection.payload))
        self.assertEqual(self.destination.read_bytes(), self.connection.payload)
        self.assertEqual(metadata["sha256"], hashlib.sha256(self.connection.payload).hexdigest())
        self.assertEqual(metadata["size_bytes"], len(self.connection.payload))
        section_calls = [call for call in self.connection.calls if "BODY.PEEK[2]" in call[2]]
        self.assertGreater(len(section_calls), 2)
        self.assertTrue(all(int(re.search(r"\.(\d+)>", call[2]).group(1)) <= 65536 for call in section_calls))

    def test_generation_change_and_text_part_rejected_before_file_creation(self):
        with self.assertRaises(MailboxChangedError):
            download_attachment({}, 7, "2", self.destination, 54, max_bytes=100)
        self.assertEqual(self.connection.calls, [])
        with self.assertRaisesRegex(RuntimeError, "附件不存在"):
            download_attachment({}, 7, "1", self.destination, 55, max_bytes=100)
        self.assertFalse(self.destination.exists())

    def test_wrong_section_uid_and_budget_leave_no_partial_file(self):
        for wrong in ("part", "uid", "budget"):
            with self.subTest(wrong=wrong):
                self.connection.wrong_part = wrong == "part"
                self.connection.wrong_uid = wrong == "uid"
                with self.assertRaises(ValueError):
                    download_attachment({}, 7, "2", self.destination, 55, max_bytes=1 if wrong == "budget" else 100)
                self.assertFalse(self.destination.exists())

    def test_existing_destination_is_preserved(self):
        self.destination.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            download_attachment({}, 7, "2", self.destination, 55, max_bytes=100)
        self.assertEqual(self.destination.read_bytes(), b"existing")

    def test_inline_image_without_filename_is_downloadable(self):
        self.connection.structure = b'("IMAGE" "PNG" NIL "<image@example.com>" NIL "BASE64" 4 NIL ("INLINE" NIL))'
        _, items = list_attachments({}, 7, 55)
        self.assertEqual(items[0]["part_id"], "1")
        self.assertEqual(items[0]["filename"], "attachment.png")

    def test_malformed_base64_removes_partial_file(self):
        self.connection.encoded = b"????"
        self.connection.structure = self.connection.structure.replace(b'"BASE64" 21', b'"BASE64" 4')
        with self.assertRaises(ValueError):
            download_attachment({}, 7, "2", self.destination, 55, max_bytes=100)
        self.assertFalse(self.destination.exists())

    def test_quoted_printable_decoder_handles_chunk_boundaries(self):
        self.connection = AttachmentConnection(b"=E4=BD=A0=E5=A5=BD=\r\nend", "QUOTED-PRINTABLE")
        self.mailbox.connection = self.connection
        with patch("astrbot_plugin_email_assistant.imap_client._ATTACHMENT_CHUNK_BYTES", 4):
            download_attachment({}, 7, "2", self.destination, 55, max_bytes=20)
        self.assertEqual(self.destination.read_bytes(), "你好end".encode())

    def test_server_oversized_literal_is_rejected_before_read_allocation(self):
        class OversizedConnection(AttachmentConnection):
            read_called = False

            def read(self, size):
                self.read_called = True
                raise AssertionError("oversized literal must not be allocated")

            def uid(self, *args):
                if "BODY.PEEK[2]" in args[2]:
                    self.read(1024 * 1024)
                return super().uid(*args)

        self.mailbox.connection = OversizedConnection()
        with self.assertRaisesRegex(ValueError, "容量限制"):
            download_attachment({}, 7, "2", self.destination, 55, max_bytes=100)
        self.assertFalse(self.mailbox.connection.read_called)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
