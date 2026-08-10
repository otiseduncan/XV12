from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .auth import current_user
from .onboarding import attach_onboarding_routes


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class GrantItem(BaseModel):
    family: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list, max_length=20)


class GrantUpdate(BaseModel):
    grants: list[GrantItem] = Field(default_factory=list, max_length=200)


class CapabilityPermissionStore:
    """Immediate, server-authoritative user grants outside the conversation store."""

    def __init__(self, path: Path, user_database_path: Path) -> None:
        self.path = path
        self.user_database_path = user_database_path

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
                CREATE TABLE IF NOT EXISTS capability_grants (
                    user_id TEXT NOT NULL,
                    family TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    granted_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id,family,scope)
                );
                CREATE INDEX IF NOT EXISTS capability_grants_user ON capability_grants(user_id);
                CREATE TABLE IF NOT EXISTS permission_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO permission_meta(key,value) VALUES('schema_version','1')
                  ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                """
            )

    def list_users(self) -> list[dict[str, Any]]:
        uri = f"file:{self.user_database_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=20) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT id,email,display_name,preferred_name,role,status,created_at,last_login_at
                   FROM users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, display_name"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_users() if item["id"] == user_id), None)

    def grants_for(self, user_id: str) -> dict[str, list[str]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT family,scope FROM capability_grants WHERE user_id=? ORDER BY family,scope",
                (user_id,),
            ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(str(row["family"]), []).append(str(row["scope"]))
        return result

    def allows(self, user_id: str, family: str, scope: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM capability_grants WHERE user_id=? AND family=? AND scope=?",
                (user_id, family, scope),
            ).fetchone()
        return row is not None

    def replace_grants(
        self,
        user_id: str,
        grants: dict[str, set[str]],
        granted_by: str,
    ) -> dict[str, list[str]]:
        now = utcnow()
        with self.connect() as db:
            db.execute("DELETE FROM capability_grants WHERE user_id=?", (user_id,))
            for family, scopes in grants.items():
                for scope in sorted(scopes):
                    db.execute(
                        "INSERT INTO capability_grants(user_id,family,scope,granted_by,updated_at) VALUES(?,?,?,?,?)",
                        (user_id, family, scope, granted_by, now),
                    )
        return self.grants_for(user_id)

    def revoke(self, user_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM capability_grants WHERE user_id=?", (user_id,))


def create_permission_router(permission_store: CapabilityPermissionStore) -> APIRouter:
    router = APIRouter(prefix="/api/admin/capabilities", tags=["capability-permissions"])

    def require_admin(user: dict[str, Any]) -> None:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Administrator role required")

    @router.get("/users")
    def users(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
        require_admin(user)
        return permission_store.list_users()

    @router.get("/catalog")
    def catalog(request: Request, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        require_admin(user)
        return {
            "registry_version": request.app.state.registry.version,
            "permission_schema": "1",
            "families": request.app.state.registry.permission_catalog("user"),
        }

    @router.get("/users/{user_id}/grants")
    def get_grants(user_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        require_admin(user)
        target = permission_store.get_user(user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        return {"user": target, "grants": permission_store.grants_for(user_id)}

    @router.put("/users/{user_id}/grants")
    def put_grants(
        user_id: str,
        payload: GrantUpdate,
        request: Request,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        require_admin(user)
        target = permission_store.get_user(user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["role"] == "admin":
            raise HTTPException(status_code=400, detail="Administrator access is implicit and cannot be reduced by grants")
        catalog = {item["family"]: set(item["allowed_scopes"]) for item in request.app.state.registry.permission_catalog("user")}
        requested: dict[str, set[str]] = {}
        for item in payload.grants:
            if item.family not in catalog:
                raise HTTPException(status_code=400, detail=f"Unknown or role-blocked capability family: {item.family}")
            scopes = set(item.scopes)
            if not scopes <= catalog[item.family]:
                raise HTTPException(status_code=400, detail=f"Grant exceeds role policy for {item.family}")
            if scopes:
                requested[item.family] = scopes
        grants = permission_store.replace_grants(user_id, requested, user["id"])
        return {"status": "updated", "user_id": user_id, "grants": grants, "effective_immediately": True}

    @router.delete("/users/{user_id}/grants", status_code=204)
    def revoke_grants(
        user_id: str,
        request: Request,
        user: dict[str, Any] = Depends(current_user),
    ) -> Response:
        require_admin(user)
        target = permission_store.get_user(user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["role"] == "admin":
            raise HTTPException(status_code=400, detail="Administrator capability access is implicit")
        permission_store.revoke(user_id)
        request.app.state.store.revoke_user_sessions(user_id)
        return Response(status_code=204)

    attach_onboarding_routes(router, permission_store)
    return router
