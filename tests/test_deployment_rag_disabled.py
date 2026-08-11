import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from applications import bootstrap


ROOT = Path(__file__).resolve().parent.parent


class DeploymentRagDisabledTests(unittest.TestCase):
    def test_dockerfile_defaults_to_lightweight_deploy_requirements(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("ARG REQUIREMENTS_FILE=requirements-deploy.txt", dockerfile)
        self.assertIn("ARG INSTALL_RAG_DEPS=0", dockerfile)
        self.assertIn("RAG_ENABLED=0", dockerfile)
        self.assertIn("requirements-rag.txt", dockerfile)

    def test_dockerfile_installs_coding_runtime_tools(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        installed_lines = {
            line.strip().removesuffix("\\").strip()
            for line in dockerfile.splitlines()
        }

        for package in (
            "bash",
            "ca-certificates",
            "curl",
            "file",
            "git",
            "jq",
            "openssh-client",
            "patch",
            "procps",
            "ripgrep",
            "unzip",
        ):
            self.assertIn(package, installed_lines)
        self.assertIn("rm -rf /var/lib/apt/lists/*", dockerfile)

    def test_compose_keeps_qdrant_behind_rag_profile(self) -> None:
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]

        self.assertEqual(["rag"], services["qdrant"]["profiles"])
        agent = services["agent-console"]
        self.assertEqual("${REQUIREMENTS_FILE:-requirements-deploy.txt}", agent["build"]["args"]["REQUIREMENTS_FILE"])
        self.assertEqual("${INSTALL_RAG_DEPS:-0}", agent["build"]["args"]["INSTALL_RAG_DEPS"])
        self.assertEqual("${RAG_ENABLED:-0}", agent["environment"]["RAG_ENABLED"])
        self.assertEqual("${SECURITY_RAG_PLUGIN_ENABLED:-0}", agent["environment"]["SECURITY_RAG_PLUGIN_ENABLED"])

    def test_runtime_global_rag_gate_disables_vector_and_security_rag(self) -> None:
        env = {
            "RAG_ENABLED": "0",
            "HISTORY_VECTOR_ENABLED": "1",
            "SECURITY_RAG_AUTO_CONTEXT_ENABLED": "1",
            "SECURITY_RAG_PLUGIN_ENABLED": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(bootstrap._history_vector_enabled())
            self.assertFalse(bootstrap._security_rag_auto_context_enabled())
            self.assertFalse(bootstrap._security_rag_plugin_enabled())

    def test_memory_lifecycle_is_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(bootstrap._env_bool("MEMORY_LIFECYCLE_ENABLED", False))
        with patch.dict(os.environ, {"MEMORY_LIFECYCLE_ENABLED": "1"}, clear=True):
            self.assertTrue(bootstrap._env_bool("MEMORY_LIFECYCLE_ENABLED", False))


if __name__ == "__main__":
    unittest.main()
