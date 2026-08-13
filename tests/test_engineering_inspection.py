from __future__ import annotations

from pathlib import Path

import pytest

from app.capabilities.engineering import RepoInspectionService
from .conftest import login


pytestmark = pytest.mark.engineering


def make_service(tmp_path: Path) -> RepoInspectionService:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def entry():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text("def test_one():\n    assert True\n\ndef test_two():\n    assert True\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Sample\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=leak-me-not", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    long_lines = "\n".join(f"line {i}" for i in range(1, 501))
    (tmp_path / "long_file.py").write_text(long_lines, encoding="utf-8")
    return RepoInspectionService([tmp_path])


def test_map_finds_manifests_entry_points_and_tests(tmp_path):
    service = make_service(tmp_path)
    result = service.map({"path": str(tmp_path)}, {})
    assert result["status"] == "success"
    assert "requirements.txt" in result["manifests"]
    assert "app/main.py" in result["entry_points"]
    assert any("test_sample.py" in item for item in result["test_files"])
    assert "README.md" in result["docs"]


def test_search_finds_text_and_respects_bounds(tmp_path):
    service = make_service(tmp_path)
    result = service.search({"query": "entry", "path": str(tmp_path)}, {})
    assert result["status"] == "success"
    assert any("main.py" in item["path"] for item in result["matches"])
    assert not any(".env" in item["path"] for item in result["matches"])


def test_search_regex_mode(tmp_path):
    service = make_service(tmp_path)
    result = service.search({"query": r"def test_\w+", "mode": "regex", "path": str(tmp_path)}, {})
    assert result["status"] == "success"
    assert result["match_count"] >= 2


def test_ranged_read_returns_line_numbers_and_continuation(tmp_path):
    service = make_service(tmp_path)
    first = service.read({"path": str(tmp_path / "long_file.py"), "start_line": 1, "end_line": 50}, {})
    assert first["status"] == "success"
    assert first["start_line"] == 1 and first["end_line"] == 50
    assert first["total_lines"] == 500
    assert first["has_more"] is True
    assert first["next_start_line"] == 51
    assert "1: line 1" in first["content"]

    continuation = service.read({"path": str(tmp_path / "long_file.py"), "start_line": first["next_start_line"], "end_line": 500}, {})
    assert continuation["status"] == "success"
    assert continuation["start_line"] == 51
    assert continuation["content"].startswith("51: line 51")


def test_batch_read_bounds_file_count_and_size(tmp_path):
    service = make_service(tmp_path)
    result = service.batch_read({"files": [{"path": str(tmp_path / "app" / "main.py")}, {"path": str(tmp_path / "README.md")}]}, {})
    assert result["status"] == "success"
    assert result["files_returned"] == 2
    assert all(item["status"] == "success" for item in result["files"])


def test_sensitive_files_are_denied_through_engineering_read(tmp_path):
    service = make_service(tmp_path)
    result = service.read({"path": str(tmp_path / ".env")}, {})
    assert result["status"] == "permission_denied"


def test_path_outside_configured_roots_is_rejected(tmp_path):
    service = make_service(tmp_path)
    outside = tmp_path.parent / "outside.py"
    outside.write_text("print('x')", encoding="utf-8")
    with pytest.raises(ValueError):
        service.read({"path": str(outside)}, {})


def test_tests_inspect_counts_test_definitions(tmp_path):
    service = make_service(tmp_path)
    result = service.tests_inspect({"path": str(tmp_path)}, {})
    assert result["status"] == "success"
    entry = next(item for item in result["test_files"] if "test_sample.py" in item["path"])
    assert entry["test_definitions"] == 2


def test_git_status_and_diff_report_execution_error_outside_a_repo(tmp_path):
    service = make_service(tmp_path)
    status = service.git_status({"path": str(tmp_path)}, {})
    assert status["status"] == "execution_error"
    diff = service.git_diff({"path": str(tmp_path)}, {})
    assert diff["status"] == "execution_error"


def test_engineering_capabilities_are_admin_only_at_the_registry(client):
    login(client, "user-a")
    listing = client.get("/api/capabilities").json()["capabilities"]
    ids = {item["id"] for item in listing}
    assert not any(item.startswith("engineering.") for item in ids)


def test_admin_can_map_the_real_xv12_repository_through_the_endpoint(client):
    login(client, "admin")
    result = client.post("/api/capabilities/engineering.repo.map", json={"arguments": {"path": r"X:\XV12"}}).json()["result"]
    assert result["status"] == "success"
    assert any("main.py" in item for item in result["entry_points"])


def test_admin_engineering_git_status_reports_real_repo_state(client):
    login(client, "admin")
    result = client.post("/api/capabilities/engineering.git.status", json={"arguments": {"path": r"X:\XV12"}}).json()["result"]
    assert result["status"] == "success"
    assert result["head"]
