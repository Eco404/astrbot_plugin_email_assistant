import hashlib
import unittest
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]


class WebAssetTests(unittest.TestCase):
    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_markdown_uses_local_parser_and_sanitizer(self):
        page = (PLUGIN_DIR / "pages/mailbox/index.html").read_text(
            encoding="utf-8"
        )
        renderer = (PLUGIN_DIR / "pages/mailbox/markdown.js").read_text(
            encoding="utf-8"
        )
        self.assertLess(page.index("markdown-it.min.js"), page.index("app.js"))
        self.assertLess(page.index("purify.min.js"), page.index("app.js"))
        self.assertIn("html: false", renderer)
        self.assertIn("RETURN_DOM_FRAGMENT: true", renderer)
        self.assertIn("ALLOW_DATA_ATTR: false", renderer)
        self.assertIn("normalizeMarkdownSource(source)", renderer)
        self.assertIn(r"[^\n*]*?\S", renderer)
        self.assertIn(r"[^\n*]*?[：:]", renderer)
        self.assertNotIn(".innerHTML", renderer)
        self.assertIn('src="./app.js?v=2.2.3"', page)
        self.assertIn('from "./markdown.js?v=2.2.3"', (PLUGIN_DIR / "pages/mailbox/app.js").read_text(encoding="utf-8"))

    def test_vendored_markdown_assets_match_locked_files(self):
        vendor = PLUGIN_DIR / "pages/mailbox/vendor"
        self.assertEqual(
            self._sha256(vendor / "markdown-it/markdown-it.min.js"),
            "70fe17bd06c7fa819f03a1ed10957904318103624198845dc893b309bf495e28",
        )
        self.assertEqual(
            self._sha256(vendor / "dompurify/purify.min.js"),
            "c45ba939765574f96cbf35ee9b6d89f73756a17921814425e74b82f7c54603ce",
        )
        self.assertTrue((vendor / "markdown-it/LICENSE").is_file())
        self.assertTrue((vendor / "dompurify/LICENSE-APACHE-2.0").is_file())
        self.assertTrue((vendor / "dompurify/LICENSE-MPL-2.0").is_file())

    def test_remote_body_read_updates_current_page_cache_state(self):
        app = (PLUGIN_DIR / "pages/mailbox/app.js").read_text(encoding="utf-8")
        self.assertIn("current.body_cached = true", app)
        self.assertIn("if (state.selectedUid !== uid) return", app)

    def test_ai_cache_auto_display_and_forced_language_controls_exist(self):
        app = (PLUGIN_DIR / "pages/mailbox/app.js").read_text(encoding="utf-8")
        page = (PLUGIN_DIR / "pages/mailbox/index.html").read_text(encoding="utf-8")
        self.assertIn('apiGet("message/ai-cache"', app)
        self.assertIn("showConfiguredCachedResult(message, aiResult)", app)
        self.assertIn("target_language: options.targetLanguage", app)
        self.assertIn("force: options.force === true", app)
        self.assertIn('className = "button secondary split-regenerate"', app)
        self.assertIn('id="processing-language-input"', page)


if __name__ == "__main__":
    unittest.main()
