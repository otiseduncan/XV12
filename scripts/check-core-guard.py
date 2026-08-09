from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "core-baseline-manifest.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def identity_contract_hash() -> str:
    tree = ast.parse((ROOT / "app" / "context.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "IDENTITY_CONTRACT" for target in node.targets):
            value = ast.literal_eval(node.value)
            return sha256_bytes(value.encode("utf-8"))
    raise RuntimeError("IDENTITY_CONTRACT was not found")


def stream_chat_hash() -> str:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "stream_chat"
    )
    segment = "".join(lines[node.lineno - 1 : node.end_lineno])
    return sha256_bytes(segment.encode("utf-8"))


def run(full: bool = False) -> dict[str, object]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failures: list[str] = []
    protected = manifest["protected_core"]
    for relative, expected in protected["files"].items():
        actual = sha256_file(ROOT / relative)
        if actual != expected:
            failures.append(f"protected core file changed: {relative}")
    if stream_chat_hash() != protected["main_stream_chat_sha256"]:
        failures.append("protected stream_chat contract changed")
    if identity_contract_hash() != manifest["identity_contract"]["sha256"]:
        failures.append("XODUZ identity contract changed")

    from app.registry import CapabilityGateway, CapabilityRegistry

    actual_signatures = {
        "list_for": str(inspect.signature(CapabilityRegistry.list_for)),
        "model_tools": str(inspect.signature(CapabilityRegistry.model_tools)),
        "register": str(inspect.signature(CapabilityGateway.register)),
        "execute": str(inspect.signature(CapabilityGateway.execute)),
    }
    if actual_signatures != protected["registry_interface_signatures"]:
        failures.append("capability-selection or execution-gateway interface changed")

    runtime = json.loads((ROOT / "config" / "runtime.json").read_text(encoding="utf-8"))["model"]
    contract = manifest["runtime_contract"]
    for key in ("alias", "context_tokens", "max_response_tokens", "temperature", "gpu_layers", "parallel"):
        if runtime[key] != contract[key]:
            failures.append(f"protected model runtime value changed: {key}")
    for section in ("model", "llama_cpp"):
        record = manifest[section]
        path = ROOT / record["repository_relative_path"]
        if not path.exists() or path.stat().st_size != record["size_bytes"]:
            failures.append(f"protected {section} file missing or size changed")
        elif full and sha256_file(path) != record["sha256"]:
            failures.append(f"protected {section} hash changed")

    return {
        "result": "PASS" if not failures else "FAIL",
        "baseline_tag": manifest["baseline"]["tag"],
        "baseline_sha": manifest["baseline"]["git_sha"],
        "full_hash_verification": full,
        "failures": failures,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    result = run(args.full)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["result"] == "PASS" else 1)
