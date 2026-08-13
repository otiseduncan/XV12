from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


DEFAULT_VOICE_NAME = "Google US English"
DEFAULT_VOICE_VOLUME = 75


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class UserScopedStore:
    """The single boundary for all private, user-owned XV12 persistence."""

    def __init__(self, path: Path, owner_google_sub: str) -> None:
        self.path = path
        self.owner_google_sub = owner_google_sub

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    google_sub TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL,
                    email_verified INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    preferred_name TEXT,
                    role TEXT NOT NULL CHECK(role IN ('admin','user')),
                    status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                    capability_state TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS exactly_one_admin ON users(role) WHERE role='admin';
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS oidc_attempts (
                    state_hash TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversations_user_updated ON conversations(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('complete','partial_success','budget_exhausted','interrupted','failed','cancelled')),
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_user_conversation ON messages(user_id, conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    summary TEXT NOT NULL,
                    through_message_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_subjects (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    subject_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                    original_name TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turn_traces (
                    turn_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    context_tokens INTEGER NOT NULL,
                    context_sections TEXT NOT NULL,
                    active_subject TEXT NOT NULL,
                    summary_used INTEGER NOT NULL,
                    model_started_at TEXT,
                    first_token_at TEXT,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    reference TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ('active','closed')) DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS projects_user_updated ON projects(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS active_projects (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    activated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_active_projects (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    activated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_evidence (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    evidence_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_settings (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    voice_name TEXT NOT NULL DEFAULT 'Google US English',
                    voice_volume INTEGER NOT NULL DEFAULT 75 CHECK(voice_volume BETWEEN 0 AND 100),
                    voice_muted INTEGER NOT NULL DEFAULT 0 CHECK(voice_muted IN (0,1)),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if "preferred_name" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN preferred_name TEXT")
            message_columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
            if "metadata_json" not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
            self._migrate_message_terminal_states(db)
            db.execute(
                "INSERT INTO app_meta(key,value) VALUES('schema_version','4') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        self._ensure_owner_record()

    @staticmethod
    def _migrate_message_terminal_states(db: sqlite3.Connection) -> None:
        """Widen messages.status's CHECK constraint to the full terminal-state vocabulary.
        SQLite cannot ALTER a CHECK constraint in place, so this rebuilds the table only when
        the live constraint is still the narrow ('complete','interrupted','failed') set --
        detected from the stored CREATE TABLE text itself, not merely a schema-version number,
        so it is safe to run unconditionally on every startup."""
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
        existing_sql = str(row["sql"] or "") if row else ""
        if "budget_exhausted" in existing_sql:
            return
        db.executescript(
            """
            ALTER TABLE messages RENAME TO messages_pre_terminal_states;
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('complete','partial_success','budget_exhausted','interrupted','failed','cancelled')),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            INSERT INTO messages(id,user_id,conversation_id,role,content,status,metadata_json,created_at)
                SELECT id,user_id,conversation_id,role,content,status,metadata_json,created_at FROM messages_pre_terminal_states;
            DROP TABLE messages_pre_terminal_states;
            CREATE INDEX IF NOT EXISTS messages_user_conversation ON messages(user_id, conversation_id, created_at);
            """
        )

    def schema_version(self) -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
            return str(row["value"]) if row else "unknown"

    def _ensure_owner_record(self) -> None:
        now = utcnow()
        with self.connect() as db:
            db.execute("UPDATE users SET role='user' WHERE role='admin' AND google_sub<>?", (self.owner_google_sub,))
            owner = db.execute("SELECT id FROM users WHERE google_sub=?", (self.owner_google_sub,)).fetchone()
            if owner:
                db.execute("UPDATE users SET role='admin',preferred_name='Otis' WHERE google_sub=?", (self.owner_google_sub,))
            else:
                db.execute(
                    "INSERT INTO users(id,google_sub,email,email_verified,display_name,preferred_name,role,status,created_at) VALUES(?,?,?,?,?,?,'admin','active',?)",
                    (str(uuid.uuid4()), self.owner_google_sub, "owner@pending.invalid", 0, "Otis", "Otis", now),
                )

    def upsert_oidc_user(self, *, google_sub: str, email: str, email_verified: bool, display_name: str) -> dict[str, Any]:
        role = "admin" if google_sub == self.owner_google_sub else "user"
        now = utcnow()
        with self.connect() as db:
            if role == "admin":
                db.execute("UPDATE users SET role='user' WHERE role='admin' AND google_sub<>?", (google_sub,))
            preferred_name = "Otis" if role == "admin" else (display_name.strip().split()[0] if display_name.strip() else "User")
            db.execute(
                """INSERT INTO users(id,google_sub,email,email_verified,display_name,preferred_name,role,status,created_at,last_login_at)
                   VALUES(?,?,?,?,?,?,?, 'active',?,?)
                   ON CONFLICT(google_sub) DO UPDATE SET
                     email=excluded.email,email_verified=excluded.email_verified,display_name=excluded.display_name,
                     preferred_name=CASE WHEN users.preferred_name IS NULL OR users.preferred_name='' THEN excluded.preferred_name ELSE users.preferred_name END,
                     role=excluded.role,last_login_at=excluded.last_login_at""",
                (str(uuid.uuid4()), google_sub, email, int(email_verified), display_name, preferred_name, role, now, now),
            )
            row = db.execute("SELECT * FROM users WHERE google_sub=?", (google_sub,)).fetchone()
            return dict(row)

    def admin_count(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0])

    def create_session(self, user_id: str, ttl_seconds: int) -> str:
        token = secrets.token_urlsafe(48)
        now = datetime.now(UTC)
        with self.connect() as db:
            db.execute(
                "INSERT INTO sessions(id,token_hash,user_id,created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), _hash_secret(token), user_id, now.isoformat(), now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat()),
            )
        return token

    def get_session_user(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        now = utcnow()
        with self.connect() as db:
            row = db.execute(
                """SELECT u.*,s.id AS session_id FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND u.status='active'""",
                (_hash_secret(token), now),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now, row["session_id"]))
            return dict(row)

    def revoke_session(self, token: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (utcnow(), _hash_secret(token)))

    def revoke_user_sessions(self, user_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (utcnow(), user_id))

    def set_preferred_name(self, user_id: str, preferred_name: str) -> dict[str, Any] | None:
        value = preferred_name.strip()[:80] or "User"
        with self.connect() as db:
            db.execute("UPDATE users SET preferred_name=? WHERE id=? AND role='user'", (value, user_id))
            row = db.execute("SELECT * FROM users WHERE id=? AND role='user'", (user_id,)).fetchone()
            return dict(row) if row else None

    def get_voice_settings(self, user_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT voice_name,voice_volume,voice_muted,updated_at FROM voice_settings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row:
            settings = dict(row)
            settings["voice_muted"] = bool(settings["voice_muted"])
        else:
            settings = {
                "voice_name": DEFAULT_VOICE_NAME,
                "voice_volume": DEFAULT_VOICE_VOLUME,
                "voice_muted": False,
                "updated_at": None,
            }
        settings["preferred_voice"] = DEFAULT_VOICE_NAME
        return settings

    def set_voice_settings(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"voice_name", "voice_volume", "voice_muted"}
        unexpected = set(changes) - allowed
        if unexpected:
            raise ValueError(f"Unsupported voice setting: {sorted(unexpected)[0]}")
        current = self.get_voice_settings(user_id)
        voice_name = str(changes.get("voice_name", current["voice_name"])).strip()
        if not voice_name or len(voice_name) > 120:
            raise ValueError("voice_name must contain 1 to 120 characters")
        volume = changes.get("voice_volume", current["voice_volume"])
        if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
            raise ValueError("voice_volume must be an integer from 0 to 100")
        muted = changes.get("voice_muted", current["voice_muted"])
        if not isinstance(muted, bool):
            raise ValueError("voice_muted must be a boolean")
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """INSERT INTO voice_settings(user_id,voice_name,voice_volume,voice_muted,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     voice_name=excluded.voice_name,
                     voice_volume=excluded.voice_volume,
                     voice_muted=excluded.voice_muted,
                     updated_at=excluded.updated_at""",
                (user_id, voice_name, volume, int(muted), now),
            )
        return self.get_voice_settings(user_id)

    def create_oidc_attempt(self) -> tuple[str, str]:
        state, nonce = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
        now = datetime.now(UTC)
        with self.connect() as db:
            db.execute(
                "INSERT INTO oidc_attempts(state_hash,nonce,created_at,expires_at) VALUES(?,?,?,?)",
                (_hash_secret(state), nonce, now.isoformat(), (now + timedelta(minutes=10)).isoformat()),
            )
        return state, nonce

    def consume_oidc_attempt(self, state: str) -> str | None:
        now = utcnow()
        with self.connect() as db:
            row = db.execute(
                "SELECT nonce FROM oidc_attempts WHERE state_hash=? AND used_at IS NULL AND expires_at>?",
                (_hash_secret(state), now),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE oidc_attempts SET used_at=? WHERE state_hash=?", (now, _hash_secret(state)))
            return str(row["nonce"])

    def create_conversation(self, user_id: str, title: str = "New conversation") -> dict[str, Any]:
        item_id, now = str(uuid.uuid4()), utcnow()
        with self.connect() as db:
            db.execute("INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)", (item_id, user_id, title[:100], now, now))
            return dict(db.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (item_id, user_id)).fetchone())

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC", (user_id,))]

    def rename_conversation(self, user_id: str, conversation_id: str, title: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute(
                "UPDATE conversations SET title=?,updated_at=? WHERE id=? AND user_id=?",
                (title.strip()[:100] or "Conversation", utcnow(), conversation_id, user_id),
            )
            row = db.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone()
            return dict(row) if row else None

    def delete_conversation(self, user_id: str, conversation_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
            return cursor.rowcount == 1

    def get_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["messages"] = [self._message_dict(message) for message in db.execute("SELECT * FROM messages WHERE conversation_id=? AND user_id=? ORDER BY created_at", (conversation_id, user_id))]
            item["attachments"] = [dict(att) for att in db.execute("SELECT id,original_name,content_type,size_bytes,created_at FROM attachments WHERE conversation_id=? AND user_id=? ORDER BY created_at", (conversation_id, user_id))]
            return item

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
        return item

    def add_message(
        self,
        user_id: str,
        conversation_id: str,
        role: str,
        content: str,
        status: str = "complete",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id, now = str(uuid.uuid4()), utcnow()
        with self.connect() as db:
            owned = db.execute("SELECT title FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone()
            if not owned:
                raise KeyError("conversation not found")
            db.execute("INSERT INTO messages(id,user_id,conversation_id,role,content,status,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (message_id, user_id, conversation_id, role, content, status, json.dumps(metadata or {}), now))
            if role == "user" and owned["title"] == "New conversation":
                db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=? AND user_id=?", (content.strip()[:72] or "Conversation", now, conversation_id, user_id))
            else:
                db.execute("UPDATE conversations SET updated_at=? WHERE id=? AND user_id=?", (now, conversation_id, user_id))
            return self._message_dict(db.execute("SELECT * FROM messages WHERE id=? AND user_id=?", (message_id, user_id)).fetchone())

    def recent_messages(self, user_id: str, conversation_id: str, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE conversation_id=? AND user_id=? ORDER BY created_at DESC LIMIT ?", (conversation_id, user_id, limit)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def get_summary(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversation_summaries WHERE conversation_id=? AND user_id=?", (conversation_id, user_id)).fetchone()
            return dict(row) if row else None

    def save_summary(self, user_id: str, conversation_id: str, summary: str, through_message_id: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO conversation_summaries(conversation_id,user_id,summary,through_message_id,updated_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET summary=excluded.summary,through_message_id=excluded.through_message_id,updated_at=excluded.updated_at
                   WHERE user_id=excluded.user_id""",
                (conversation_id, user_id, summary, through_message_id, utcnow()),
            )

    def get_active_subject(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT subject_json FROM active_subjects WHERE conversation_id=? AND user_id=?", (conversation_id, user_id)).fetchone()
            return json.loads(row["subject_json"]) if row else {}

    def ensure_active_subject(self, user_id: str, conversation_id: str, latest_user_text: str) -> dict[str, Any]:
        """Update the active-subject state with the latest substantial user message. The
        subject evolves every turn -- an abrupt switch, an explicit correction, or returning
        to an earlier topic all just become the new current topic -- instead of freezing to
        the conversation's first message forever. A short rolling history of prior topics is
        kept so the model can recognize a return to something discussed earlier; this is
        deliberately plain state tracking, not an intent classifier."""
        current = self.get_active_subject(user_id, conversation_id)
        text = latest_user_text.strip()
        if len(text) < 8:
            return current
        topic = text[:180]
        if current.get("topic") == topic:
            return current
        history = [item for item in (current.get("recent_topics") or []) if item != topic]
        if current.get("topic") and current["topic"] != topic:
            history = [current["topic"], *[item for item in history if item != current["topic"]]]
        subject = {"topic": topic, "recent_topics": history[:3]}
        with self.connect() as db:
            db.execute(
                """INSERT INTO active_subjects(conversation_id,user_id,subject_json,updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET subject_json=excluded.subject_json,updated_at=excluded.updated_at
                   WHERE user_id=excluded.user_id""",
                (conversation_id, user_id, json.dumps(subject), utcnow()),
            )
        return subject

    def add_attachment(self, user_id: str, conversation_id: str | None, original_name: str, storage_path: str, content_type: str, size_bytes: int) -> dict[str, Any]:
        attachment_id = str(uuid.uuid4())
        with self.connect() as db:
            if conversation_id and not db.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone():
                raise KeyError("conversation not found")
            db.execute("INSERT INTO attachments(id,user_id,conversation_id,original_name,storage_path,content_type,size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?)", (attachment_id, user_id, conversation_id, original_name, storage_path, content_type, size_bytes, utcnow()))
            return dict(db.execute("SELECT * FROM attachments WHERE id=? AND user_id=?", (attachment_id, user_id)).fetchone())

    def delete_attachment(self, user_id: str, attachment_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM attachments WHERE id=? AND user_id=?", (attachment_id, user_id)).fetchone()
            if not row:
                return None
            db.execute("DELETE FROM attachments WHERE id=? AND user_id=?", (attachment_id, user_id))
            return dict(row)

    def create_project(self, user_id: str, name: str, reference: str | None, description: str = "") -> dict[str, Any]:
        project_id, now = str(uuid.uuid4()), utcnow()
        with self.connect() as db:
            db.execute(
                "INSERT INTO projects(id,user_id,name,reference,description,status,created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?)",
                (project_id, user_id, name.strip()[:120], (reference or "").strip()[:500] or None, description.strip()[:2000], now, now),
            )
            return dict(db.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, user_id)).fetchone())

    def list_projects(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            active = db.execute("SELECT project_id FROM active_projects WHERE user_id=?", (user_id,)).fetchone()
            active_id = active["project_id"] if active else None
            return [
                {**dict(row), "is_active": row["id"] == active_id}
                for row in db.execute("SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
            ]

    def activate_project(self, user_id: str, project_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=? AND user_id=? AND status='active'", (project_id, user_id)).fetchone()
            if not row:
                return None
            db.execute(
                "INSERT INTO active_projects(user_id,project_id,activated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET project_id=excluded.project_id,activated_at=excluded.activated_at",
                (user_id, project_id, utcnow()),
            )
            return {**dict(row), "is_active": True}

    def deactivate_project(self, user_id: str) -> bool:
        with self.connect() as db:
            return db.execute("DELETE FROM active_projects WHERE user_id=?", (user_id,)).rowcount > 0

    def active_project(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT p.* FROM active_projects a JOIN projects p ON p.id=a.project_id WHERE a.user_id=? AND p.user_id=? AND p.status='active'",
                (user_id, user_id),
            ).fetchone()
            return dict(row) if row else None

    def activate_project_for_conversation(self, user_id: str, conversation_id: str, project_id: str) -> dict[str, Any] | None:
        """Bind a project to one specific conversation without disturbing the user's global
        last-selected project (the standalone Projects UI and its header chip keep working
        unchanged). This is what in-chat project activation should use: two open
        conversations for the same user can carry different active projects, preventing the
        cross-conversation project-context bleed the global-only table allowed."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=? AND user_id=? AND status='active'", (project_id, user_id)).fetchone()
            if not row:
                return None
            db.execute(
                """INSERT INTO conversation_active_projects(conversation_id,user_id,project_id,activated_at) VALUES(?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET project_id=excluded.project_id,activated_at=excluded.activated_at
                   WHERE user_id=excluded.user_id""",
                (conversation_id, user_id, project_id, utcnow()),
            )
            return {**dict(row), "is_active": True}

    def deactivate_project_for_conversation(self, user_id: str, conversation_id: str) -> bool:
        with self.connect() as db:
            return db.execute(
                "DELETE FROM conversation_active_projects WHERE conversation_id=? AND user_id=?", (conversation_id, user_id)
            ).rowcount > 0

    def active_project_for_conversation(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        """The project bound to this specific conversation if one was explicitly activated in
        it; otherwise the user's global last-selected project (unchanged legacy behavior)."""
        with self.connect() as db:
            row = db.execute(
                """SELECT p.* FROM conversation_active_projects c JOIN projects p ON p.id=c.project_id
                   WHERE c.conversation_id=? AND c.user_id=? AND p.user_id=? AND p.status='active'""",
                (conversation_id, user_id, user_id),
            ).fetchone()
            if row:
                return dict(row)
        return self.active_project(user_id)

    def attach_to_conversation(self, user_id: str, conversation_id: str, attachment_ids: list[str]) -> list[dict[str, Any]]:
        if not attachment_ids:
            return []
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone():
                raise KeyError("conversation not found")
            found = []
            for attachment_id in attachment_ids:
                row = db.execute("SELECT * FROM attachments WHERE id=? AND user_id=?", (attachment_id, user_id)).fetchone()
                if not row:
                    raise KeyError("attachment not found")
                db.execute("UPDATE attachments SET conversation_id=? WHERE id=? AND user_id=?", (conversation_id, attachment_id, user_id))
                found.append(dict(row))
            return found

    def save_evidence(self, user_id: str, conversation_id: str, evidence: dict[str, Any]) -> None:
        """Bounded structured evidence from a turn that stopped before natural completion --
        target, sources inspected, observations, unresolved items, receipts, stop reason, and
        next action. Never raw tool payloads and never hidden reasoning; see
        app.assistant.build_evidence_snapshot for what is actually stored."""
        with self.connect() as db:
            db.execute(
                """INSERT INTO conversation_evidence(conversation_id,user_id,evidence_json,updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(conversation_id) DO UPDATE SET evidence_json=excluded.evidence_json,updated_at=excluded.updated_at
                   WHERE user_id=excluded.user_id""",
                (conversation_id, user_id, json.dumps(evidence, ensure_ascii=False), utcnow()),
            )

    def get_evidence(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT evidence_json FROM conversation_evidence WHERE conversation_id=? AND user_id=?",
                (conversation_id, user_id),
            ).fetchone()
            return json.loads(row["evidence_json"]) if row else None

    def clear_evidence(self, user_id: str, conversation_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM conversation_evidence WHERE conversation_id=? AND user_id=?", (conversation_id, user_id))

    def create_trace(self, trace: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO turn_traces(turn_id,user_id,conversation_id,context_tokens,context_sections,active_subject,summary_used,status,detail)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (trace["turn_id"], trace["user_id"], trace["conversation_id"], trace["context_tokens"], json.dumps(trace["context_sections"]), json.dumps(trace["active_subject"]), int(trace["summary_used"]), trace["status"], json.dumps(trace.get("detail", {}))),
            )

    def update_trace(self, turn_id: str, **fields: Any) -> None:
        allowed = {"model_started_at", "first_token_at", "completed_at", "status", "detail"}
        values = {key: (json.dumps(value) if key == "detail" else value) for key, value in fields.items() if key in allowed}
        if not values:
            return
        assignment = ",".join(f"{key}=?" for key in values)
        with self.connect() as db:
            db.execute(f"UPDATE turn_traces SET {assignment} WHERE turn_id=?", (*values.values(), turn_id))
