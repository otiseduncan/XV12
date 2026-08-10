from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

from .auth import SESSION_COOKIE
from .config import Settings
from .database import UserScopedStore, utcnow


ONBOARDING_COOKIE = "xv12_onboarding"
_onboarding_handle: ContextVar[str | None] = ContextVar("xv12_onboarding_handle", default=None)
_oidc_invitation_id: ContextVar[str | None] = ContextVar("xv12_oidc_invitation_id", default=None)


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EnrollmentPending(Exception):
    def __init__(self, invitation_id: str) -> None:
        super().__init__("Owner approval is required")
        self.invitation_id = invitation_id


class EnrollmentDenied(Exception):
    pass


class EnrollmentMiddleware(BaseHTTPMiddleware):
    """Carries a server-side invitation handoff through the existing Google OIDC router."""

    async def dispatch(self, request, call_next):
        handle_token = _onboarding_handle.set(request.cookies.get(ONBOARDING_COOKIE))
        invitation_token = _oidc_invitation_id.set(None)
        try:
            return await call_next(request)
        except EnrollmentPending as error:
            response = RedirectResponse(f"/onboarding/pending?invitation={error.invitation_id}", status_code=303)
            response.delete_cookie(SESSION_COOKIE, path="/")
            response.delete_cookie(ONBOARDING_COOKIE, path="/")
            return response
        except EnrollmentDenied:
            response = RedirectResponse("/onboarding/error", status_code=303)
            response.delete_cookie(SESSION_COOKIE, path="/")
            response.delete_cookie(ONBOARDING_COOKIE, path="/")
            return response
        finally:
            _oidc_invitation_id.reset(invitation_token)
            _onboarding_handle.reset(handle_token)


class EnrollmentStore(UserScopedStore):
    """Adds atomic invitation-bound enrollment while preserving the frozen user store."""

    def __init__(self, path, owner_google_sub: str, settings: Settings) -> None:
        super().__init__(path, owner_google_sub)
        self.settings = settings
        self.permission_store = None

    def initialize(self) -> None:
        super().initialize()
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS enrollment_invitations (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('pending','pending_approval','active','revoked','expired')),
                    creator_id TEXT NOT NULL REFERENCES users(id),
                    target_user_id TEXT REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approval_required INTEGER NOT NULL CHECK(approval_required IN (0,1)),
                    initial_grants_json TEXT NOT NULL DEFAULT '[]',
                    claimed_at TEXT,
                    claimed_user_id TEXT REFERENCES users(id),
                    claimed_google_sub TEXT,
                    claimed_email TEXT,
                    claimed_display_name TEXT,
                    approved_at TEXT,
                    approved_by TEXT REFERENCES users(id),
                    revoked_at TEXT,
                    revoked_by TEXT REFERENCES users(id),
                    tailscale_invite_id TEXT,
                    tailscale_invite_url TEXT,
                    tailscale_status TEXT NOT NULL DEFAULT 'not_configured',
                    tailscale_error TEXT
                );
                CREATE INDEX IF NOT EXISTS enrollment_invitation_status
                    ON enrollment_invitations(status,expires_at);
                CREATE TABLE IF NOT EXISTS enrollment_handoffs (
                    handle_hash TEXT PRIMARY KEY,
                    invitation_id TEXT NOT NULL REFERENCES enrollment_invitations(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enrollment_oidc_links (
                    state_hash TEXT PRIMARY KEY,
                    invitation_id TEXT NOT NULL REFERENCES enrollment_invitations(id),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS enrollment_audit (
                    id TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    actor_user_id TEXT,
                    invitation_id TEXT,
                    subject_user_id TEXT,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS enrollment_audit_created ON enrollment_audit(created_at DESC);
                DROP INDEX IF EXISTS enrollment_claimed_google_sub;
                """
            )

    @staticmethod
    def _public_invitation(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item.pop("token_hash", None)
        item["approval_required"] = bool(item["approval_required"])
        item["initial_grants"] = json.loads(item.pop("initial_grants_json", "[]") or "[]")
        return item

    def _audit(
        self,
        db: sqlite3.Connection,
        event: str,
        *,
        actor_user_id: str | None = None,
        invitation_id: str | None = None,
        subject_user_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO enrollment_audit(id,event,actor_user_id,invitation_id,subject_user_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), event, actor_user_id, invitation_id, subject_user_id, json.dumps(detail or {}, sort_keys=True), utcnow()),
        )

    def create_enrollment_invitation(
        self,
        creator_id: str,
        *,
        expires_at: str,
        approval_required: bool,
        initial_grants: list[dict[str, Any]],
        target_user_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        token = secrets.token_urlsafe(40)
        invitation_id = str(uuid.uuid4())
        now = utcnow()
        with self.connect() as db:
            if target_user_id:
                target = db.execute("SELECT role,status FROM users WHERE id=?", (target_user_id,)).fetchone()
                if not target or target["role"] == "admin":
                    raise ValueError("Re-invitation target is invalid")
            db.execute(
                """INSERT INTO enrollment_invitations(
                       id,token_hash,status,creator_id,target_user_id,created_at,expires_at,
                       approval_required,initial_grants_json
                   ) VALUES(?,?,'pending',?,?,?,?,?,?)""",
                (invitation_id, secret_hash(token), creator_id, target_user_id, now, expires_at, int(approval_required), json.dumps(initial_grants, sort_keys=True)),
            )
            self._audit(db, "invitation.created", actor_user_id=creator_id, invitation_id=invitation_id)
            row = db.execute("SELECT * FROM enrollment_invitations WHERE id=?", (invitation_id,)).fetchone()
        return self._public_invitation(row), token

    def update_tailscale_invitation(
        self,
        invitation_id: str,
        *,
        status: str,
        invite_id: str = "",
        invite_url: str = "",
        error: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE enrollment_invitations SET tailscale_status=?,tailscale_invite_id=?,tailscale_invite_url=?,tailscale_error=? WHERE id=?",
                (status, invite_id or None, invite_url or None, error[:500] or None, invitation_id),
            )
            self._audit(db, f"tailscale_invitation.{status}", invitation_id=invitation_id, detail={"has_invite_id": bool(invite_id), "has_error": bool(error)})

    def invitation(self, invitation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            self._expire(db)
            row = db.execute("SELECT * FROM enrollment_invitations WHERE id=?", (invitation_id,)).fetchone()
        return self._public_invitation(row) if row else None

    def invitation_by_token(self, token: str) -> dict[str, Any] | None:
        with self.connect() as db:
            self._expire(db)
            row = db.execute("SELECT * FROM enrollment_invitations WHERE token_hash=?", (secret_hash(token),)).fetchone()
        return self._public_invitation(row) if row else None

    def list_enrollment_invitations(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            self._expire(db)
            rows = db.execute("SELECT * FROM enrollment_invitations ORDER BY created_at DESC").fetchall()
        return [self._public_invitation(row) for row in rows]

    def list_enrollment_users(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT u.id,u.google_sub,u.email,u.display_name,u.preferred_name,u.role,u.status,
                          u.created_at,u.last_login_at,i.id AS invitation_id,i.status AS enrollment_status,
                          i.approved_at,i.revoked_at
                   FROM users u LEFT JOIN enrollment_invitations i ON i.claimed_user_id=u.id
                   ORDER BY CASE u.role WHEN 'admin' THEN 0 ELSE 1 END,u.display_name"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_handoff(self, token: str) -> tuple[str, dict[str, Any]]:
        now = datetime.now(UTC)
        handle = secrets.token_urlsafe(40)
        with self.connect() as db:
            self._expire(db)
            row = db.execute(
                "SELECT * FROM enrollment_invitations WHERE token_hash=? AND status='pending' AND expires_at>?",
                (secret_hash(token), now.isoformat()),
            ).fetchone()
            if not row:
                raise EnrollmentDenied("Invitation is invalid, expired, or already used")
            db.execute(
                "INSERT INTO enrollment_handoffs(handle_hash,invitation_id,created_at,expires_at) VALUES(?,?,?,?)",
                (secret_hash(handle), row["id"], now.isoformat(), min(datetime.fromisoformat(row["expires_at"]), now + timedelta(minutes=20)).isoformat()),
            )
            self._audit(db, "invitation.opened", invitation_id=row["id"])
        return handle, self._public_invitation(row)

    def invitation_for_handoff(self, handle: str | None) -> dict[str, Any] | None:
        if not handle:
            return None
        with self.connect() as db:
            self._expire(db)
            row = db.execute(
                """SELECT i.* FROM enrollment_handoffs h
                   JOIN enrollment_invitations i ON i.id=h.invitation_id
                   WHERE h.handle_hash=? AND h.expires_at>? AND i.status='pending'""",
                (secret_hash(handle), utcnow()),
            ).fetchone()
        return self._public_invitation(row) if row else None

    def create_oidc_attempt(self) -> tuple[str, str]:
        state, nonce = super().create_oidc_attempt()
        handle = _onboarding_handle.get()
        if handle:
            now = datetime.now(UTC)
            with self.connect() as db:
                handoff = db.execute(
                    """SELECT h.invitation_id FROM enrollment_handoffs h
                       JOIN enrollment_invitations i ON i.id=h.invitation_id
                       WHERE h.handle_hash=? AND h.used_at IS NULL AND h.expires_at>? AND i.status='pending' AND i.expires_at>?""",
                    (secret_hash(handle), now.isoformat(), now.isoformat()),
                ).fetchone()
                if handoff:
                    db.execute(
                        "INSERT INTO enrollment_oidc_links(state_hash,invitation_id,created_at,expires_at) VALUES(?,?,?,?)",
                        (secret_hash(state), handoff["invitation_id"], now.isoformat(), (now + timedelta(minutes=10)).isoformat()),
                    )
                    db.execute("UPDATE enrollment_handoffs SET used_at=? WHERE handle_hash=?", (now.isoformat(), secret_hash(handle)))
        return state, nonce

    def consume_oidc_attempt(self, state: str) -> str | None:
        nonce = super().consume_oidc_attempt(state)
        if not nonce:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT invitation_id FROM enrollment_oidc_links WHERE state_hash=? AND used_at IS NULL AND expires_at>?",
                (secret_hash(state), utcnow()),
            ).fetchone()
            if row:
                db.execute("UPDATE enrollment_oidc_links SET used_at=? WHERE state_hash=?", (utcnow(), secret_hash(state)))
                _oidc_invitation_id.set(str(row["invitation_id"]))
        return nonce

    def upsert_oidc_user(self, *, google_sub: str, email: str, email_verified: bool, display_name: str) -> dict[str, Any]:
        if self.settings.auth_mode == "test":
            return super().upsert_oidc_user(google_sub=google_sub, email=email, email_verified=email_verified, display_name=display_name)
        invitation_id = _oidc_invitation_id.get()
        now = utcnow()
        with self.connect() as db:
            existing = db.execute("SELECT * FROM users WHERE google_sub=?", (google_sub,)).fetchone()
            if existing and (existing["role"] == "admin" or existing["status"] == "active"):
                pass
            else:
                invitation = db.execute("SELECT * FROM enrollment_invitations WHERE id=?", (invitation_id,)).fetchone() if invitation_id else None
                if not invitation or invitation["status"] != "pending" or invitation["expires_at"] <= now:
                    self._audit(db, "signin.denied_no_invitation", subject_user_id=existing["id"] if existing else None)
                    db.commit()
                    raise EnrollmentDenied("A valid invitation is required")
                if invitation["claimed_google_sub"] and invitation["claimed_google_sub"] != google_sub:
                    self._audit(db, "invitation.identity_collision", invitation_id=invitation["id"])
                    db.commit()
                    raise EnrollmentDenied("Invitation identity collision")
                target_user_id = invitation["target_user_id"]
                if existing:
                    if not target_user_id or target_user_id != existing["id"]:
                        self._audit(db, "invitation.target_mismatch", invitation_id=invitation["id"], subject_user_id=existing["id"])
                        db.commit()
                        raise EnrollmentDenied("A revoked identity requires a targeted re-invitation")
                    user_id = existing["id"]
                else:
                    if target_user_id:
                        self._audit(db, "invitation.target_mismatch", invitation_id=invitation["id"])
                        db.commit()
                        raise EnrollmentDenied("Targeted invitation identity does not match")
                    user_id = str(uuid.uuid4())
                approval_required = bool(invitation["approval_required"])
                user_status = "disabled" if approval_required else "active"
                preferred_name = display_name.strip().split()[0] if display_name.strip() else "User"
                if existing:
                    db.execute(
                        "UPDATE users SET email=?,email_verified=?,display_name=?,status=?,last_login_at=? WHERE id=?",
                        (email, int(email_verified), display_name, user_status, now, user_id),
                    )
                else:
                    db.execute(
                        """INSERT INTO users(id,google_sub,email,email_verified,display_name,preferred_name,role,status,created_at,last_login_at)
                           VALUES(?,?,?,?,?,?,'user',?,?,?)""",
                        (user_id, google_sub, email, int(email_verified), display_name, preferred_name, user_status, now, now),
                    )
                next_status = "pending_approval" if approval_required else "active"
                updated = db.execute(
                    """UPDATE enrollment_invitations SET status=?,claimed_at=?,claimed_user_id=?,claimed_google_sub=?,
                              claimed_email=?,claimed_display_name=?
                       WHERE id=? AND status='pending'""",
                    (next_status, now, user_id, google_sub, email, display_name, invitation["id"]),
                )
                if updated.rowcount != 1:
                    raise EnrollmentDenied("Invitation was already redeemed")
                self._audit(db, "invitation.claimed", invitation_id=invitation["id"], subject_user_id=user_id, detail={"approval_required": approval_required})
                row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                grants = json.loads(invitation["initial_grants_json"] or "[]")
                if not approval_required:
                    self._apply_grants(user_id, grants, invitation["creator_id"])
                if approval_required:
                    db.commit()
                    raise EnrollmentPending(str(invitation["id"]))
                return dict(row)
        return super().upsert_oidc_user(google_sub=google_sub, email=email, email_verified=email_verified, display_name=display_name)

    def _apply_grants(self, user_id: str, grants: list[dict[str, Any]], granted_by: str) -> None:
        if not self.permission_store:
            return
        normalized = {str(item["family"]): set(map(str, item.get("scopes", []))) for item in grants if item.get("scopes")}
        self.permission_store.replace_grants(user_id, normalized, granted_by)

    def approve_invitation(self, invitation_id: str, approver_id: str) -> dict[str, Any] | None:
        now = utcnow()
        with self.connect() as db:
            invitation = db.execute("SELECT * FROM enrollment_invitations WHERE id=? AND status='pending_approval'", (invitation_id,)).fetchone()
            if not invitation:
                return None
            db.execute("UPDATE users SET status='active' WHERE id=? AND role='user'", (invitation["claimed_user_id"],))
            db.execute(
                "UPDATE enrollment_invitations SET status='active',approved_at=?,approved_by=? WHERE id=? AND status='pending_approval'",
                (now, approver_id, invitation_id),
            )
            self._audit(db, "invitation.approved", actor_user_id=approver_id, invitation_id=invitation_id, subject_user_id=invitation["claimed_user_id"])
            grants = json.loads(invitation["initial_grants_json"] or "[]")
            user_id = str(invitation["claimed_user_id"])
        self._apply_grants(user_id, grants, approver_id)
        return self.invitation(invitation_id)

    def revoke_invitation(self, invitation_id: str, actor_id: str) -> dict[str, Any] | None:
        now = utcnow()
        with self.connect() as db:
            invitation = db.execute("SELECT * FROM enrollment_invitations WHERE id=?", (invitation_id,)).fetchone()
            if not invitation or invitation["status"] not in {"pending", "pending_approval"}:
                return None
            db.execute("UPDATE enrollment_invitations SET status='revoked',revoked_at=?,revoked_by=? WHERE id=?", (now, actor_id, invitation_id))
            if invitation["claimed_user_id"]:
                db.execute("UPDATE users SET status='disabled' WHERE id=? AND role='user'", (invitation["claimed_user_id"],))
                db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, invitation["claimed_user_id"]))
            self._audit(db, "invitation.revoked", actor_user_id=actor_id, invitation_id=invitation_id, subject_user_id=invitation["claimed_user_id"])
        return self.invitation(invitation_id)

    def revoke_enrolled_user(self, user_id: str, actor_id: str) -> dict[str, Any] | None:
        now = utcnow()
        with self.connect() as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user or user["role"] == "admin":
                return None
            db.execute("UPDATE users SET status='disabled' WHERE id=?", (user_id,))
            db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, user_id))
            db.execute("UPDATE enrollment_invitations SET status='revoked',revoked_at=?,revoked_by=? WHERE claimed_user_id=? AND status IN ('active','pending_approval')", (now, actor_id, user_id))
            self._audit(db, "user.revoked", actor_user_id=actor_id, subject_user_id=user_id)
        if self.permission_store:
            self.permission_store.revoke(user_id)
        return dict(user)

    def audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM enrollment_audit ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json", "{}") or "{}")
            result.append(item)
        return result

    @staticmethod
    def _expire(db: sqlite3.Connection) -> None:
        db.execute("UPDATE enrollment_invitations SET status='expired' WHERE status='pending' AND expires_at<=?", (utcnow(),))
