import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plugins.markdown_pdf.plugin import MarkdownPdfPlugin


def _reportlab_available() -> bool:
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


class MarkdownPdfPluginTests(unittest.TestCase):
    def _plugin(self, workspace: Path) -> MarkdownPdfPlugin:
        plugin = MarkdownPdfPlugin()
        plugin.setup(SimpleNamespace(workspace=workspace))
        return plugin

    def test_tool_is_visible_in_bot_and_coding_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registration = self._plugin(Path(tmp)).tools()[0]

        self.assertEqual("markdown_to_pdf", registration.schema["function"]["name"])
        self.assertEqual({"bot", "coding"}, registration.enabled_modes)
        self.assertTrue(registration.always_on)
        self.assertEqual("normal", registration.risk)

    def test_rejects_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin(Path(tmp))

            with self.assertRaisesRegex(ValueError, "inside the workspace"):
                plugin.markdown_to_pdf("../private.md")

    def test_rejects_existing_destination_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.md").write_text("# note", encoding="utf-8")
            output = workspace / "storage" / "generated" / "note.pdf"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"already-here")
            plugin = self._plugin(workspace)

            with self.assertRaisesRegex(ValueError, "overwrite=true"):
                plugin.markdown_to_pdf("note.md")

    def test_rejects_source_larger_than_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.md").write_text("12345", encoding="utf-8")
            with patch.dict("os.environ", {"MARKDOWN_PDF_MAX_BYTES": "4"}):
                plugin = self._plugin(workspace)

            with self.assertRaisesRegex(ValueError, "too large"):
                plugin.markdown_to_pdf("note.md")

    def test_render_failure_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.md").write_text("# note", encoding="utf-8")
            plugin = self._plugin(workspace)

            with patch(
                "plugins.markdown_pdf.plugin.render_markdown_pdf",
                side_effect=RuntimeError("broken renderer"),
            ):
                with self.assertRaisesRegex(RuntimeError, "broken renderer"):
                    plugin.markdown_to_pdf("note.md")

            generated = workspace / "storage" / "generated"
            self.assertFalse((generated / "note.pdf").exists())
            self.assertEqual([], list(generated.iterdir()))

    @unittest.skipUnless(_reportlab_available(), "reportlab is not installed")
    def test_converts_markdown_with_chinese_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.md").write_text(
                "# 标题\n\n一段 **内容**。\n\n- 第一项\n- 第二项\n\n```html\n<div>ok</div>\n```\n",
                encoding="utf-8",
            )
            plugin = self._plugin(workspace)

            result = json.loads(plugin.markdown_to_pdf("note.md", title="测试文档"))
            output = workspace / result["output_path"]

            self.assertEqual("created", result["status"])
            self.assertEqual("storage/generated/note.pdf", result["output_path"])
            self.assertGreater(result["bytes"], 100)
            self.assertTrue(output.read_bytes().startswith(b"%PDF"))

    @unittest.skipUnless(_reportlab_available(), "reportlab is not installed")
    def test_embeds_safe_local_image_and_skips_remote_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            image_dir = workspace / "assets"
            image_dir.mkdir()
            (image_dir / "pixel.png").write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
                "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
            ))
            (workspace / "note.md").write_text(
                "![本地图片](assets/pixel.png)\n\n"
                "![远程图片](https://example.com/image.png)\n",
                encoding="utf-8",
            )
            plugin = self._plugin(workspace)

            result = json.loads(plugin.markdown_to_pdf("note.md"))

            self.assertEqual(1, result["local_images"])
            self.assertEqual(1, result["skipped_images"])
