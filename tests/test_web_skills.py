import io
import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import Mock, patch

from skill_runtime import SkillLoader
from web.auth_store import AuthenticatedUser
from web.server import RequestHandler


class WebSkillSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "skills"
        self.file = self.root / "review" / "SKILL.md"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("---\nname: review\ndescription: Review code\n---\nOriginal body\n")
        self.loader = SkillLoader(self.root)
        self.patch = patch("skill_runtime.SKILL_LOADER", self.loader)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def request(self, path, *, method="GET", payload=None, role="admin", headers=None,
                authorized=True):
        handler = object.__new__(RequestHandler)
        handler.path = path
        body = json.dumps(payload).encode() if payload is not None else b""
        handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body)),
                           "Host": "localhost:8000", **(headers or {})}
        handler.rfile = io.BytesIO(body)
        handler.auth_user = AuthenticatedUser(user_id="test", role=role)
        handler._authorize = Mock(return_value=authorized)
        handler.agent_service = Mock()
        responses = []
        handler._send_json = lambda value, status=HTTPStatus.OK, headers=None: responses.append(
            (value, status, headers or {}))
        getattr(handler, "do_" + method)()
        handler.agent_service.ensure_started.assert_not_called()
        return responses

    def detail(self):
        return self.loader.get_document("review")

    def test_admin_lists_and_reads_skills_without_starting_runtime(self):
        payload, status, headers = self.request("/api/skills")[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual("review", payload["skills"][0]["id"])
        self.assertEqual("no-store", headers["Cache-Control"])
        document, status, _ = self.request("/api/skills/detail?id=review")[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(self.file.read_text(), document["content"])
        self.assertTrue(document["revision"])

    def test_regular_user_cannot_access_any_skill_management_route(self):
        for route, method, payload in [
            ("/api/skills", "GET", None),
            ("/api/skills/detail?id=review", "GET", None),
            ("/api/skills/reload", "POST", {}),
            ("/api/skills/update", "POST", {"role": "admin", "enabled": False}),
        ]:
            with self.subTest(route=route):
                self.assertEqual(HTTPStatus.FORBIDDEN,
                                 self.request(route, method=method, payload=payload, role="user")[0][1])
        self.assertIn("Original body", self.loader.get_content("review"))

    def test_unauthorized_request_stops_before_catalog_access(self):
        with patch.object(self.loader, "management_catalog") as catalog:
            self.assertEqual([], self.request("/api/skills", authorized=False))
            catalog.assert_not_called()

    def test_toggle_edit_and_reload_share_runtime_catalog(self):
        result, status, _ = self.request("/api/skills/update", method="POST", payload={
            "id": "review", "revision": self.detail()["revision"], "enabled": False,
        })[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertNotIn("Original body", self.loader.get_content("review"))
        self.assertFalse(self.request("/api/skills")[0][0]["skills"][0]["enabled"])
        content = "---\nname: review\nenabled: true\n---\nNew body\n"
        self.assertEqual(HTTPStatus.OK, self.request("/api/skills/update", method="POST", payload={
            "id": "review", "revision": result["revision"], "content": content,
        })[0][1])
        self.assertIn("New body", self.loader.get_content("review"))
        new = self.root / "extra" / "SKILL.md"
        new.parent.mkdir()
        new.write_text("# External addition")
        catalog, status, _ = self.request("/api/skills/reload", method="POST", payload={})[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual({"extra", "review"}, {item["id"] for item in catalog["skills"]})
        new.unlink()
        self.assertEqual(["review"], [item["id"] for item in
                         self.request("/api/skills/reload", method="POST", payload={})[0][0]["skills"]])

    def test_stale_edit_returns_conflict_without_overwriting_other_editor(self):
        revision = self.detail()["revision"]
        self.file.write_text("External editor's newer content")
        result, status, _ = self.request("/api/skills/update", method="POST", payload={
            "id": "review", "revision": revision, "content": "Stale replacement",
        })[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertTrue(result["error"])
        self.assertEqual("External editor's newer content", self.file.read_text())

    def test_bad_document_does_not_replace_working_skill(self):
        before = self.file.read_bytes()
        self.assertEqual(HTTPStatus.BAD_REQUEST,
                         self.request("/api/skills/update", method="POST", payload={
                             "id": "review", "revision": self.detail()["revision"],
                             "content": "---\nname: [broken\n---\nBody",
                         })[0][1])
        self.assertEqual(before, self.file.read_bytes())

    def test_invalid_external_document_is_listed_and_can_be_repaired(self):
        self.file.write_text("---\nname: [broken\n---\nBody")
        skill = self.request("/api/skills")[0][0]["skills"][0]
        self.assertFalse(skill["valid"])
        self.assertTrue(skill["diagnostics"])
        self.assertEqual(HTTPStatus.OK, self.request("/api/skills/update", method="POST", payload={
            "id": skill["id"], "revision": skill["revision"], "content": "Repaired body",
        })[0][1])
        self.assertIn("Repaired body", self.loader.get_content("review"))

    def test_request_contract_rejects_ambiguous_types_and_extra_fields(self):
        base = {"id": "review", "revision": self.detail()["revision"]}
        for payload in [None, [], "text", {**base}, {**base, "enabled": "false"},
                        {**base, "enabled": 0}, {**base, "content": None},
                        {**base, "content": "x", "enabled": False},
                        {**base, "enabled": False, "root": "/tmp"},
                        {"id": 1, "revision": "x", "enabled": False}]:
            with self.subTest(payload=payload):
                self.assertEqual(HTTPStatus.BAD_REQUEST,
                                 self.request("/api/skills/update", method="POST", payload=payload)[0][1])
        self.assertEqual(HTTPStatus.BAD_REQUEST,
                         self.request("/api/skills/reload", method="POST", payload={"root": "/tmp"})[0][1])

    def test_write_requires_json_and_same_origin_for_browser_requests(self):
        payload = {"id": "review", "revision": self.detail()["revision"], "enabled": False}
        for headers, expected in [
            ({"Content-Type": "text/plain"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
            ({"Origin": "https://other.example"}, HTTPStatus.FORBIDDEN),
            ({"Origin": "null"}, HTTPStatus.FORBIDDEN),
            ({"Origin": "http://["}, HTTPStatus.FORBIDDEN),
            ({"Sec-Fetch-Site": "cross-site"}, HTTPStatus.FORBIDDEN),
            ({"Content-Length": "-1"}, HTTPStatus.BAD_REQUEST),
            ({"Content-Length": str(2 * 1024 * 1024 + 1)}, HTTPStatus.BAD_REQUEST),
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(expected, self.request("/api/skills/update", method="POST",
                                 payload=payload, headers=headers)[0][1])
        self.assertEqual(HTTPStatus.OK, self.request("/api/skills/update", method="POST",
                         payload=payload, headers={"Origin": "http://localhost:8000"})[0][1])

    def test_invalid_identifiers_and_missing_files_fail_closed(self):
        for path in ("/api/skills/detail", "/api/skills/detail?id=review&id=extra",
                     "/api/skills/detail?id=..%2Foutside", "/api/skills/detail?id=%2Fetc"):
            with self.subTest(path=path):
                self.assertEqual(HTTPStatus.BAD_REQUEST, self.request(path)[0][1])
        self.assertEqual(HTTPStatus.NOT_FOUND, self.request("/api/skills/detail?id=missing")[0][1])

    def test_storage_failure_is_service_unavailable(self):
        with patch.object(self.loader, "management_catalog", side_effect=OSError("disk failure")):
            with self.assertLogs("web.server", level="ERROR"):
                self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, self.request("/api/skills")[0][1])


if __name__ == "__main__":
    unittest.main()
