import json
import tempfile
import unittest
from pathlib import Path

from knowledge.chunking import ChunkingRouter, JsonAdvisoryChunking, MarkdownDocChunking, SemgrepYamlChunking
from knowledge.security_rag import chunks_from_file


class SecurityRagChunkingTests(unittest.TestCase):
    def test_chunking_router_loads_and_routes_by_source_type(self) -> None:
        router = ChunkingRouter()

        self.assertIsInstance(router.strategy_for(Path("advisory-database/advisories/GHSA-x.json")), JsonAdvisoryChunking)
        self.assertIsInstance(router.strategy_for(Path("semgrep-rules/python/security/rule.yaml")), SemgrepYamlChunking)
        self.assertIsInstance(router.strategy_for(Path("docs/security.md")), MarkdownDocChunking)

    def test_json_advisory_chunks_keep_header_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "advisory-database" / "advisories" / "GHSA-test.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.4.0",
                        "id": "GHSA-abcd-efgh-ijkl",
                        "aliases": ["CVE-2026-12345"],
                        "summary": "Path traversal in demo package",
                        "details": "First paragraph.\n\nSecond paragraph with remediation guidance.",
                        "severity": [{"type": "CVSS_V3", "score": "HIGH"}],
                        "affected": [
                            {
                                "package": {"ecosystem": "PyPI", "name": "demo"},
                                "ranges": [{"type": "ECOSYSTEM", "events": [{"fixed": "1.2.3"}]}],
                            }
                        ],
                        "references": [{"type": "ADVISORY", "url": "https://example.test/advisory"}],
                        "database_specific": {"cwe_ids": ["CWE-22"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            chunks = chunks_from_file(path, root=root, chunk_chars=80)

        self.assertGreaterEqual(len(chunks), 4)
        self.assertTrue(all(chunk.source_type == "advisory" for chunk in chunks))
        self.assertTrue(all("ID: GHSA-abcd-efgh-ijkl" in chunk.text for chunk in chunks))
        self.assertTrue(all("Aliases: CVE-2026-12345" in chunk.text for chunk in chunks))
        self.assertEqual("GHSA-abcd-efgh-ijkl", chunks[0].metadata["advisory_id"])
        self.assertEqual(["CVE-2026-12345"], chunks[0].metadata["aliases"])
        self.assertEqual(["PyPI:demo"], chunks[0].metadata["packages"])
        self.assertEqual(["CWE-22"], chunks[0].metadata["cwes"])
        self.assertIn("summary", {chunk.metadata["field"] for chunk in chunks})
        self.assertIn("affected", {chunk.metadata["field"] for chunk in chunks})

    def test_semgrep_yaml_chunks_by_rule_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "semgrep-rules" / "python" / "security" / "rules.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(
                """
rules:
  - id: python.demo.injection
    message: Avoid shell=True with user input
    severity: WARNING
    languages: [python]
    pattern: subprocess.call($X, shell=True)
    metadata:
      cwe: CWE-78
      category: security
  - id: python.demo.path-traversal
    message: Validate file paths before reading
    severity: ERROR
    languages: [python]
    pattern: open($PATH)
    metadata:
      cwe: CWE-22
      category: security
""",
                encoding="utf-8",
            )

            chunks = chunks_from_file(path, root=root)

        self.assertEqual(2, len(chunks))
        self.assertEqual({"python.demo.injection", "python.demo.path-traversal"}, {chunk.title for chunk in chunks})
        self.assertTrue(all(chunk.source_type == "semgrep_rule" for chunk in chunks))
        self.assertTrue(all(chunk.metadata["corpus_type"] == "semgrep_rule" for chunk in chunks))
        first = next(chunk for chunk in chunks if chunk.title == "python.demo.injection")
        self.assertEqual(["CWE-78"], first.metadata["cwe"])
        self.assertIn("Rule ID: python.demo.injection", first.text)

    def test_markdown_chunks_use_heading_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "guide.md"
            path.write_text(
                "# Web Security\n\nIntro\n\n## XSS\n\nEncode output.\n\n### HTML Context\n\nUse escaping.",
                encoding="utf-8",
            )

            chunks = chunks_from_file(path, root=root, chunk_chars=500)

        headings = [chunk.metadata["heading"] for chunk in chunks]
        self.assertIn("Web Security", headings)
        self.assertIn("Web Security > XSS", headings)
        self.assertIn("Web Security > XSS > HTML Context", headings)


if __name__ == "__main__":
    unittest.main()
