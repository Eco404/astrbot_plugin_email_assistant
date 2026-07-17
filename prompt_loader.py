from __future__ import annotations

import json
from pathlib import Path


PROMPTS_PATH = Path(__file__).with_name("prompts.json")
REQUIRED_PROMPTS = {
    "default_narration",
    "history_placeholder",
    "direct_narration_tool_output",
    "cron_narration_note",
    "email_tool_conversation",
    "tool_list_accounts_description",
    "tool_list_messages_description",
    "tool_get_latest_message_description",
    "tool_show_message_description",
    "tool_summarize_message_description",
    "tool_translate_message_description",
    "tool_create_draft_description",
    "tool_create_reply_draft_description",
    "tool_confirm_send_description",
    "tool_cancel_draft_description",
    "mail_content_system",
    "mail_summary",
    "mail_translate",
}


def load_prompts(path: Path = PROMPTS_PATH) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法加载邮件助手提示词文件 {path.name}：{exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"邮件助手提示词文件 {path.name} 的顶层必须是对象。")
    prompts = {
        str(key): str(value).strip()
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }
    missing = sorted(REQUIRED_PROMPTS - prompts.keys())
    if missing:
        raise RuntimeError(
            f"邮件助手提示词文件 {path.name} 缺少字段：{', '.join(missing)}"
        )
    return prompts


PROMPTS = load_prompts()


def get_prompt(name: str) -> str:
    try:
        return PROMPTS[name]
    except KeyError as exc:
        raise RuntimeError(f"邮件助手提示词不存在：{name}") from exc


def render_prompt(name: str, **values: str) -> str:
    try:
        return get_prompt(name).format_map(values)
    except KeyError as exc:
        raise RuntimeError(
            f"邮件助手提示词 {name} 缺少模板变量：{exc.args[0]}"
        ) from exc
