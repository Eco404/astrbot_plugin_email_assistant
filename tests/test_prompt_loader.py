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
        self.assertEqual(schema["narration_prompt"]["default"], "")
        self.assertEqual(schema["mail_summary_prompt"]["default"], "")
        self.assertEqual(schema["mail_translation_prompt"]["default"], "")


if __name__ == "__main__":
    unittest.main()
