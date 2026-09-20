"""Firebase Authentication for the API.

Identity comes from a Firebase ID token sent as ``Authorization: Bearer <token>``
and verified with the Firebase Admin SDK; a uid supplied by the browser is never
trusted. The Firebase uid is the only user identifier the app keeps — there is no
user database.
"""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from fastapi import Depends, HTTPException, Request, status

log = logging.getLogger("auth")


@dataclass(frozen=True)
class AuthUser:
    uid: str
    email: str = ""
    name: str = ""
    picture: str = ""

    def dict(self) -> dict[str, str]:
        return {"uid": self.uid, "email": self.email, "name": self.name, "picture": self.picture}


# The browser needs these to talk to Firebase; they are public by design.
WEB_CONFIG_KEYS = {
    "apiKey": "FIREBASE_API_KEY",
    "authDomain": "FIREBASE_AUTH_DOMAIN",
    "projectId": "FIREBASE_PROJECT_ID",
    "storageBucket": "FIREBASE_STORAGE_BUCKET",
    "messagingSenderId": "FIREBASE_MESSAGING_SENDER_ID",
    "appId": "FIREBASE_APP_ID",
}

# Dev user for machines with no Firebase project configured (see auth_enabled()).
DEV_USER = AuthUser(uid="dev-local", email="dev@example.com", name="Local dev")


def web_config() -> dict[str, str]:
    """Frontend-safe Firebase config, empty when the project is not configured."""
    config = {key: os.environ.get(env, "") for key, env in WEB_CONFIG_KEYS.items()}
    return config if config["apiKey"] and config["projectId"] else {}


def _service_account() -> dict[str, str] | None:
    project_id = os.environ.get("FIREBASE_PROJECT_ID", "")
    client_email = os.environ.get("FIREBASE_CLIENT_EMAIL", "")
    private_key = os.environ.get("FIREBASE_PRIVATE_KEY", "")
    if not (project_id and client_email and private_key):
        return None
    return {
        "type": "service_account",
        "project_id": project_id,
        "client_email": client_email,
        # Env vars usually carry the key with literal "\n" sequences.
        "private_key": private_key.replace("\\n", "\n"),
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def admin_configured() -> bool:
    return bool(_service_account() or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))


def auth_enabled() -> bool:
    """Auth is on unless the machine has no Firebase credentials and opts out.

    ``DEV_AUTH_BYPASS`` only works when Firebase Admin is not configured, so a
    deployment holding real credentials can never be talked out of verifying.
    """
    if admin_configured():
        return True
    return os.environ.get("DEV_AUTH_BYPASS", "") not in {"1", "true", "yes"}


@lru_cache(maxsize=1)
def _admin_auth() -> Any:
    import firebase_admin
    from firebase_admin import auth as admin_auth, credentials

    if not firebase_admin._apps:
        account = _service_account()
        cred = credentials.Certificate(account) if account else credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    return admin_auth


def verify_token(token: str) -> AuthUser:
    """Verify a Firebase ID token, or raise 401 with a safe message."""
    from firebase_admin import auth as admin_auth

    try:
        claims = _admin_auth().verify_id_token(token, check_revoked=True)
    except admin_auth.ExpiredIdTokenError as exc:
        raise _unauthorized("Session expired — sign in again.") from exc
    except admin_auth.RevokedIdTokenError as exc:
        raise _unauthorized("Session revoked — sign in again.") from exc
    except admin_auth.UserDisabledError as exc:
        raise _unauthorized("This account is disabled.") from exc
    except Exception as exc:  # bad signature, wrong project, malformed token…
        log.warning("Rejected Firebase ID token: %s", type(exc).__name__)
        raise _unauthorized("Invalid session — sign in again.") from exc
    return AuthUser(
        uid=claims["uid"],
        email=claims.get("email", ""),
        name=claims.get("name", ""),
        picture=claims.get("picture", ""),
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_firebase_user(request: Request) -> AuthUser:
    """FastAPI dependency: the verified Firebase user behind this request."""
    if not auth_enabled():
        return DEV_USER
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Sign in to continue.")
    if not admin_configured():
        # Tokens cannot be verified here, so nothing is trusted.
        log.error("Firebase Admin credentials are missing; rejecting API request.")
        raise HTTPException(status_code=503, detail="Authentication is not configured on the server.")
    return verify_token(token.strip())


AuthenticatedUser = Depends(require_firebase_user)
