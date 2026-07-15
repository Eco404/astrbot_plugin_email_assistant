import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_email_assistant.account_utils import (
    parse_reply_payload,
    parse_send_payload,
    resolve_account,
    visible_accounts,
)


class AccountUtilsTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "admin_uids": ["9000"],
            "mail_accounts": [
                {"account_id": "one", "name": "私人", "owner_umo": "p:FriendMessage:1", "enabled": True},
                {"account_id": "two", "name": "工作", "owner_umo": "p:FriendMessage:2", "enabled": True},
                {"account_id": "off", "name": "关闭", "owner_umo": "p:FriendMessage:1", "enabled": False},
            ],
        }

    def test_owner_only_sees_bound_enabled_accounts(self):
        accounts = visible_accounts(self.config, umo="p:FriendMessage:1", sender_id="1")
        self.assertEqual([item["account_id"] for item in accounts], ["one"])

    def test_admin_sees_all_enabled_accounts(self):
        accounts = visible_accounts(self.config, umo="p:FriendMessage:9000", sender_id="9000")
        self.assertEqual({item["account_id"] for item in accounts}, {"one", "two"})

    def test_duplicate_name_requires_account_id(self):
        accounts = [
            {"account_id": "a", "name": "同名"},
            {"account_id": "b", "name": "同名"},
        ]
        selected, error = resolve_account(accounts, "同名")
        self.assertIsNone(selected)
        self.assertIn("同名", error)
        selected, error = resolve_account(accounts, "a")
        self.assertEqual(selected["account_id"], "a")
        self.assertEqual(error, "")

    def test_send_and_reply_payloads(self):
        self.assertEqual(
            parse_send_payload("one user@example.com 测试 主题|正文内容"),
            ("one", "user@example.com", "测试 主题", "正文内容"),
        )
        self.assertEqual(parse_reply_payload("one 12 已收到 谢谢"), ("one", 12, "已收到 谢谢"))


if __name__ == "__main__":
    unittest.main()
