import unittest
import sys
from pathlib import Path
from email.message import EmailMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.mail_parser import parse_mail


class MailParserTests(unittest.TestCase):
    def test_decodes_headers_plain_body_and_reply_metadata(self):
        message = EmailMessage()
        message["Subject"] = "早餐提醒"
        message["From"] = "测试用户 <sender@example.com>"
        message["Reply-To"] = "reply@example.com"
        message["Date"] = "Wed, 15 Jul 2026 10:43:00 +0800"
        message["Message-ID"] = "<mail-1@example.com>"
        message["References"] = "<root@example.com>"
        message.set_content("你好，记得吃早餐。")

        parsed = parse_mail(message.as_bytes(), 42)

        self.assertEqual(parsed.uid, 42)
        self.assertEqual(parsed.subject, "早餐提醒")
        self.assertEqual(parsed.from_addr, "sender@example.com")
        self.assertEqual(parsed.reply_to, "reply@example.com")
        self.assertIn("记得吃早餐", parsed.body)
        self.assertEqual(parsed.message_id, "<mail-1@example.com>")
        self.assertEqual(parsed.references, "<root@example.com>")
        self.assertFalse(parsed.has_attachments)

    def test_html_fallback_and_attachment_detection(self):
        message = EmailMessage()
        message["Subject"] = "HTML"
        message["From"] = "sender@example.com"
        message.set_content("<p>Hello <b>World</b></p>", subtype="html")
        message.add_attachment(b"data", maintype="application", subtype="octet-stream", filename="a.bin")

        parsed = parse_mail(message.as_bytes(), 9)

        self.assertEqual(parsed.body, "Hello World")
        self.assertTrue(parsed.has_attachments)

    def test_body_preview_is_bounded(self):
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message.set_content("abcdefghij")
        parsed = parse_mail(message.as_bytes(), 1)
        self.assertEqual(parsed.body_preview(5), "abcde…")


if __name__ == "__main__":
    unittest.main()
