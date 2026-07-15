import asyncio
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _install_astrbot_stubs():
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    platform_api = types.ModuleType("astrbot.api.platform")
    star_mod = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    message = types.ModuleType("astrbot.core.message")
    components = types.ModuleType("astrbot.core.message.components")

    class Logger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

    class AstrBotConfig(dict):
        pass

    class Plain:
        def __init__(self, text):
            self.text = text

    class MessageChain(list):
        pass

    class MessageType:
        FRIEND_MESSAGE = "friend"

    class Star:
        def __init__(self, context):
            self.context = context
            self._kv = {}

        async def get_kv_data(self, key, default=None):
            return self._kv.get(key, default)

        async def put_kv_data(self, key, value):
            self._kv[key] = value

    def register(*args, **kwargs):
        return lambda cls: cls

    def command_group(*args, **kwargs):
        def decorate(fn):
            def command(*args, **kwargs):
                return lambda subfn: subfn

            fn.command = command
            return fn

        return decorate

    api.AstrBotConfig = AstrBotConfig
    api.logger = Logger()
    event_mod.AstrMessageEvent = object
    event_mod.MessageChain = MessageChain
    event_mod.filter = types.SimpleNamespace(command_group=command_group)
    platform_api.MessageType = MessageType
    star_mod.Context = object
    star_mod.Star = Star
    star_mod.register = register
    components.Plain = Plain

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.platform": platform_api,
            "astrbot.api.star": star_mod,
            "astrbot.core": core,
            "astrbot.core.message": message,
            "astrbot.core.message.components": components,
        }
    )


_install_astrbot_stubs()

from astrbot_plugin_email_assistant.imap_client import FetchItem
from astrbot_plugin_email_assistant.mail_parser import ParsedMail
from astrbot_plugin_email_assistant.main import EmailAssistantPlugin


ACCOUNT = {
    "account_id": "one",
    "name": "私人邮箱",
    "enabled": True,
    "owner_umo": "p:FriendMessage:1",
    "receive_enabled": True,
    "query_enabled": True,
    "send_enabled": True,
}


def parsed(uid=2):
    return ParsedMail(uid, "新标题", "", "a@example.com", "a@example.com", "", 0, "", False, "", "")


class FakeContext:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def send_message(self, umo, chain):
        if self.fail:
            raise RuntimeError("platform unavailable")
        self.sent.append((umo, chain[0].text))


class MainRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def immediate_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        self._to_thread_patcher = patch(
            "astrbot_plugin_email_assistant.main.asyncio.to_thread",
            side_effect=immediate_to_thread,
        )
        self._to_thread_patcher.start()
        self.addCleanup(self._to_thread_patcher.stop)
        self.context = FakeContext()
        self.plugin = EmailAssistantPlugin(
            self.context,
            {"mail_accounts": [dict(ACCOUNT)], "max_fetch_per_check": 20, "network_timeout_seconds": 20},
        )
        self.account = self.plugin._accounts()[0]

    async def test_first_check_only_establishes_baseline(self):
        with patch("astrbot_plugin_email_assistant.main.get_max_uid", return_value=8):
            count, baseline = await self.plugin._check_account(self.account)
        self.assertEqual((count, baseline), (0, True))
        self.assertEqual(self.context.sent, [])
        self.assertEqual(await self.plugin.get_kv_data(self.plugin._cursor_key(self.account)), 8)

    async def test_concurrent_checks_do_not_duplicate_notification(self):
        await self.plugin.put_kv_data(self.plugin._cursor_key(self.account), 1)

        def fake_fetch(account, last_uid, limit, timeout):
            return [FetchItem(2, parsed(2))] if last_uid < 2 else []

        with patch("astrbot_plugin_email_assistant.main.fetch_after_uid", side_effect=fake_fetch):
            await asyncio.gather(self.plugin._check_account(self.account), self.plugin._check_account(self.account))
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.context.sent[0][1], "📧 [私人邮箱] 新邮件：新标题")
        self.assertEqual(await self.plugin.get_kv_data(self.plugin._cursor_key(self.account)), 2)

    async def test_failed_notification_does_not_advance_cursor(self):
        await self.plugin.put_kv_data(self.plugin._cursor_key(self.account), 1)
        self.context.fail = True
        with patch(
            "astrbot_plugin_email_assistant.main.fetch_after_uid",
            return_value=[FetchItem(2, parsed(2))],
        ):
            with self.assertRaises(RuntimeError):
                await self.plugin._check_account(self.account)
        self.assertEqual(await self.plugin.get_kv_data(self.plugin._cursor_key(self.account)), 1)

    async def test_invalid_or_duplicate_account_id_is_rejected(self):
        self.plugin.config["mail_accounts"].append(dict(ACCOUNT))
        with self.assertRaisesRegex(ValueError, "不唯一"):
            await self.plugin._check_account(self.account)

    def test_no_llm_or_conversation_history_write_path(self):
        source = inspect.getsource(sys.modules["astrbot_plugin_email_assistant.main"])
        self.assertNotIn("llm_generate", source)
        self.assertNotIn("add_message_pair", source)


if __name__ == "__main__":
    unittest.main()
