from __future__ import annotations

from pathlib import Path

import pytest

from app.capabilities.files import LocalFilesCapability, SensitivePathError
from .conftest import login


pytestmark = pytest.mark.security


def make_capability(tmp_path: Path) -> LocalFilesCapability:
    return LocalFilesCapability(
        read_roots=[tmp_path],
        managed_root=tmp_path / "managed",
        attachments_root=tmp_path / "attachments",
    )


# --- Unit-level: LocalFilesCapability authorization, isolated from the real X:\XV12 tree ---

def test_normal_user_cannot_read_another_users_managed_file(tmp_path):
    capability = make_capability(tmp_path)
    user_a = {"id": "user-a", "role": "user"}
    user_b = {"id": "user-b", "role": "user"}
    written = capability.write({"path": "diary.txt", "content": "user A private notes"}, user_a)
    assert written["status"] == "success"
    private_path = written["receipt"]["path"]

    result = capability.read({"path": private_path}, user_b)
    assert result["status"] != "success"
    assert "content" not in result


def test_normal_user_cannot_enumerate_the_shared_managed_root(tmp_path):
    capability = make_capability(tmp_path)
    user_a = {"id": "user-a", "role": "user"}
    user_b = {"id": "user-b", "role": "user"}
    capability.write({"path": "secret-plan.txt", "content": "user A"}, user_a)

    # Listing the parent managed root (which contains every user's subtree) must be denied,
    # not silently return an empty or partial listing -- enumeration is the attack, not just read.
    listing = capability.read({"path": str(capability.managed_root)}, user_b)
    assert listing["status"] != "success"


def test_normal_user_cannot_read_repository_wide_admin_roots(tmp_path):
    """A normal user with Files/read permission must not inherit repository-wide filesystem
    access just because the admin's broad read_roots happen to be configured on the same
    capability instance."""
    capability = make_capability(tmp_path)
    (tmp_path / "app-internal.txt").write_text("application-private data", encoding="utf-8")
    admin = {"id": "admin-1", "role": "admin"}
    user = {"id": "user-a", "role": "user"}

    admin_result = capability.read({"path": str(tmp_path / "app-internal.txt")}, admin)
    assert admin_result["status"] == "success"

    user_result = capability.read({"path": str(tmp_path / "app-internal.txt")}, user)
    assert user_result["status"] != "success"
    assert "content" not in user_result


def test_normal_user_can_read_their_own_attachment_area(tmp_path):
    capability = make_capability(tmp_path)
    user = {"id": "user-a", "role": "user"}
    attachment_dir = capability.attachments_root / user["id"]
    attachment_dir.mkdir(parents=True, exist_ok=True)
    target = attachment_dir / "uploaded.txt"
    target.write_text("attachment content", encoding="utf-8")

    result = capability.read({"path": str(target)}, user)
    assert result["status"] == "success"
    assert result["content"] == "attachment content"


@pytest.mark.parametrize("filename", [".env", ".env.local", ".env.security-fixture", "synthetic-secret.txt", "id_rsa", "credentials.json"])
def test_secret_and_credential_paths_are_denied_for_every_role(tmp_path, filename):
    capability = make_capability(tmp_path)
    target = tmp_path / filename
    target.write_text("XV12_SYNTHETIC_FIXTURE_SECRET=do-not-leak", encoding="utf-8")
    admin = {"id": "admin-1", "role": "admin"}
    user = {"id": "user-a", "role": "user"}

    for actor in (admin, user):
        result = capability.read({"path": str(target)}, actor)
        assert result["status"] == "permission_denied", (filename, actor["role"], result)
        assert "content" not in result


def test_secret_directories_are_denied_even_when_the_filename_looks_ordinary(tmp_path):
    capability = make_capability(tmp_path)
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    target = ssh_dir / "config"
    target.write_text("Host * IdentityFile ~/.ssh/id_ed25519", encoding="utf-8")
    admin = {"id": "admin-1", "role": "admin"}

    result = capability.read({"path": str(target)}, admin)
    assert result["status"] == "permission_denied"


def test_sensitive_directory_listing_omits_secret_files():
    from app.capabilities.files import is_sensitive_path
    assert is_sensitive_path(Path(".env"))
    assert is_sensitive_path(Path("config/.env.local"))
    assert is_sensitive_path(Path("id_rsa"))
    assert is_sensitive_path(Path("data/xv12.db"))
    assert not is_sensitive_path(Path("app/main.py"))
    assert not is_sensitive_path(Path("README.md"))


def test_guard_sensitive_path_raises_typed_error(tmp_path):
    from app.capabilities.files import guard_sensitive_path
    with pytest.raises(SensitivePathError):
        guard_sensitive_path(tmp_path / ".env")


# --- Integration: cross-user isolation through the real gateway, registry, and grants ---

def _grant_files_read_write(admin_client, user_id: str) -> None:
    response = admin_client.put(
        f"/api/admin/capabilities/users/{user_id}/grants",
        json={"grants": [{"family": "files", "scopes": ["read", "write"]}]},
    )
    assert response.status_code == 200, response.text


@pytest.mark.security
def test_cross_user_files_isolation_end_to_end(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as admin_client, TestClient(app) as user_a_client, TestClient(app) as user_b_client:
        login(admin_client, "admin")
        user_a = login(user_a_client, "user-a")
        user_b = login(user_b_client, "user-b")
        _grant_files_read_write(admin_client, user_a["id"])
        _grant_files_read_write(admin_client, user_b["id"])

        written = user_a_client.post(
            "/api/capabilities/files.local.write",
            json={"arguments": {"path": "user-a-private.txt", "content": "user A confidential content"}},
        ).json()
        assert written["result"]["status"] == "success"
        private_path = written["result"]["receipt"]["path"]

        # User B has Files/read permission generally, but must not be able to read a path
        # that resolves outside their own managed/attachment roots.
        cross_read = user_b_client.post(
            "/api/capabilities/files.local.read",
            json={"arguments": {"path": private_path}},
        ).json()
        assert cross_read["result"]["status"] != "success"
        assert "user A confidential content" not in str(cross_read)

        # The owner can still read their own file.
        own_read = user_a_client.post(
            "/api/capabilities/files.local.read",
            json={"arguments": {"path": private_path}},
        ).json()
        assert own_read["result"]["status"] == "success"
        assert own_read["result"]["content"] == "user A confidential content"

        # User B cannot enumerate the shared managed-files parent directory to discover
        # user A's files even indirectly.
        capability_data_root = str(Path(private_path).parent.parent)
        enumerate_attempt = user_b_client.post(
            "/api/capabilities/files.local.read",
            json={"arguments": {"path": capability_data_root}},
        ).json()
        assert enumerate_attempt["result"]["status"] != "success"

        # A normal user (even with a broad "files" read grant) cannot reach a secret/config
        # path that happens to live under the XV12 root -- denied because it's outside their
        # authorized roots entirely, which is a stricter guarantee than the secret-path check.
        secret_attempt = user_a_client.post(
            "/api/capabilities/files.local.read",
            json={"arguments": {"path": r"X:\XV12\config\.env.local"}},
        ).json()
        assert secret_attempt["result"]["status"] == "permission_denied"

        # The secret-path guard itself: even the administrator, whose roots legitimately
        # cover the repository, is denied the live .env.local file specifically.
        admin_secret_attempt = admin_client.post(
            "/api/capabilities/files.local.read",
            json={"arguments": {"path": r"X:\XV12\config\.env.local"}},
        ).json()
        assert admin_secret_attempt["result"]["status"] == "permission_denied"


@pytest.mark.security
def test_batch_read_enforces_the_same_per_user_authorization(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as admin_client, TestClient(app) as user_a_client, TestClient(app) as user_b_client:
        login(admin_client, "admin")
        user_a = login(user_a_client, "user-a")
        user_b = login(user_b_client, "user-b")
        _grant_files_read_write(admin_client, user_a["id"])
        _grant_files_read_write(admin_client, user_b["id"])
        written = user_a_client.post(
            "/api/capabilities/files.local.write",
            json={"arguments": {"path": "batch-secret.txt", "content": "user A batch content"}},
        ).json()
        private_path = written["result"]["receipt"]["path"]

        result = user_b_client.post(
            "/api/capabilities/files.local.batch_read",
            json={"arguments": {"files": [{"path": private_path}]}},
        ).json()
        assert result["result"]["status"] == "success"
        entry = result["result"]["files"][0]
        assert entry["status"] != "success"
        assert "batch content" not in str(entry)
