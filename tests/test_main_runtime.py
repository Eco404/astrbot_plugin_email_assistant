import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
        def debug(self, *args, **kwargs):
            pass

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

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name):
            return Path("/tmp") / str(plugin_name)

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
    def llm_tool(*args, **kwargs):
        return lambda fn: fn

    def on_llm_request(*args, **kwargs):
        return lambda fn: fn

    event_mod.filter = types.SimpleNamespace(
        command_group=command_group,
        llm_tool=llm_tool,
        on_llm_request=on_llm_request,
    )
    platform_api.MessageType = MessageType
    star_mod.Context = object
    star_mod.Star = Star
    star_mod.StarTools = StarTools
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

from astrbot_plugin_email_assistant.imap_client import (
    FetchItem,
    HeaderSyncResult,
    MailFolder,
    MailNotFoundError,
)
from astrbot_plugin_email_assistant.mail_index import MailHeaderIndex, mail_content_hash
from astrbot_plugin_email_assistant.mail_parser import ParsedMail
from astrbot_plugin_email_assistant.main import EmailAssistantPlugin
from astrbot_plugin_email_assistant import page_api as page_api_module
from astrbot_plugin_email_assistant.page_api import EmailAssistantPageApi


ACCOUNT = {
    "account_id": "one",
    "name": "私人邮箱",
    "enabled": True,
    "owner_user_id": "1",
    "target_platform": "p",
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
        self.provider = None
        self.providers = {}
        self.persona_manager = None
        self.conversation_manager = None
        self.cron_manager = None

    async def send_message(self, umo, chain):
        if self.fail:
            raise RuntimeError("platform unavailable")
        self.sent.append((umo, chain[0].text))
        return True

    def get_using_provider(self, umo):
        return self.provider

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)

    def get_config(self, umo=None):
        return {"provider_settings": {"default_personality": "default"}}


class FakeResponse:
    def __init__(self, text, *, tools_call_name=None, tools_call_args=None):
        self.completion_text = text
        self.tools_call_name = tools_call_name or []
        self.tools_call_args = tools_call_args or []


class FakeProvider:
    def __init__(self, text="人格化转述"):
        self.text = text
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.text)


class FakePersonaManager:
    async def resolve_selected_persona(self, **kwargs):
        return "current", {"prompt": "当前人格提示"}, None, False


class FakeConversationManager:
    def __init__(self):
        self.cid = "cid-1"
        self.pairs = []

    async def get_curr_conversation_id(self, umo):
        return self.cid

    async def get_conversation(self, umo, cid):
        return types.SimpleNamespace(persona_id="current")

    async def new_conversation(self, umo, title=None):
        self.cid = "new-cid"
        return self.cid

    async def add_message_pair(self, **kwargs):
        self.pairs.append(kwargs)


class FakeCronManager:
    def __init__(self):
        self.jobs = []

    async def add_active_job(self, **kwargs):
        self.jobs.append(kwargs)
        return types.SimpleNamespace(job_id="job-1")


class FakePlatform:
    def __init__(self, platform_id, name):
        self._meta = types.SimpleNamespace(id=platform_id, name=name)

    def meta(self):
        return self._meta


class FakePlatformManager:
    def __init__(self, *platforms):
        self.platform_insts = list(platforms)

    def get_insts(self):
        return self.platform_insts


class FakeEvent:
    def __init__(
        self,
        sender_id,
        umo,
        platform_name="aiocqhttp",
        extras=None,
        message_str="",
    ):
        self.sender_id = sender_id
        self.unified_msg_origin = umo
        self.platform_name = platform_name
        self.extras = extras or {}
        self.message_str = message_str

    def get_sender_id(self):
        return self.sender_id

    def get_platform_name(self):
        return self.platform_name

    def get_extra(self, key):
        return self.extras.get(key)

    def get_message_str(self):
        return self.message_str


class FakePageRequest:
    def __init__(self):
        self.query = {}
        self.payload = {}

    async def json(self, default=None):
        return self.payload


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

    def _enable_test_index(self) -> MailHeaderIndex:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        index = MailHeaderIndex(Path(temp_dir.name) / "mail_headers.db")
        index.initialize()
        self.plugin._mail_index = index
        return index

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
        self.assertEqual(
            self.context.sent[0][1],
            "📧 [私人邮箱] 新邮件：新标题\naccount_id: one | uid: 2",
        )
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

    async def test_notification_resolves_adapter_name_to_platform_instance_id(self):
        self.context.platform_manager = FakePlatformManager(
            FakePlatform("onebot-instance-id", "aiocqhttp")
        )
        self.account["target_platform"] = "aiocqhttp"
        await self.plugin._send_title_notification(self.account, "平台测试", 42)
        self.assertEqual(
            self.context.sent[-1],
            (
                "onebot-instance-id:FriendMessage:1",
                "📧 [私人邮箱] 新邮件：平台测试\naccount_id: one | uid: 42",
            ),
        )

    async def test_notification_rejects_ambiguous_platform_name(self):
        self.context.platform_manager = FakePlatformManager(
            FakePlatform("onebot-a", "aiocqhttp"),
            FakePlatform("onebot-b", "aiocqhttp"),
        )
        self.account["target_platform"] = "aiocqhttp"
        with self.assertRaisesRegex(RuntimeError, "多个实例"):
            await self.plugin._send_title_notification(self.account, "平台测试", 42)

    def test_visible_accounts_match_real_platform_instance_to_adapter_name(self):
        self.context.platform_manager = FakePlatformManager(
            FakePlatform("onebot-instance-id", "aiocqhttp"),
            FakePlatform("telegram-instance-id", "telegram"),
        )
        self.account["target_platform"] = "aiocqhttp"
        matching = FakeEvent("1", "onebot-instance-id:FriendMessage:1")
        other_platform = FakeEvent("1", "telegram-instance-id:FriendMessage:1")
        self.assertEqual(self.plugin._visible_accounts(matching), [self.account])
        self.assertEqual(self.plugin._visible_accounts(other_platform), [])

    async def test_false_send_result_does_not_advance_cursor(self):
        await self.plugin.put_kv_data(self.plugin._cursor_key(self.account), 1)

        async def not_sent(umo, chain):
            return False

        self.context.send_message = not_sent
        with patch(
            "astrbot_plugin_email_assistant.main.fetch_after_uid",
            return_value=[FetchItem(2, parsed(2))],
        ):
            with self.assertRaisesRegex(RuntimeError, "未找到目标平台"):
                await self.plugin._check_account(self.account)
        self.assertEqual(await self.plugin.get_kv_data(self.plugin._cursor_key(self.account)), 1)

    async def test_invalid_or_duplicate_account_id_is_rejected(self):
        self.plugin.config["mail_accounts"].append(dict(ACCOUNT))
        with self.assertRaisesRegex(ValueError, "不唯一"):
            await self.plugin._check_account(self.account)

    async def test_llm_notification_uses_persona_and_optionally_archives(self):
        self.plugin.config.update(
            {
                "notification_mode": "llm",
                "llm_write_official_history": True,
                "narration_prompt": "{account_name}|{sender}|{subject}|{body}|{uid}",
            }
        )
        self.context.provider = FakeProvider()
        self.context.persona_manager = FakePersonaManager()
        self.context.conversation_manager = FakeConversationManager()
        mail = ParsedMail(
            7,
            "测试主题",
            "张三",
            "sender@example.com",
            "sender@example.com",
            "2026-07-16 10:00:00",
            0,
            "正文",
            False,
            "",
            "",
        )

        await self.plugin._send_mail_notification(self.account, mail)

        self.assertEqual(self.context.sent[-1][1], "人格化转述")
        call = self.context.provider.calls[0]
        self.assertEqual(call["system_prompt"], "当前人格提示")
        self.assertEqual(call["max_tokens"], 500)
        self.assertIn("私人邮箱|张三 <sender@example.com>|测试主题|正文|7", call["prompt"])
        pair = self.context.conversation_manager.pairs[0]
        self.assertIn("邮件主动承接占位", pair["user_message"]["content"])
        self.assertEqual(pair["assistant_message"]["content"], "人格化转述")

    async def test_llm_notification_does_not_archive_when_disabled(self):
        self.plugin.config["notification_mode"] = "llm"
        self.plugin.config["llm_write_official_history"] = False
        self.context.provider = FakeProvider()
        self.context.persona_manager = FakePersonaManager()
        self.context.conversation_manager = FakeConversationManager()

        await self.plugin._send_mail_notification(self.account, parsed())

        self.assertEqual(self.context.conversation_manager.pairs, [])

    async def test_llm_notification_uses_configured_provider(self):
        self.plugin.config.update(
            {
                "notification_mode": "llm",
                "narration_provider_id": "mail-provider",
            }
        )
        default_provider = FakeProvider("默认模型结果")
        selected_provider = FakeProvider("指定模型结果")
        self.context.provider = default_provider
        self.context.providers["mail-provider"] = selected_provider
        self.context.persona_manager = FakePersonaManager()

        await self.plugin._send_mail_notification(self.account, parsed())

        self.assertEqual(self.context.sent[-1][1], "指定模型结果")
        self.assertEqual(len(selected_provider.calls), 1)
        self.assertEqual(default_provider.calls, [])

    async def test_blank_provider_falls_back_to_session_provider(self):
        self.plugin.config.update(
            {
                "notification_mode": "llm",
                "narration_provider_id": "",
                "narration_max_tokens": 0,
            }
        )
        self.context.provider = FakeProvider("会话默认模型结果")
        self.context.persona_manager = FakePersonaManager()

        await self.plugin._send_mail_notification(self.account, parsed())

        self.assertEqual(self.context.sent[-1][1], "会话默认模型结果")
        self.assertNotIn("max_tokens", self.context.provider.calls[0])

    async def test_missing_configured_provider_fails_explicitly(self):
        self.plugin.config.update(
            {
                "notification_mode": "llm",
                "narration_provider_id": "missing-provider",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "missing-provider"):
            await self.plugin._send_mail_notification(self.account, parsed())

    async def test_mail_summary_does_not_load_persona(self):
        self.plugin.config["mail_processing_max_tokens"] = 0
        self.context.provider = FakeProvider("简短总结")
        self.context.persona_manager = FakePersonaManager()
        result, _ = await self.plugin._process_mail_content(
            self.account,
            ParsedMail(
                9, "主题", "张三", "a@example.com", "a@example.com",
                "2026-07-16", 0, "正文", False, "", ""
            ),
            task="summary",
            target_language="简体中文",
        )
        self.assertEqual(result, "简短总结")
        call = self.context.provider.calls[0]
        self.assertNotEqual(call["system_prompt"], "当前人格提示")
        self.assertIn("不使用或模仿任何聊天人格", call["system_prompt"])
        self.assertIn("简明总结", call["prompt"])
        self.assertNotIn("func_tool", call)
        self.assertNotIn("max_tokens", call)

        default_key = self.plugin._mail_processing_cache_key("summary")
        self.plugin.config["mail_summary_prompt"] = "自定义总结 {subject}：{body}"
        self.assertNotEqual(
            default_key, self.plugin._mail_processing_cache_key("summary")
        )
        await self.plugin._process_mail_content(
            self.account,
            ParsedMail(
                10, "自定义主题", "", "a@example.com", "a@example.com",
                "", 0, "自定义正文", False, "", ""
            ),
            task="summary",
            target_language="简体中文",
        )
        self.assertIn("自定义总结 自定义主题：自定义正文", self.context.provider.calls[1]["prompt"])

    def test_narration_can_be_extracted_from_send_tool_arguments(self):
        response = FakeResponse(
            "",
            tools_call_name=["send_message_to_user"],
            tools_call_args=[
                {
                    "session": "untrusted:FriendMessage:other",
                    "messages": [
                        {"type": "plain", "text": "工具参数中的转述"},
                        {"type": "image", "path": "/tmp/ignored.png"},
                    ],
                }
            ],
        )
        self.assertEqual(
            self.plugin._narration_from_response(response), "工具参数中的转述"
        )

    async def test_llm_request_includes_output_tool_when_available(self):
        self.plugin.config["notification_mode"] = "llm"
        self.context.provider = FakeProvider("正文转述")
        self.context.persona_manager = FakePersonaManager()
        output_tools = object()

        with patch.object(
            self.plugin, "_narration_output_tool_set", return_value=output_tools
        ):
            await self.plugin._send_mail_notification(self.account, parsed())

        call = self.context.provider.calls[0]
        self.assertIs(call["func_tool"], output_tools)
        self.assertIn("send_message_to_user", call["prompt"])

    async def test_cron_notification_creates_one_shot_job_without_direct_send(self):
        self.plugin.config["notification_mode"] = "cron"
        self.context.cron_manager = FakeCronManager()

        await self.plugin._send_mail_notification(self.account, parsed(9))

        self.assertEqual(self.context.sent, [])
        self.assertEqual(len(self.context.cron_manager.jobs), 1)
        job = self.context.cron_manager.jobs[0]
        self.assertTrue(job["run_once"])
        self.assertTrue(job["persistent"])
        self.assertEqual(job["payload"]["email_assistant"]["uid"], 9)
        self.assertIn("send_message_to_user", job["payload"]["note"])

    async def test_llm_tool_lists_only_visible_query_enabled_accounts(self):
        event = FakeEvent("1", "p:FriendMessage:1")
        result = json.loads(await self.plugin.tool_list_accounts(event))
        self.assertTrue(result["success"])
        self.assertEqual(result["accounts"][0]["account_id"], "one")
        self.assertNotIn("password", result["accounts"][0])

    async def test_llm_request_injects_silent_email_tool_rules_once(self):
        event = FakeEvent("1", "p:FriendMessage:1")
        req = types.SimpleNamespace(system_prompt="原人格提示")

        await self.plugin.inject_email_tool_conversation_rules(event, req)
        first_prompt = req.system_prompt
        await self.plugin.inject_email_tool_conversation_rules(event, req)

        self.assertIn("原人格提示", req.system_prompt)
        self.assertIn("直接返回工具调用", req.system_prompt)
        self.assertIn("不要逐步播报", req.system_prompt)
        self.assertEqual(req.system_prompt, first_prompt)

    async def test_llm_request_does_not_inject_for_group_or_unbound_user(self):
        group_req = types.SimpleNamespace(system_prompt="原提示")
        await self.plugin.inject_email_tool_conversation_rules(
            FakeEvent("1", "p:GroupMessage:1"), group_req
        )
        self.assertEqual(group_req.system_prompt, "原提示")

        other_req = types.SimpleNamespace(system_prompt="原提示")
        await self.plugin.inject_email_tool_conversation_rules(
            FakeEvent("2", "p:FriendMessage:2"), other_req
        )
        self.assertEqual(other_req.system_prompt, "原提示")

    async def test_llm_tool_lists_messages_with_limit(self):
        event = FakeEvent("1", "p:FriendMessage:1")
        mail = ParsedMail(
            12,
            "列表主题",
            "发件人",
            "sender@example.com",
            "sender@example.com",
            "2026-07-16 09:00:00",
            0,
            "不应出现在列表",
            True,
            "",
            "",
        )
        with patch(
            "astrbot_plugin_email_assistant.main.query_since", return_value=[mail]
        ) as query:
            result = json.loads(
                await self.plugin.tool_list_messages(
                    event, account="one", since_date="2026-07-01", limit=999
                )
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["messages"][0]["uid"], 12)
        self.assertNotIn("body", result["messages"][0])
        self.assertEqual(query.call_args.args[3], self.plugin._query_limit())

    async def test_llm_tool_shows_bounded_message_body(self):
        event = FakeEvent("1", "p:FriendMessage:1")
        self.plugin.config["detail_body_max_chars"] = 200
        mail = ParsedMail(
            13,
            "详情主题",
            "发件人",
            "sender@example.com",
            "reply@example.com",
            "2026-07-16 09:00:00",
            0,
            "正" * 300,
            False,
            "",
            "",
        )
        with patch("astrbot_plugin_email_assistant.main.fetch_detail", return_value=mail):
            result = json.loads(
                await self.plugin.tool_show_message(event, 13, account="one")
            )
        self.assertTrue(result["success"])
        self.assertTrue(result["message"]["body_truncated"])
        self.assertLessEqual(len(result["message"]["body"]), 201)

    async def test_llm_tool_gets_latest_message_in_one_operation(self):
        event = FakeEvent("1", "p:FriendMessage:1")
        mail = ParsedMail(
            14,
            "最新主题",
            "发件人",
            "sender@example.com",
            "reply@example.com",
            "2026-07-16 10:00:00",
            0,
            "最新正文",
            False,
            "",
            "",
        )
        with patch(
            "astrbot_plugin_email_assistant.main.fetch_latest", return_value=mail
        ) as latest:
            result = json.loads(
                await self.plugin.tool_get_latest_message(
                    event, account="", since_date="2026-07-01"
                )
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], "one")
        self.assertEqual(result["message"]["uid"], 14)
        self.assertEqual(result["message"]["body"], "最新正文")
        self.assertEqual(latest.call_args.args[1].strftime("%Y-%m-%d"), "2026-07-01")

    async def test_llm_tool_latest_returns_empty_result(self):
        event = FakeEvent("1", "p:FriendMessage:1")
        with patch("astrbot_plugin_email_assistant.main.fetch_latest", return_value=None):
            result = json.loads(await self.plugin.tool_get_latest_message(event))
        self.assertTrue(result["success"])
        self.assertIsNone(result["message"])

    def _enable_llm_draft_workflow(self):
        index = self._enable_test_index()
        self.plugin.config["llm_mail_write_enabled"] = True
        self.account.update(
            {
                "email": "bot@example.com",
                "password": "secret",
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_security": "ssl",
            }
        )
        return index

    async def test_llm_draft_requires_confirmation_in_new_user_message(self):
        index = self._enable_llm_draft_workflow()
        creation_event = FakeEvent(
            "1", "p:FriendMessage:1", message_str="给 reader@example.com 写一封邮件"
        )
        created = json.loads(
            await self.plugin.tool_create_draft(
                creation_event,
                "reader@example.com",
                "测试主题",
                "测试正文",
                account="one",
            )
        )
        self.assertTrue(created["success"])
        draft = created["draft"]
        self.assertEqual(draft["status"], "pending_review")
        self.assertTrue(created["requires_new_user_confirmation"])
        code = draft["confirmation_code"]

        same_turn = json.loads(
            await self.plugin.tool_confirm_send(
                creation_event, draft["draft_id"], code
            )
        )
        self.assertFalse(same_turn["success"])
        self.assertEqual(index.get_draft(draft["draft_id"]).status, "pending_review")

        confirmation_event = FakeEvent(
            "1", "p:FriendMessage:1", message_str=f"确认发送 {code}"
        )
        with patch(
            "astrbot_plugin_email_assistant.draft_service.send_draft_message"
        ) as send:
            sent = json.loads(
                await self.plugin.tool_confirm_send(
                    confirmation_event, draft["draft_id"], code
                )
            )
            duplicate = json.loads(
                await self.plugin.tool_confirm_send(
                    confirmation_event, draft["draft_id"], code
                )
            )
        self.assertTrue(sent["success"])
        self.assertEqual(sent["draft"]["status"], "sent")
        self.assertFalse(duplicate["success"])
        send.assert_called_once()

    async def test_llm_draft_confirmation_requires_exact_message_and_owner(self):
        index = self._enable_llm_draft_workflow()
        owner = FakeEvent("1", "p:FriendMessage:1", message_str="写邮件")
        created = json.loads(
            await self.plugin.tool_create_draft(
                owner, "reader@example.com", "主题", "正文", "one"
            )
        )["draft"]
        code = created["confirmation_code"]
        ambiguous = FakeEvent(
            "1", "p:FriendMessage:1", message_str=f"不要确认发送 {code}"
        )
        denied = json.loads(
            await self.plugin.tool_confirm_send(
                ambiguous, created["draft_id"], code
            )
        )
        self.assertFalse(denied["success"])

        other = FakeEvent("2", "p:FriendMessage:2", message_str=f"确认发送 {code}")
        denied = json.loads(
            await self.plugin.tool_confirm_send(other, created["draft_id"], code)
        )
        self.assertFalse(denied["success"])
        self.assertEqual(index.get_draft(created["draft_id"]).status, "pending_review")

    async def test_llm_reply_draft_refetches_original_before_send(self):
        self._enable_llm_draft_workflow()
        original = ParsedMail(
            77,
            "原主题",
            "Sender",
            "sender@example.com",
            "reply@example.com",
            "2026-07-16",
            0,
            "不可信正文",
            False,
            "<message@example.com>",
            "<root@example.com>",
        )
        event = FakeEvent("1", "p:FriendMessage:1", message_str="回复 UID 77")
        with patch.object(
            self.plugin, "_fetch_remote_detail", new=AsyncMock(return_value=original)
        ) as fetch:
            created_result = json.loads(
                await self.plugin.tool_create_reply_draft(
                    event, 77, "已经收到，谢谢。", "one", "INBOX"
                )
            )
            created = created_result["draft"]
            confirm_event = FakeEvent(
                "1",
                "p:FriendMessage:1",
                message_str=f"确认发送 {created['confirmation_code']}",
            )
            with patch(
                "astrbot_plugin_email_assistant.draft_service.send_draft_message"
            ) as send:
                sent = json.loads(
                    await self.plugin.tool_confirm_send(
                        confirm_event,
                        created["draft_id"],
                        created["confirmation_code"],
                    )
                )
        self.assertTrue(sent["success"])
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(created["to"], ["reply@example.com"])
        self.assertEqual(created["subject"], "Re: 原主题")
        self.assertIs(send.call_args.kwargs["original"], original)

    async def test_llm_cancel_draft_is_owner_scoped_and_invalidates_send(self):
        index = self._enable_llm_draft_workflow()
        event = FakeEvent("1", "p:FriendMessage:1", message_str="写邮件")
        created = json.loads(
            await self.plugin.tool_create_draft(
                event, "reader@example.com", "主题", "正文", "one"
            )
        )["draft"]
        other = FakeEvent("2", "p:FriendMessage:2", message_str="取消")
        denied = json.loads(
            await self.plugin.tool_cancel_draft(other, created["draft_id"])
        )
        self.assertFalse(denied["success"])
        cancelled = json.loads(
            await self.plugin.tool_cancel_draft(event, created["draft_id"])
        )
        self.assertTrue(cancelled["success"])
        self.assertEqual(index.get_draft(created["draft_id"]).status, "cancelled")
        confirm_event = FakeEvent(
            "1",
            "p:FriendMessage:1",
            message_str=f"确认发送 {created['confirmation_code']}",
        )
        send = json.loads(
            await self.plugin.tool_confirm_send(
                confirm_event, created["draft_id"], created["confirmation_code"]
            )
        )
        self.assertFalse(send["success"])

    async def test_llm_smtp_failure_consumes_confirmation_without_retry(self):
        index = self._enable_llm_draft_workflow()
        event = FakeEvent("1", "p:FriendMessage:1", message_str="写邮件")
        created = json.loads(
            await self.plugin.tool_create_draft(
                event, "reader@example.com", "主题", "正文", "one"
            )
        )["draft"]
        confirm_event = FakeEvent(
            "1",
            "p:FriendMessage:1",
            message_str=f"确认发送 {created['confirmation_code']}",
        )
        with patch(
            "astrbot_plugin_email_assistant.draft_service.send_draft_message",
            side_effect=RuntimeError("connection lost"),
        ) as send:
            failed = json.loads(
                await self.plugin.tool_confirm_send(
                    confirm_event, created["draft_id"], created["confirmation_code"]
                )
            )
            retried = json.loads(
                await self.plugin.tool_confirm_send(
                    confirm_event, created["draft_id"], created["confirmation_code"]
                )
            )
        self.assertFalse(failed["success"])
        self.assertIn("不会自动重试", failed["error"])
        self.assertFalse(retried["success"])
        self.assertEqual(index.get_draft(created["draft_id"]).status, "failed")
        send.assert_called_once()

    async def test_llm_write_tools_are_disabled_by_default_and_reject_cron(self):
        self._enable_test_index()
        self.account.update({"email": "bot@example.com", "password": "secret"})
        normal = FakeEvent("1", "p:FriendMessage:1", message_str="写邮件")
        disabled = json.loads(
            await self.plugin.tool_create_draft(
                normal, "reader@example.com", "主题", "正文", "one"
            )
        )
        self.assertFalse(disabled["success"])

        self.plugin.config["llm_mail_write_enabled"] = True
        cron = FakeEvent(
            "1",
            "p:FriendMessage:1",
            platform_name="cron",
            extras={"cron_job": {"id": "job"}},
            message_str="写邮件",
        )
        denied = json.loads(
            await self.plugin.tool_create_draft(
                cron, "reader@example.com", "主题", "正文", "one"
            )
        )
        self.assertFalse(denied["success"])

    async def test_local_index_sync_drives_header_query(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        header = ParsedMail(
            21,
            "索引主题",
            "发件人",
            "sender@example.com",
            "sender@example.com",
            "2026-07-16 10:00:00",
            timestamp,
            "",
            False,
            "",
            "",
        )
        sync_result = HeaderSyncResult(10, 22, 21, [header], None, False)
        with patch(
            "astrbot_plugin_email_assistant.main.sync_headers",
            return_value=sync_result,
        ), patch("astrbot_plugin_email_assistant.main.query_since") as remote_query:
            results = await self.plugin._query_mail_headers(
                self.account, datetime(2026, 7, 1), 10
            )
        self.assertEqual([item.uid for item in results], [21])
        remote_query.assert_not_called()
        self.assertEqual(index.stats("one", "INBOX")["active"], 1)

    async def test_background_discovers_and_progressively_indexes_folders(self):
        index = self._enable_test_index()
        folders = [
            MailFolder("INBOX", "INBOX", "/", (), True, "inbox"),
            MailFolder("Archive", "Archive", "/", (), True, "archive"),
            MailFolder("Sent", "Sent", "/", (), True, "sent"),
        ]
        with patch(
            "astrbot_plugin_email_assistant.main.list_imap_folders",
            return_value=folders,
        ):
            discovered = await self.plugin._refresh_folder_catalog(
                self.account, force=True
            )
        self.assertEqual([item.name for item in discovered], ["INBOX", "Archive", "Sent"])

        sync = AsyncMock()
        with patch.object(self.plugin, "_sync_account_index", sync):
            await self.plugin._sync_secondary_folder_step(self.account)
        sync.assert_awaited_once_with(self.account, folder="Archive")
        self.assertEqual(
            [item.name for item in index.list_folders("one")],
            ["INBOX", "Archive", "Sent"],
        )

    async def test_deleted_cloud_mail_is_marked_missing_on_detail(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        header = ParsedMail(
            22,
            "已删除",
            "",
            "sender@example.com",
            "sender@example.com",
            "2026-07-16 10:00:00",
            timestamp,
            "",
            False,
            "",
            "",
        )
        index.apply_sync("one", "INBOX", 10, 22, [header])
        index.cache_body("one", "INBOX", 10, 22, "旧缓存", 1024)
        index.cache_ai_result(
            "one", "INBOX", 10, 22, "old-hash", "summary:v2:test", "简体中文", "旧总结", "p"
        )
        with patch(
            "astrbot_plugin_email_assistant.main.fetch_detail_checked",
            side_effect=MailNotFoundError("missing"),
        ):
            with self.assertRaisesRegex(MailNotFoundError, "云端删除"):
                await self.plugin._fetch_remote_detail(self.account, 22)
        self.assertEqual(index.stats("one", "INBOX")["remote_missing"], 1)
        self.assertIsNone(index.get_cached_body("one", "INBOX", 10, 22))
        self.assertIsNone(
            index.get_ai_result(
                "one", "INBOX", 10, 22, "old-hash", "summary:v2:test", "简体中文"
            )
        )

    async def test_remote_detail_is_cached_on_demand(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        header = ParsedMail(
            25, "正文缓存", "", "sender@example.com", "sender@example.com",
            "2026-07-16 10:00:00", timestamp, "", False, "", ""
        )
        detail = ParsedMail(
            25, "正文缓存", "", "sender@example.com", "sender@example.com",
            "2026-07-16 10:00:00", timestamp, "完整正文", False, "", ""
        )
        index.apply_sync("one", "INBOX", 10, 25, [header])
        index.cache_ai_result(
            "one", "INBOX", 10, 25, "old-content", "summary:v2:test", "简体中文", "旧总结", "p"
        )
        with patch(
            "astrbot_plugin_email_assistant.main.fetch_detail_checked",
            return_value=(10, detail),
        ):
            result = await self.plugin._fetch_remote_detail(self.account, 25)
        self.assertEqual(result.body, "完整正文")
        cached = index.get_cached_body("one", "INBOX", 10, 25)
        self.assertEqual(cached.body_text, "完整正文")
        self.assertIsNone(
            index.get_ai_result(
                "one", "INBOX", 10, 25, "old-content", "summary:v2:test", "简体中文"
            )
        )

    async def test_transient_detail_error_does_not_mark_mail_missing(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        header = ParsedMail(
            23,
            "仍然有效",
            "",
            "sender@example.com",
            "sender@example.com",
            "2026-07-16 10:00:00",
            timestamp,
            "",
            False,
            "",
            "",
        )
        index.apply_sync("one", "INBOX", 10, 23, [header])
        with patch(
            "astrbot_plugin_email_assistant.main.fetch_detail_checked",
            side_effect=TimeoutError("timeout"),
        ):
            with self.assertRaises(TimeoutError):
                await self.plugin._fetch_remote_detail(self.account, 23)
        self.assertEqual(index.stats("one", "INBOX")["active"], 1)
        self.assertEqual(index.stats("one", "INBOX")["remote_missing"], 0)

    async def test_index_query_uses_cache_when_cloud_sync_fails(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        header = ParsedMail(
            24,
            "缓存主题",
            "",
            "sender@example.com",
            "sender@example.com",
            "2026-07-16 10:00:00",
            timestamp,
            "",
            False,
            "",
            "",
        )
        index.apply_sync("one", "INBOX", 10, 24, [header])
        with patch(
            "astrbot_plugin_email_assistant.main.sync_headers",
            side_effect=TimeoutError("sync timeout"),
        ):
            results = await self.plugin._query_mail_headers(
                self.account, datetime(2026, 7, 1), 10
            )
        self.assertEqual([item.uid for item in results], [24])
        self.assertIn("sync timeout", self.plugin._index_warnings["one"])

    async def test_uidvalidity_change_resets_notification_cursor(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        index.apply_sync("one", "INBOX", 10, 8, [
            ParsedMail(8, "旧代", "", "a@example.com", "a@example.com", "2026-07-16", timestamp, "", False, "", "")
        ])
        await self.plugin.put_kv_data(self.plugin._cursor_key(self.account), 8)
        fresh = ParsedMail(1, "新代", "", "a@example.com", "a@example.com", "2026-07-16", timestamp, "", False, "", "")
        result = HeaderSyncResult(
            11, 6, 1, [fresh], {1}, True,
            history_before_uid=1, history_complete=True
        )
        with patch(
            "astrbot_plugin_email_assistant.main.sync_headers", return_value=result
        ):
            await self.plugin._sync_account_index(
                self.account, force_reconcile=True
            )
        self.assertEqual(
            await self.plugin.get_kv_data(self.plugin._cursor_key(self.account)), 5
        )
        self.assertEqual(index.get_state("one", "INBOX").uidvalidity, 11)

    async def test_unchanged_index_sync_does_not_log_success(self):
        index = self._enable_test_index()
        existing = ParsedMail(
            8, "已有邮件", "", "a@example.com", "a@example.com", "", 0,
            "", False, "", ""
        )
        index.apply_sync("one", "INBOX", 10, 8, [existing])
        result = HeaderSyncResult(10, 9, 8, [existing], None, False)
        with patch(
            "astrbot_plugin_email_assistant.main.sync_headers", return_value=result
        ), patch(
            "astrbot_plugin_email_assistant.main.logger.info"
        ) as info, patch(
            "astrbot_plugin_email_assistant.main.logger.debug"
        ) as debug:
            await self.plugin._sync_account_index(self.account)
        info.assert_not_called()
        debug.assert_called_once()
        self.assertIn("同步无变化", debug.call_args.args[0])

    async def test_llm_read_tools_reject_cron_events(self):
        event = FakeEvent(
            "1",
            "p:FriendMessage:1",
            platform_name="cron",
            extras={"cron_job": {"id": "job"}},
        )
        accounts = json.loads(await self.plugin.tool_list_accounts(event))
        listing = json.loads(await self.plugin.tool_list_messages(event, "one"))
        latest = json.loads(await self.plugin.tool_get_latest_message(event, "one"))
        detail = json.loads(
            await self.plugin.tool_show_message(event, 1, account="one")
        )
        self.assertFalse(accounts["success"])
        self.assertFalse(listing["success"])
        self.assertFalse(latest["success"])
        self.assertFalse(detail["success"])

    async def test_llm_read_tools_enforce_owner_and_query_switch(self):
        other_user = FakeEvent("2", "p:FriendMessage:2")
        denied = json.loads(await self.plugin.tool_list_messages(other_user, "one"))
        self.assertFalse(denied["success"])

        self.account["query_enabled"] = False
        owner = FakeEvent("1", "p:FriendMessage:1")
        disabled = json.loads(
            await self.plugin.tool_show_message(owner, 1, account="one")
        )
        self.assertFalse(disabled["success"])

    async def test_page_api_registers_routes_and_lists_indexed_messages(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        index.apply_sync(
            "one", "INBOX", 10, 2,
            [
                ParsedMail(1, "第一封", "A", "a@example.com", "a@example.com", "", timestamp, "", False, "", ""),
                ParsedMail(2, "第二封", "B", "b@example.com", "b@example.com", "", timestamp + 60, "", False, "", ""),
            ],
        )
        routes = []
        self.context.register_web_api = lambda *args: routes.append(args)
        api = EmailAssistantPageApi(self.plugin)
        api.register_routes()
        self.assertIn("/astrbot_plugin_email_assistant/messages", [item[0] for item in routes])

        fake_request = FakePageRequest()
        fake_request.query = {"account_id": "one", "limit": "1"}
        with patch.object(page_api_module, "request", fake_request):
            response = await api.list_messages()
        self.assertEqual(response["status"], "ok")
        self.assertEqual([item["uid"] for item in response["data"]["items"]], [2])
        self.assertTrue(response["data"]["has_more"])

        with self.assertRaises(PermissionError):
            api._resolve_account("one", capability="organize_enabled")

    async def test_page_draft_requires_approval_and_sends_once(self):
        index = self._enable_test_index()
        self.account.update(
            {
                "email": "bot@example.com",
                "password": "secret",
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_security": "ssl",
            }
        )
        api = EmailAssistantPageApi(self.plugin)
        fake_request = FakePageRequest()
        fake_request.payload = {
            "account_id": "one",
            "to_addrs": "reader@example.com",
            "subject": "测试主题",
            "body_text": "测试正文",
        }
        with patch.object(page_api_module, "request", fake_request):
            created_response = await api.create_draft()
        created = created_response["data"]

        fake_request.query = {}
        with patch.object(page_api_module, "request", fake_request):
            list_response = await api.list_drafts()
        self.assertNotIn("body_text", list_response["data"]["items"][0])
        fake_request.query = {"draft_id": created["draft_id"]}
        with patch.object(page_api_module, "request", fake_request):
            detail_response = await api.get_draft()
        self.assertEqual(detail_response["data"]["body_text"], "测试正文")

        fake_request.payload = {
            "draft_id": created["draft_id"],
            "revision": created["revision"],
        }
        with patch.object(page_api_module, "request", fake_request):
            approved_response = await api.approve_draft()
        approved = approved_response["data"]
        self.assertEqual(approved["status"], "approved")

        fake_request.payload = {
            "draft_id": approved["draft_id"],
            "revision": approved["revision"],
        }
        with patch.object(page_api_module, "request", fake_request), patch.object(
            sys.modules["astrbot_plugin_email_assistant.draft_service"],
            "send_draft_message",
        ) as send:
            sent_response = await api.send_draft()
            duplicate_response = await api.send_draft()
        self.assertEqual(sent_response["data"]["status"], "sent")
        self.assertEqual(duplicate_response["status"], "error")
        send.assert_called_once()
        self.assertEqual(index.get_draft(created["draft_id"]).status, "sent")

    async def test_page_translation_reuses_content_cache(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        message = ParsedMail(
            8, "Subject", "Sender", "a@example.com", "a@example.com",
            "2026-07-16", timestamp, "Mail body", False, "", ""
        )
        index.apply_sync("one", "INBOX", 10, 8, [message])
        self.context.provider = FakeProvider("翻译结果")
        api = EmailAssistantPageApi(self.plugin)
        fake_request = FakePageRequest()
        fake_request.payload = {
            "account_id": "one",
            "folder": "INBOX",
            "uid": 8,
            "locale": "zh-CN",
        }
        remote_detail = AsyncMock(return_value=message)
        with patch.object(page_api_module, "request", fake_request), patch.object(
            self.plugin, "_fetch_remote_detail", new=remote_detail
        ):
            first = await api.translate_message()
            second = await api.translate_message()
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["data"]["cached"])
        self.assertTrue(second["data"]["cached"])
        self.assertEqual(second["data"]["content"], "翻译结果")
        self.assertEqual(len(self.context.provider.calls), 1)
        remote_detail.assert_awaited_once()

    async def test_page_forced_translation_uses_requested_language_and_replaces_cache(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        message = ParsedMail(
            9, "Subject", "Sender", "a@example.com", "a@example.com",
            "2026-07-16", timestamp, "Mail body", False, "", ""
        )
        index.apply_sync("one", "INBOX", 10, 9, [message])
        index.cache_ai_result(
            "one", "INBOX", 10, 9, mail_content_hash(message),
            self.plugin._mail_processing_cache_key("translate"),
            "简体中文", "旧中文缓存", "old-provider",
        )
        self.context.provider = FakeProvider("English result")
        api = EmailAssistantPageApi(self.plugin)
        fake_request = FakePageRequest()
        fake_request.payload = {
            "account_id": "one", "folder": "INBOX", "uid": 9,
            "locale": "zh-CN", "target_language": "English", "force": True,
        }
        with patch.object(page_api_module, "request", fake_request), patch.object(
            self.plugin, "_fetch_remote_detail", new=AsyncMock(return_value=message)
        ):
            result = await api.translate_message()
            fake_request.payload = {
                "account_id": "one", "folder": "INBOX", "uid": 9,
                "locale": "zh-CN",
            }
            normal_click = await api.translate_message()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["cached"])
        self.assertEqual(result["data"]["target_language"], "English")
        self.assertTrue(normal_click["data"]["cached"])
        self.assertEqual(normal_click["data"]["content"], "English result")
        self.assertEqual(normal_click["data"]["target_language"], "English")
        self.assertEqual(len(self.context.provider.calls), 1)

        fake_request.query = {
            "account_id": "one", "folder": "INBOX", "uid": 9,
            "locale": "zh-CN", "task": "translate",
        }
        with patch.object(page_api_module, "request", fake_request):
            cached = await api.get_message_ai_cache()
        self.assertTrue(cached["data"]["available"])
        self.assertEqual(cached["data"]["content"], "English result")
        self.assertEqual(cached["data"]["target_language"], "English")

    async def test_page_cached_detail_returns_before_separate_verification(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        cached_mail = ParsedMail(
            18, "缓存主题", "Sender", "a@example.com", "a@example.com",
            "2026-07-16", timestamp, "缓存正文", False, "", ""
        )
        changed_mail = ParsedMail(
            18, "更新主题", "Sender", "a@example.com", "a@example.com",
            "2026-07-16", timestamp, "更新正文", False, "", ""
        )
        index.apply_sync("one", "INBOX", 10, 18, [cached_mail])
        index.cache_body("one", "INBOX", 10, 18, cached_mail.body, 1024)
        api = EmailAssistantPageApi(self.plugin)
        fake_request = FakePageRequest()
        fake_request.query = {"account_id": "one", "folder": "INBOX", "uid": 18}
        with patch.object(page_api_module, "request", fake_request):
            cached_response = await api.get_cached_message()
        self.assertEqual(cached_response["status"], "ok")
        self.assertTrue(cached_response["data"]["from_cache"])
        self.assertEqual(cached_response["data"]["body"], "缓存正文")

        fake_request.payload = {"account_id": "one", "folder": "INBOX", "uid": 18}
        with patch.object(page_api_module, "request", fake_request), patch.object(
            self.plugin, "_fetch_remote_detail", return_value=changed_mail
        ):
            verification = await api.verify_message()
        self.assertEqual(
            verification["data"]["verification_status"], "changed"
        )
        self.assertFalse(verification["data"]["verification_cached"])

    async def test_page_current_verification_uses_thirty_second_cooldown(self):
        index = self._enable_test_index()
        timestamp = datetime(2026, 7, 16, 10, 0).timestamp()
        message = ParsedMail(
            20, "未变化", "Sender", "a@example.com", "a@example.com",
            "2026-07-16", timestamp, "缓存正文", False, "", "",
        )
        index.apply_sync("one", "INBOX", 10, 20, [message])
        index.cache_body("one", "INBOX", 10, 20, message.body, 1024)
        api = EmailAssistantPageApi(self.plugin)
        fake_request = FakePageRequest()
        fake_request.payload = {
            "account_id": "one", "folder": "INBOX", "uid": 20,
        }
        with patch.object(page_api_module, "request", fake_request), patch.object(
            self.plugin, "_fetch_remote_detail", return_value=message
        ) as fetch:
            first = await api.verify_message()
            repeated = await api.verify_message()
        self.assertEqual(first["data"]["verification_status"], "current")
        self.assertFalse(first["data"]["verification_cached"])
        self.assertTrue(repeated["data"]["verification_cached"])
        fetch.assert_awaited_once()

    async def test_page_concurrent_remote_detail_requests_are_merged(self):
        message = ParsedMail(
            19, "并发正文", "Sender", "a@example.com", "a@example.com",
            "2026-07-16", datetime(2026, 7, 16, 10, 0).timestamp(),
            "body", False, "", "",
        )
        api = EmailAssistantPageApi(self.plugin)
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_detail(*_args):
            started.set()
            await release.wait()
            return message

        with patch.object(
            self.plugin, "_fetch_remote_detail", new=AsyncMock(side_effect=delayed_detail)
        ) as fetch:
            first = asyncio.create_task(
                api._fetch_detail_shared(self.account, "INBOX", 19)
            )
            await started.wait()
            second = asyncio.create_task(
                api._fetch_detail_shared(self.account, "INBOX", 19)
            )
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second)
        self.assertEqual([item.uid for item in results], [19, 19])
        fetch.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
