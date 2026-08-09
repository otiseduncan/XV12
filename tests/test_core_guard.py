from __future__ import annotations

import json
import subprocess

import pytest

from app.config import ROOT


@pytest.mark.x_core
def test_frozen_core_guard_passes_without_unlock():
    result = subprocess.run(
        [str(ROOT / "runtime" / "python" / "Scripts" / "python.exe"), str(ROOT / "scripts" / "check-core-guard.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["result"] == "PASS"
