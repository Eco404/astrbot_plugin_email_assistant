"""Verify host integration contracts offline against an explicit AstrBot checkout.

Run with that checkout's virtual-environment Python. Runtime configuration and
SQLite state are isolated in a temporary directory; no provider requests occur.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


async def verify(root: Path) -> dict:
    from astrbot.core import db_helper
    from astrbot.core.agent.message import Message, bind_checkpoint_messages
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.message.components import File
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.provider.sources.anthropic_source import ProviderAnthropic
    from astrbot.core.provider.sources.gemini_source import ProviderGoogleGenAI
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

    from astrbot_plugin_email_assistant.main import (
        EMAIL_LLM_TOOL_NAMES,
        EmailAssistantPlugin,
    )

    registered = {
        tool.name: tool for tool in llm_tools.func_list
        if tool.name.startswith("email_assistant_")
    }
    assert set(registered) == EMAIL_LLM_TOOL_NAMES
    assert len(registered) == 7
    schemas = {}
    for name in ("email_assistant_create_draft", "email_assistant_create_reply_draft"):
        schema = registered[name].parameters["properties"]["attachments"]
        assert schema["type"] == "array" and schema["items"] == {"type": "string"}
        schemas[name] = {"type": schema["type"], "items": schema["items"]}

    await db_helper.initialize()
    manager = ConversationManager(db_helper)
    plugin = object.__new__(EmailAssistantPlugin)
    plugin.context = SimpleNamespace(conversation_manager=manager)
    plugin.config = {"notification_settings": {"official_history_placeholder": "{none}"}}
    origin = "offline:FriendMessage:integration-check"
    try:
        await plugin._archive_narration(origin, "邮件通知内容")
        cid = await manager.get_curr_conversation_id(origin)
        stored = await db_helper.get_conversation_by_id(cid=cid)
        assert stored is not None
        assert stored.content[0] == {"role": "user", "content": ""}
        fresh_manager = ConversationManager(db_helper)
        reloaded = await fresh_manager.get_conversation(origin, cid)
        history = json.loads(reloaded.history)
        parsed = bind_checkpoint_messages(history)
        assert parsed[0].role == "user" and parsed[0].content == ""
        readable, _ = await fresh_manager.get_human_readable_context(origin, cid)
        assert "User: " in readable
        assert Message(role="user", content="").content == ""
        none_rejected = False
        try:
            Message(role="user", content=None)
        except ValueError:
            none_rejected = True
        assert none_rejected

        openai_payload = {"messages": copy.deepcopy(history)}
        ProviderOpenAIOfficial._sanitize_assistant_messages(openai_payload)
        assert openai_payload["messages"][0]["content"] == ""
        anthropic = object.__new__(ProviderAnthropic)
        _, anthropic_messages = anthropic._prepare_payload(copy.deepcopy(history))
        assert anthropic_messages[0]["content"] == ""
        gemini = object.__new__(ProviderGoogleGenAI)
        gemini_messages = await gemini._prepare_conversation({"messages": copy.deepcopy(history)})
        assert gemini_messages[0].parts[0].text == " "
        assert history[0]["content"] == ""

        local_file = root / "附件.bin"
        local_file.write_bytes(b"offline file component check")
        file_component = File(name="附件.bin", file=str(local_file))
        assert file_component.file_ == str(local_file)
        assert file_component.file == str(local_file.resolve())
        assert await file_component.get_file() == str(local_file.resolve())

        return {
            "registered_tools": sorted(registered),
            "attachment_schemas": schemas,
            "empty_history": {"stored": stored.content[0], "reloaded": parsed[0].model_dump(), "readable": readable, "null_rejected": none_rejected},
            "offline_provider_conversion": {"openai_user_content": openai_payload["messages"][0]["content"], "anthropic_user_content": anthropic_messages[0]["content"], "gemini_user_text": gemini_messages[0].parts[0].text},
            "file_component": {"name": file_component.name, "local_path_resolved": True},
            "live_provider_requests": False,
        }
    finally:
        await db_helper.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot-root", required=True, type=Path, help="AstrBot source checkout")
    args = parser.parse_args()
    if not (args.astrbot_root / "astrbot" / "core").is_dir():
        parser.error("--astrbot-root must identify an AstrBot checkout")
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(args.astrbot_root.resolve()), str(Path(__file__).resolve().parents[2])]
    with tempfile.TemporaryDirectory(prefix="email-assistant-host-") as directory:
        os.environ["ASTRBOT_ROOT"] = directory
        result = asyncio.run(verify(Path(directory)))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
