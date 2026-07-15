import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.mail_parser import ParsedMail
from astrbot_plugin_email_assistant.smtp_client import build_message, send_mail


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


if __name__ == "__main__":
    unittest.main()
