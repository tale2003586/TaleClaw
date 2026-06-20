import json
import unittest
from http import HTTPStatus
from unittest.mock import patch

from web.auth_store import EnvironmentUser, WebAuthStore
from web.server import AUTH_COOKIE_NAME, RequestHandler
from postgres_utils import temporary_postgres_schema


class WebAuthStoreTests(unittest.TestCase):
    def test_registered_password_is_hashed_and_session_can_be_revoked(self) -> None:
        with temporary_postgres_schema("web_auth_hash") as dsn:
            store = WebAuthStore(dsn)
            user = store.register(user_id="alice", password="alice-password")
            token = store.create_session(user)

            with store._connect() as conn:
                row = conn.execute(
                    "SELECT password_hash, salt, role, source FROM web_users WHERE user_id = 'alice'"
                ).fetchone()

            self.assertNotEqual("alice-password", row["password_hash"])
            self.assertEqual(64, len(row["password_hash"]))
            self.assertEqual(32, len(row["salt"]))
            self.assertEqual("user", row["role"])
            self.assertEqual("registration", row["source"])
            self.assertEqual("alice", store.authenticate_session(token).user_id)

            store.revoke_session(token)
            self.assertIsNone(store.authenticate_session(token))

    def test_environment_user_is_synced_as_admin(self) -> None:
        with temporary_postgres_schema("web_auth_env") as dsn:
            store = WebAuthStore(dsn)
            store.sync_environment_users({
                "admin": EnvironmentUser(
                    user_id="admin",
                    password="admin-password",
                    role="admin",
                )
            })

            user = store.authenticate(user_id="admin", password="admin-password")

            self.assertEqual("admin", user.user_id)
            self.assertEqual("admin", user.role)
            self.assertIsNone(store.authenticate(user_id="admin", password="wrong-password"))

    def test_registration_rejects_short_password_and_duplicate_username(self) -> None:
        with temporary_postgres_schema("web_auth_register") as dsn:
            store = WebAuthStore(dsn)
            with self.assertRaisesRegex(ValueError, "at least 8"):
                store.register(user_id="alice", password="short")

            store.register(user_id="alice", password="alice-password")
            with self.assertRaisesRegex(ValueError, "already registered"):
                store.register(user_id="alice", password="another-password")


class WebAuthHandlerTests(unittest.TestCase):
    def _handler(self) -> tuple[RequestHandler, list[tuple[dict, HTTPStatus, dict]]]:
        handler = object.__new__(RequestHandler)
        handler.headers = {}
        responses = []
        handler._send_json = lambda payload, status=HTTPStatus.OK, headers=None: responses.append(
            (payload, status, headers or {})
        )
        return handler, responses

    def test_login_endpoint_sets_http_only_cookie_for_environment_admin(self) -> None:
        with temporary_postgres_schema("web_auth_login") as dsn:
            store = WebAuthStore(dsn)
            handler, responses = self._handler()
            handler._read_json_body = lambda: {
                "username": "admin",
                "password": "admin-password",
            }
            users = json.dumps({
                "admin": {"password": "admin-password", "role": "admin"},
            })

            with (
                patch("web.server.web_auth_store", return_value=store),
                patch.dict("os.environ", {"WEB_USERS_JSON": users}),
            ):
                handler._handle_auth_login()

            payload, status, headers = responses[0]
            self.assertEqual(HTTPStatus.OK, status)
            self.assertEqual("admin", payload["user"]["role"])
            self.assertIn(f"{AUTH_COOKIE_NAME}=", headers["Set-Cookie"])
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            self.assertIn("SameSite=Lax", headers["Set-Cookie"])

    def test_registration_endpoint_creates_regular_user_and_cookie_session(self) -> None:
        with temporary_postgres_schema("web_auth_endpoint") as dsn:
            store = WebAuthStore(dsn)
            handler, responses = self._handler()
            handler._read_json_body = lambda: {
                "username": "guest",
                "password": "guest-password",
            }

            with (
                patch("web.server.web_auth_store", return_value=store),
                patch.dict("os.environ", {
                    "WEB_ALLOW_REGISTRATION": "1",
                    "WEB_USERS_JSON": json.dumps({
                        "admin": {"password": "admin-password", "role": "admin"},
                    }),
                }),
            ):
                handler._handle_auth_register()

            payload, status, headers = responses[0]
            self.assertEqual(HTTPStatus.CREATED, status)
            self.assertEqual({"id": "guest", "role": "user"}, payload["user"])
            token = headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
            self.assertEqual("guest", store.authenticate_session(token).user_id)

    def test_cookie_session_authorizes_request_and_logout_revokes_it(self) -> None:
        with temporary_postgres_schema("web_auth_cookie") as dsn:
            store = WebAuthStore(dsn)
            user = store.register(user_id="guest", password="guest-password")
            token = store.create_session(user)
            handler, responses = self._handler()
            handler.path = "/api/health"
            handler.headers = {"Cookie": f"{AUTH_COOKIE_NAME}={token}"}

            with patch("web.server.web_auth_store", return_value=store):
                self.assertTrue(handler._authorize())
                self.assertEqual("guest", handler._current_user().user_id)
                handler._handle_auth_logout()

            self.assertIsNone(store.authenticate_session(token))
            self.assertIn("Max-Age=0", responses[0][2]["Set-Cookie"])

    def test_registration_endpoint_can_be_disabled(self) -> None:
        handler, responses = self._handler()
        with patch.dict("os.environ", {"WEB_ALLOW_REGISTRATION": "0"}):
            handler._handle_auth_register()

        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][1])


if __name__ == "__main__":
    unittest.main()
