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
        self.assertNotIn(".innerHTML", renderer)

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


if __name__ == "__main__":
    unittest.main()
