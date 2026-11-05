import unittest
import sys
import hashlib
import tempfile
from types import SimpleNamespace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.mail_parser import ParsedMail
from astrbot_plugin_email_assistant.smtp_client import (
    build_draft_message,
    build_message,
    send_mail,
)


ACCOUNT = {
    "email": "bot@example.com",
    "username": "imap-user@example.com",
    "password": "shared-secret",
    "smtp_username": "smtp-user@example.com",
    "smtp_password": "",
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
    "smtp_security": "ssl",
    "sender_name": "AstrBot",
}


class SMTPClientTests(unittest.TestCase):
    def test_builds_threaded_reply_headers(self):
        original = ParsedMail(
            uid=3,
            subject="问题",
            from_name="User",
            from_addr="user@example.com",
            reply_to="reply@example.com",
            date="",
            timestamp=0,
            body="",
            has_attachments=False,
            message_id="<child@example.com>",
            references="<root@example.com>",
        )
        message = build_message(ACCOUNT, original.reply_to, "Re: 问题", "已收到", original=original)
        self.assertEqual(message["To"], "reply@example.com")
        self.assertEqual(message["In-Reply-To"], "<child@example.com>")
        self.assertEqual(message["References"], "<root@example.com> <child@example.com>")

    def test_reuses_general_password_and_sends(self):
        class FakeSMTP:
            refused = {}

            def __init__(self):
                self.login_args = None

            def login(self, username, password):
                self.login_args = (username, password)

            def send_message(self, message):
                self.message = message
                return self.refused

            def quit(self):
                return None

        fake = FakeSMTP()
        with patch("astrbot_plugin_email_assistant.smtp_client._connect", return_value=fake):
            send_mail(ACCOUNT, "user@example.com", "主题", "正文")
        self.assertEqual(fake.login_args, ("smtp-user@example.com", "shared-secret"))

    def test_rejects_header_injection(self):
        with self.assertRaises(ValueError):
            build_message(ACCOUNT, "user@example.com", "ok\nBcc: evil@example.com", "正文")

    def test_builds_multi_recipient_draft_and_thread_headers(self):
        original = ParsedMail(
            3, "问题", "User", "user@example.com", "reply@example.com", "", 0,
            "", False, "<child@example.com>", "<root@example.com>"
        )
        message = build_draft_message(
            ACCOUNT,
            ["one@example.com", "two@example.com"],
            ["copy@example.com"],
            ["hidden@example.com"],
            "Re: 问题",
            "正文",
            original=original,
        )
        self.assertEqual(message["To"], "one@example.com, two@example.com")
        self.assertEqual(message["Cc"], "copy@example.com")
        self.assertEqual(message["Bcc"], "hidden@example.com")
        self.assertEqual(message["In-Reply-To"], "<child@example.com>")

    def test_mime_attachments_roundtrip_binary_unicode_names_and_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            records = []
            for filename, content_type, payload in [
                ("报告 résumé.bin", "application/octet-stream", bytes(range(256))),
                ("截图.png", "image/png", b"\x89PNG\r\n\x1a\nimage"),
            ]:
                path = Path(directory) / filename
                path.write_bytes(payload)
                records.append(SimpleNamespace(path=path, filename=filename, content_type=content_type, size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
            original = ParsedMail(3, "问题", "User", "user@example.com", "reply@example.com", "", 0, "", False, "<child@example.com>", "<root@example.com>")
            message = build_draft_message(ACCOUNT, ["to@example.com"], ["copy@example.com"], ["hidden@example.com"], "Re: 问题", "正文", original=original, attachments=records)
            parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes(policy=policy.SMTP))
            self.assertEqual(parsed["To"], "to@example.com")
            self.assertEqual(parsed["Cc"], "copy@example.com")
            self.assertEqual(parsed["Bcc"], "hidden@example.com")
            self.assertEqual(parsed["References"], "<root@example.com> <child@example.com>")
            self.assertEqual(parsed.get_body(preferencelist=("plain",)).get_content().strip(), "正文")
            for record, part in zip(records, parsed.iter_attachments(), strict=True):
                self.assertEqual(part.get_filename(), record.filename)
                self.assertEqual(part.get_content_type(), record.content_type)
                self.assertEqual(part.get_payload(decode=True), record.path.read_bytes())

    def test_changed_attachment_rejected_before_smtp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.bin"
            path.write_bytes(b"tampered")
            record = SimpleNamespace(path=path, filename="file.bin", content_type="application/octet-stream", size_bytes=8, sha256=hashlib.sha256(b"original").hexdigest())
            with self.assertRaisesRegex(ValueError, "附件内容"):
                build_draft_message(ACCOUNT, ["to@example.com"], [], [], "subject", "body", attachments=[record])


if __name__ == "__main__":
    unittest.main()
