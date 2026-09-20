"""Checks on Firebase auth: no bypass, no leaked credentials, clean 401s."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_ENV = {
    "FIREBASE_PROJECT_ID": "demo-project",
    "FIREBASE_CLIENT_EMAIL": "admin@demo-project.iam.gserviceaccount.com",
    "FIREBASE_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----\\n",
}
WEB_ENV = {"FIREBASE_API_KEY": "web-api-key", "FIREBASE_AUTH_DOMAIN": "demo-project.firebaseapp.com"}
PROTECTED = ["/api/me", "/api/match", "/api/crop", "/api/export", "/api/parse"]


def with_env(env: dict[str, str]):
    for key in [*ADMIN_ENV, *WEB_ENV, "DEV_AUTH_BYPASS"]:
        os.environ.pop(key, None)
    os.environ.update(env)


def test_protected_without_token() -> None:
    with_env({**ADMIN_ENV, **WEB_ENV})
    client = TestClient(app)
    for path in PROTECTED:
        response = client.request("GET" if path == "/api/me" else "POST", path)
        assert response.status_code == 401, (path, response.status_code)
        assert "sign in" in response.json()["detail"].lower(), response.json()


def test_bypass_ignored_when_admin_configured() -> None:
    with_env({**ADMIN_ENV, **WEB_ENV, "DEV_AUTH_BYPASS": "1"})
    assert auth.auth_enabled() is True
    assert TestClient(app).get("/api/me").status_code == 401


def test_bypass_only_without_credentials() -> None:
    with_env({"DEV_AUTH_BYPASS": "1"})
    assert auth.auth_enabled() is False
    assert TestClient(app).get("/api/me").json()["uid"] == auth.DEV_USER.uid


def test_config_exposes_web_keys_only() -> None:
    with_env({**ADMIN_ENV, **WEB_ENV})
    body = TestClient(app).get("/api/config").json()
    assert body["auth_enabled"] is True
    assert body["firebase"]["apiKey"] == "web-api-key"
    assert "-----BEGIN" not in str(body) and ADMIN_ENV["FIREBASE_CLIENT_EMAIL"] not in str(body)


def test_private_key_newlines_restored() -> None:
    with_env(ADMIN_ENV)
    account = auth._service_account()
    assert account is not None and "\\n" not in account["private_key"]
    assert account["private_key"].startswith("-----BEGIN PRIVATE KEY-----\n")


def test_garbage_token_is_rejected_cleanly() -> None:
    with_env({**ADMIN_ENV, **WEB_ENV})
    response = TestClient(app).get("/api/me", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401, response.status_code
    assert "sign in again" in response.json()["detail"].lower(), response.json()


def main() -> int:
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("ok:", name)
    print("auth checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
