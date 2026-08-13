from __future__ import annotations

from pathlib import Path

import pytest

from app.capabilities.files import LocalFilesCapability
from app.registry import CapabilityRegistry


OWNER = {"id": "owner", "role": "admin", "status": "active", "google_sub": "owner-google-sub"}
USER = {"id": "user-a", "role": "user", "status": "active"}


def make_capability(tmp_path: Path) -> tuple[LocalFilesCapability, Path, Path, Path]:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    sandbox = tmp_path / "xoduz-sandbox"
    root_a.mkdir()
    root_b.mkdir()
    sandbox.mkdir()
    capability = LocalFilesCapability(
        read_roots=[root_a, root_b],
        managed_root=tmp_path / "managed",
        attachments_root=tmp_path / "attachments",
        admin_sandbox_root=sandbox,
    )
    return capability, root_a, root_b, sandbox


def test_owner_can_move_file_between_authorized_roots_outside_sandbox(tmp_path: Path) -> None:
    capability, root_a, root_b, _sandbox = make_capability(tmp_path)
    source = root_a / "outside-a.txt"
    destination = root_b / "outside-b.txt"
    source.write_text("move me", encoding="utf-8")

    result = capability.modify({"path": str(source), "destination": str(destination)}, OWNER)

    assert result["status"] == "success"
    assert result["operation"] == "move"
    assert result["receipt"]["from"] == str(source.resolve())
    assert result["receipt"]["to"] == str(destination.resolve())
    assert result["receipt"]["overwrote"] is False
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "move me"


def test_owner_move_never_overwrites_existing_destination(tmp_path: Path) -> None:
    capability, root_a, root_b, _sandbox = make_capability(tmp_path)
    source = root_a / "source.txt"
    destination = root_b / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        capability.modify({"path": str(source), "destination": str(destination)}, OWNER)

    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "keep"


def test_owner_cannot_move_file_to_unconfigured_host_path(tmp_path: Path) -> None:
    capability, root_a, _root_b, _sandbox = make_capability(tmp_path)
    source = root_a / "source.txt"
    source.write_text("source", encoding="utf-8")
    unauthorized = tmp_path / "not-authorized" / "destination.txt"
    unauthorized.parent.mkdir()

    with pytest.raises(ValueError, match="configured Local Files roots"):
        capability.modify({"path": str(source), "destination": str(unauthorized)}, OWNER)

    assert source.exists()
    assert not unauthorized.exists()


def test_owner_cannot_move_sensitive_file_or_move_into_sensitive_name(tmp_path: Path) -> None:
    capability, root_a, root_b, _sandbox = make_capability(tmp_path)
    sensitive_source = root_a / ".env"
    sensitive_source.write_text("SECRET=value", encoding="utf-8")
    normal_source = root_a / "normal.txt"
    normal_source.write_text("normal", encoding="utf-8")

    with pytest.raises(PermissionError):
        capability.modify({"path": str(sensitive_source), "destination": str(root_b / "moved.txt")}, OWNER)
    with pytest.raises(PermissionError):
        capability.modify({"path": str(normal_source), "destination": str(root_b / "credentials.txt")}, OWNER)

    assert sensitive_source.exists()
    assert normal_source.exists()


def test_move_can_optionally_use_sha_guard(tmp_path: Path) -> None:
    capability, root_a, root_b, _sandbox = make_capability(tmp_path)
    source = root_a / "guarded.txt"
    source.write_text("guarded", encoding="utf-8")
    destination = root_b / "guarded-moved.txt"

    read = capability.read({"path": str(source)}, OWNER)
    with pytest.raises(ValueError, match="expected SHA256"):
        capability.modify({"path": str(source), "destination": str(destination), "expected_sha256": "0" * 64}, OWNER)

    result = capability.modify({"path": str(source), "destination": str(destination), "expected_sha256": read["sha256"]}, OWNER)
    assert result["status"] == "success"
    assert destination.exists()


def test_normal_user_move_remains_inside_private_managed_root(tmp_path: Path) -> None:
    capability, root_a, _root_b, _sandbox = make_capability(tmp_path)
    written = capability.write({"path": "first.txt", "content": "private"}, USER)
    source = Path(written["receipt"]["path"])
    destination = source.with_name("second.txt")

    result = capability.modify({"path": str(source), "destination": str(destination)}, USER)
    assert result["status"] == "success"
    assert not source.exists() and destination.exists()

    outside = root_a / "outside.txt"
    with pytest.raises(ValueError, match="managed root"):
        capability.modify({"path": str(destination), "destination": str(outside)}, USER)


def test_content_replacement_still_requires_sha_and_stays_in_writable_root(tmp_path: Path) -> None:
    capability, _root_a, _root_b, sandbox = make_capability(tmp_path)
    written = capability.write({"path": str(sandbox / "editable.txt"), "content": "one"}, OWNER)
    with pytest.raises(ValueError, match="requires expected_sha256"):
        capability.modify({"path": str(sandbox / "editable.txt"), "content": "two"}, OWNER)

    changed = capability.modify({
        "path": str(sandbox / "editable.txt"),
        "content": "two",
        "expected_sha256": written["receipt"]["sha256"],
    }, OWNER)
    assert changed["status"] == "success"
    assert (sandbox / "editable.txt").read_text(encoding="utf-8") == "two"


def test_registry_exposes_move_through_modify_but_no_delete_capability() -> None:
    registry = CapabilityRegistry(Path("config/capabilities.v1.json"))
    modify = registry.capabilities["files.local.modify"]

    assert "destination" in modify["arguments_schema"]["properties"]
    assert "move" in modify["description"].casefold()
    assert "delete" in modify["description"].casefold()
    assert "files.local.delete" not in registry.capabilities
