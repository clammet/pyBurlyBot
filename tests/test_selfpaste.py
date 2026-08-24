from unittest import TestCase

from pyburlybot_modules.selfpaste import _html_paste


class SelfPasteTests(TestCase):
    def test_html_paste_references_packaged_stylesheet(self) -> None:
        rendered = _html_paste("https://example.test", "Example")

        self.assertIn('<link rel="stylesheet" href="style/style.css">', rendered)
