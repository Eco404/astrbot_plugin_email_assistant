import json
import sys
import unittest
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

from astrbot_plugin_email_assistant.prompt_loader import (
    REQUIRED_PROMPTS,
    get_prompt,
    load_prompts,
    render_prompt,
)


class PromptLoaderTests(unittest.TestCase):
    def test_prompt_file_contains_all_required_entries(self):
        prompts = load_prompts()
        self.assertTrue(REQUIRED_PROMPTS.issubset(prompts))

    def test_prompt_template_renders_named_value(self):
        rendered = render_prompt(
            "direct_narration_tool_output", narration_prompt="转述内容"
        )
        self.assertIn("转述内容", rendered)
        self.assertNotIn("{narration_prompt}", rendered)

    def test_configurable_prompt_fields_default_to_blank(self):
        schema = json.loads(
            (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["notification_settings"]["items"]["narration_prompt"]["default"],
            "",
        )
        mail_ai = schema["mail_ai_settings"]["items"]
        self.assertEqual(mail_ai["mail_summary_prompt"]["default"], "")
        self.assertEqual(mail_ai["mail_translation_prompt"]["default"], "")
        webui = schema["webui_settings"]["items"]
        self.assertEqual(
            webui["mail_verification_cooldown_minutes"]["default"], 5
        )
        account_fields = schema["mail_accounts"]["templates"]["email_account"][
            "items"
        ]
        self.assertNotIn("owner_umo", account_fields)


if __name__ == "__main__":
    unittest.main()
