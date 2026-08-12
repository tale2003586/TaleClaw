import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from runtime.context import ContextBuilder, PromptAssetsService
from runtime.context.budget import ContextBudgeter
from runtime.sessions.session import Session
from skill_runtime.loader import SkillLoader


class SkillRuntimeTests(unittest.TestCase):
    def test_loader_refreshes_and_normalizes_extended_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skill_dir = skills_dir / "review"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "---\n"
                "name: review\n"
                "description: Review code carefully.\n"
                "tags: [code, audit]\n"
                "triggers:\n"
                "  - review\n"
                "  - audit\n"
                "applies_to: [coding]\n"
                "requires_tools: [rg, read_file]\n"
                "priority: 10\n"
                "safety:\n"
                "  risk: low\n"
                "---\n"
                "# Review\n",
                encoding="utf-8",
            )

            loader = SkillLoader(skills_dir)
            descriptions = loader.get_descriptions()
            catalog = loader.get_catalog()

            self.assertIn("review: Review code carefully.", descriptions)
            self.assertIn("tags=[code, audit]", descriptions)
            self.assertIn("triggers=[review, audit]", descriptions)
            self.assertIn("tools=[rg, read_file]", descriptions)
            self.assertEqual(["review", "audit"], catalog[0]["triggers"])
            self.assertEqual(["rg", "read_file"], catalog[0]["requires_tools"])
            self.assertEqual({"risk": "low"}, catalog[0]["safety"])

            skill_file.write_text(
                "---\n"
                "name: review\n"
                "description: Updated review workflow.\n"
                "tags: code, audit\n"
                "---\n"
                "# Review updated\n",
                encoding="utf-8",
            )

            self.assertIn("Updated review workflow.", loader.get_descriptions())
            self.assertIn("Review updated", loader.get_content("review"))

            skill_file.unlink()
            self.assertEqual("(no skills available)", loader.get_descriptions())

    def test_context_does_not_eagerly_inject_installed_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skill_dir = skills_dir / "pdf"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "---\n"
                "name: pdf\n"
                "description: Process PDFs.\n"
                "triggers: [pdf]\n"
                "---\n"
                "# PDF\n",
                encoding="utf-8",
            )
            budgeter = ContextBudgeter.from_env()
            builder = ContextBuilder(
                budgeter=budgeter,
                prompt_assets_service=PromptAssetsService(
                    budgeter=budgeter,
                ),
            )

            context = builder.build(
                session=Session(id="web:test"),
                agent_spec=SimpleNamespace(instructions="base", tool_mode="bot"),
            )

            system = context.messages[0]["content"]
            self.assertNotIn("<skill-catalog>", system)
            self.assertNotIn("pdf: Process PDFs.", system)
            report = context.report.to_dict()
            self.assertNotIn("skill_catalog", report["sections"])


if __name__ == "__main__":
    unittest.main()
