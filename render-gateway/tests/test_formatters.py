"""Tests des formatters : protection du code, LaTeX et découpage Discord."""

import unittest
from unittest.mock import AsyncMock, patch

from bots.telegram_bot import markdown_to_telegram_html
from formatters import latex_to_unicode, smart_split_discord
from formatters.latex_renderer import extract_and_render_latex


class TestLatexCodeProtection(unittest.TestCase):
    def test_inline_code_is_not_converted(self):
        text = "Le code `$x^2$` reste intact, mais la formule $x^2$ devient x²."
        self.assertEqual(
            latex_to_unicode(text),
            "Le code `$x^2$` reste intact, mais la formule x² devient x².",
        )

    def test_fenced_code_is_not_converted(self):
        text = "```python\nprix = '$x^2$'\n```"
        self.assertEqual(latex_to_unicode(text), text)

    def test_code_block_with_block_equation_is_preserved(self):
        text = "```python\n# $$E=mc^2$$\nprint('ok')\n```"
        self.assertEqual(latex_to_unicode(text), text)


class TestExtractAndRenderLatex(unittest.IsolatedAsyncioTestCase):
    async def test_equation_inside_code_block_is_not_rendered(self):
        text = "```python\n# $$E=mc^2$$\n```\n$$a^2+b^2=c^2$$\n"

        with patch(
            "formatters.latex_renderer.render_latex_to_png",
            new=AsyncMock(return_value=b"\x89PNG" + b"x" * 200),
        ) as render:
            updated, images = await extract_and_render_latex(text)

        self.assertIn("```python\n# $$E=mc^2$$\n```", updated)
        self.assertEqual([image["formula"] for image in images], ["a^2+b^2=c^2"])
        render.assert_awaited_once_with("a^2+b^2=c^2", dark_mode=True)


class TestTelegramHtml(unittest.TestCase):
    def test_code_block_keeps_language_and_math(self):
        html = markdown_to_telegram_html(
            "```python\nprix = '$x^2$'\n```"
        )
        self.assertIn('<pre><code class="language-python">', html)
        self.assertIn("$x^2$", html)

    def test_inline_math_is_converted_outside_code(self):
        html = markdown_to_telegram_html("La formule $x^2$ est utile.")
        self.assertIn("x²", html)


class TestSmartSplitDiscord(unittest.TestCase):
    def test_long_line_is_split_below_limit(self):
        parts = smart_split_discord("A" * 5000, max_chars=1950)
        self.assertTrue(all(len(part) <= 1950 for part in parts))
        self.assertGreater(len(parts), 1)

    def test_code_fences_are_balanced_in_each_part(self):
        text = (
            "intro\n```python\n"
            + "\n".join(f"line{i} = {i}" for i in range(400))
            + "\n```\nfin"
        )
        parts = smart_split_discord(text, max_chars=1950)

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 1950 for part in parts))
        self.assertTrue(all(part.count("```") % 2 == 0 for part in parts))


if __name__ == "__main__":
    unittest.main()
