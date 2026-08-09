from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.standalone
def test_runtime_paths_are_xv12_owned_and_no_historical_imports() -> None:
    root = Path.cwd().resolve()
    runtime = json.loads((root / "config" / "runtime.json").read_text(encoding="utf-8"))
    paths = [runtime["model"]["executable"], runtime["model"]["path"], runtime["storage"]["database"], runtime["storage"]["attachments"], runtime["storage"]["adas_database"]]
    assert all((root / value).resolve().is_relative_to(root) for value in paths)
    forbidden = ("x" + "v11", "b" + "b1", "xoduz" + "11")
    for path in (root / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8").casefold()
        assert not any(token in text for token in forbidden), path
