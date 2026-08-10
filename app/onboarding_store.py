from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class OnboardingStore:
    """One-time Xoduz enrollment records stored beside capability grants."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS onboarding_invitations (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    tailscale_invite_id TEXT,
                    tailscale_invite_url TEXT NOT NULL,
                    claimed_user_id TEXT,
                    claimed_google_sub TEXT,
                    claimed_email TEXT,
                    claimed_display_name TEXT,
                    claimed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS onboarding_invitations_status
                    ON onboarding_invitations(status, expires_at);
                CREATE TABLE IF NOT EXISTS onboarding_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO onboarding_meta(key,value) VALUES('schema_version','1')
                  ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                """
            )

    def create_invitation(
        self,
        *,
        created_by: str,
        expires_hours: int,
        tailscale_invite_id: str | None,
        tailscale_invite_url: str,
    ) -> tuple[dict[str, Any], str]:
        token = secrets.token_urlsafe(36)
        invitation_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires = now + timedelta(hours=expires_hours)
        with self.connect() as db:
            db.execute(
                """INSERT INTO onboarding_invitations(
                       id,token_hash,created_by,created_at,expires_at,status,
                       tailscale_invite_id,tailscale_invite_url,updated_at
                   ) VALUES(?,?,?,?,?,'pending',?,?,?)""",
                (
                    invitation_id,
                    _token_hash(token),
                    created_by,
                    now.isoformat(),
                    expires.isoformat(),
                    tailscale_invite_id,
                    tailscale_invite_url,
                    now.isoformat(),
                ),
            )
            row = db.execute("SELECT * FROM onboarding_invitations WHERE id=?", (invitation_id,)).fetchone()
        return self._public_invitation(dict(row), include_network_link=True), token

    def _expire_row_if_needed(self, db: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
        if row["status"] == "pending" and _parse_time(str(row["expires_at"])) <= datetime.now(UTC):
            db.execute(
                "UPDATE onboarding_invitations SET status='expired',updated_at=? WHERE id=? AND status='pending'",
                (utcnow(), row["id"]),
            )
            row = db.execute("SELECT * FROM onboarding_invitations WHERE id=?", (row["id"],)).fetchone()
        return row

    def invitation_for_token(self, token: str, *, include_network_link: bool = False) -> dict[str, Any] | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM onboarding_invitations WHERE token_hash=?",
                (_token_hash(token),),
            ).fetchone()
            if not row:
                return None
            row = self._expire_row_if_needed(db, row)
            return self._public_invitation(dict(row), include_network_link=include_network_link)

    def invitation_for_id(self, invitation_id: str, *, include_network_link: bool = False) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM onboarding_invitations WHERE id=?", (invitation_id,)).fetchone()
            if not row:
                return None
            row = self._expire_row_if_needed(db, row)
            return self._public_invitation(dict(row), include_network_link=include_network_link)

    def list_invitations(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM onboarding_invitations ORDER BY created_at DESC").fetchall()
            result = []
            for row in rows:
                row = self._expire_row_if_needed(db, row)
                result.append(self._public_invitation(dict(row), include_network_link=False))
            return result

    def claim_invitation(self, token: str, user: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM onboarding_invitations WHERE token_hash=?",
                (_token_hash(token),),
            ).fetchone()
            if not row:
                raise ValueError("Invitation was not found")
            row = self._expire_row_if_needed(db, row)
            if row["status"] == "active" and row["claimed_user_id"] == user["id"]:
                return self._public_invitation(dict(row), include_network_link=False)
            if row["status"] != "pending":
                raise ValueError(f"Invitation is {row['status']}")
            db.execute(
                """UPDATE onboarding_invitations
                   SET status='active',claimed_user_id=?,claimed_google_sub=?,claimed_email=?,
                       claimed_display_name=?,claimed_at=?,updated_at=?
                   WHERE id=? AND status='pending'""",
                (
                    user["id"],
                    user.get("google_sub") or "",
                    user.get("email") or "",
                    user.get("display_name") or "User",
                    now,
                    now,
                    row["id"],
                ),
            )
            updated = db.execute("SELECT * FROM onboarding_invitations WHERE id=?", (row["id"],)).fetchone()
        return self._public_invitation(dict(updated), include_network_link=False)

    def revoke_invitation(self, invitation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM onboarding_invitations WHERE id=?", (invitation_id,)).fetchone()
            if not row:
                return None
            if row["status"] in {"pending", "expired"}:
                db.execute(
                    "UPDATE onboarding_invitations SET status='revoked',updated_at=? WHERE id=?",
                    (utcnow(), invitation_id),
                )
                row = db.execute("SELECT * FROM onboarding_invitations WHERE id=?", (invitation_id,)).fetchone()
            return self._public_invitation(dict(row), include_network_link=True)

    @staticmethod
    def _public_invitation(row: dict[str, Any], *, include_network_link: bool) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "claimed_user_id": row.get("claimed_user_id"),
            "claimed_email": row.get("claimed_email"),
            "claimed_display_name": row.get("claimed_display_name"),
            "claimed_at": row.get("claimed_at"),
            "tailscale_invite_id": row.get("tailscale_invite_id"),
        }
        if include_network_link:
            result["tailscale_invite_url"] = row.get("tailscale_invite_url")
        return result
