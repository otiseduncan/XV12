from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any

from .files import BATCH_READ_FILE_CHAR_LIMIT, BATCH_READ_MAX_FILES, BATCH_READ_TOTAL_CHAR_LIMIT, SensitivePathError, guard_sensitive_path, is_sensitive_path, read_text_range


class RepoInspectionService:
    """Read-only engineering inspection over explicitly configured repository roots.

    This is the safe inspection surface for ordinary (admin) X: map, search, ranged
    reads, Git state, and test inventory — without shell access, writes, sandbox
    execution, or Builder involvement. Mutation, builds, tests, previews, and
    deliverables remain Builder's responsibility.
    """

    IGNORED_DIRS = {".git", "node_modules", ".creator-deps", ".xv12-artifacts", "__pycache__", ".pytest_cache", "dist", "build", ".next", ".venv", "runtime", "models", "data"}
    SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
    CONFIG_NAMES = {
        "package.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml", "pytest.ini", "Dockerfile",
        "docker-compose.yml", "tsconfig.json", "vite.config.js", "vite.config.ts", "webpack.config.js", "runtime.json",
    }
    ENTRY_NAMES = {"main.py", "app.py", "wsgi.py", "asgi.py", "index.js", "index.ts", "index.html", "server.js", "app.js", "main.js", "main.ts"}
    MAX_FILES_SCANNED = 6000
    MAX_LIST = 40
    MAX_SEARCH_RESULTS = 60
    MAX_FILE_BYTES = 750_000
    SEARCH_LINE_CHARS = 200
    GIT_OUTPUT_CHAR_LIMIT = 16_000
    GIT_TIMEOUT_SECONDS = 30
    TEST_DEF_RE = re.compile(r"^\s*(?:def\s+test_\w+|it\(|test\(|describe\()", re.MULTILINE)

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [path.resolve() for path in roots if path]

    def _resolve_root(self, raw: str) -> Path:
        """Resolve a repository target to one of the configured inspection roots."""
        if not str(raw or "").strip():
            return self.roots[0]
        candidate = Path(str(raw)).resolve()
        for root in self.roots:
            if candidate == root or candidate.is_relative_to(root):
                return root
        raise ValueError("Path is outside the configured engineering inspection roots.")

    def _resolve_file(self, raw: str) -> Path:
        path = Path(str(raw or "")).resolve()
        if not any(path == root or path.is_relative_to(root) for root in self.roots):
            raise ValueError("Path is outside the configured engineering inspection roots.")
        guard_sensitive_path(path)
        return path

    def _walk(self, root: Path):
        scanned = 0
        for path in sorted(root.rglob("*")):
            if scanned >= self.MAX_FILES_SCANNED:
                return
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in self.IGNORED_DIRS for part in relative.parts):
                continue
            scanned += 1
            yield path, relative

    def map(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_root(str(arguments.get("path") or ""))
        manifests: list[str] = []
        entry_points: list[str] = []
        tests: list[str] = []
        docs: list[str] = []
        source_dirs: set[str] = set()
        total = 0
        for path, relative in self._walk(root):
            total += 1
            rel_posix = relative.as_posix()
            if path.name in self.CONFIG_NAMES and len(manifests) < self.MAX_LIST:
                manifests.append(rel_posix)
            if path.name in self.ENTRY_NAMES and len(entry_points) < self.MAX_LIST:
                entry_points.append(rel_posix)
            if "test" in rel_posix.casefold() and path.suffix.casefold() in self.SOURCE_SUFFIXES and len(tests) < self.MAX_LIST:
                tests.append(rel_posix)
            if path.suffix.casefold() in {".md", ".rst"} and len(docs) < self.MAX_LIST:
                docs.append(rel_posix)
            if path.suffix.casefold() in self.SOURCE_SUFFIXES and len(relative.parts) > 1:
                source_dirs.add(relative.parts[0])
        return {
            "status": "success", "root": str(root), "manifests": manifests, "entry_points": entry_points,
            "source_dirs": sorted(source_dirs)[: self.MAX_LIST], "test_files": tests, "docs": docs,
            "files_scanned": total, "truncated": total >= self.MAX_FILES_SCANNED,
        }

    def search(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("A search query is required.")
        mode = str(arguments.get("mode") or "text")
        if mode not in {"text", "regex", "filename"}:
            raise ValueError("mode must be text, regex, or filename.")
        limit = max(1, min(int(arguments.get("limit") or 25), self.MAX_SEARCH_RESULTS))
        path_glob = str(arguments.get("path_glob") or "").strip()
        root = self._resolve_root(str(arguments.get("path") or ""))
        pattern: re.Pattern[str] | None = None
        if mode == "regex":
            try:
                pattern = re.compile(query)
            except re.error as error:
                raise ValueError(f"Invalid regular expression: {error}") from error
        matches: list[dict[str, Any]] = []
        files_scanned = 0
        for path, relative in self._walk(root):
            if len(matches) >= limit:
                break
            rel_posix = relative.as_posix()
            if path_glob and not fnmatch.fnmatch(rel_posix, path_glob):
                continue
            if is_sensitive_path(path):
                continue
            if mode == "filename":
                files_scanned += 1
                if query.casefold() in rel_posix.casefold():
                    matches.append({"path": rel_posix, "line": 0, "text": rel_posix})
                continue
            try:
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files_scanned += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                hit = bool(pattern.search(line)) if pattern else (query.casefold() in line.casefold())
                if not hit:
                    continue
                matches.append({"path": rel_posix, "line": line_number, "text": line.strip()[: self.SEARCH_LINE_CHARS]})
                if len(matches) >= limit:
                    break
        return {
            "status": "success" if matches else "no_result", "root": str(root), "query": query, "mode": mode,
            "matches": matches, "match_count": len(matches), "files_scanned": files_scanned,
            "truncated": len(matches) >= limit,
        }

    def read(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        try:
            path = self._resolve_file(str(arguments.get("path") or ""))
        except SensitivePathError as error:
            return {"status": "permission_denied", "message": str(error)}
        if not path.is_file():
            return {"status": "no_result", "path": str(path)}
        if b"\x00" in path.read_bytes()[:4096]:
            return {"status": "partial_success", "path": str(path), "message": "Binary file; content not decoded."}
        start = int(arguments.get("start_line") or 1)
        end = int(arguments.get("end_line") or 0)
        ranged = read_text_range(path, start, end if end else start + 199)
        return {"status": "success", "path": str(path), **ranged, "truncated": ranged["has_more"]}

    def batch_read(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        requests = arguments.get("files") or []
        if not isinstance(requests, list) or not requests:
            raise ValueError("files must be a non-empty array of read requests.")
        results: list[dict[str, Any]] = []
        total_chars = 0
        for item in requests[:BATCH_READ_MAX_FILES]:
            raw = str((item or {}).get("path") or "")
            entry: dict[str, Any] = {"path": raw}
            try:
                path = self._resolve_file(raw)
                if not path.is_file():
                    entry["status"] = "no_result"
                elif b"\x00" in path.read_bytes()[:4096]:
                    entry.update({"status": "partial_success", "message": "Binary file skipped in batch read."})
                else:
                    start = int((item or {}).get("start_line") or 1)
                    end = int((item or {}).get("end_line") or 0)
                    ranged = read_text_range(path, start, end if end else start + 199)
                    entry.update({"status": "success", **ranged, "content": ranged["content"][:BATCH_READ_FILE_CHAR_LIMIT]})
            except SensitivePathError as error:
                entry.update({"status": "permission_denied", "message": str(error)})
            except (ValueError, OSError) as error:
                entry.update({"status": "invalid_arguments", "message": str(error)[:300]})
            total_chars += len(str(entry.get("content") or ""))
            results.append(entry)
            if total_chars > BATCH_READ_TOTAL_CHAR_LIMIT:
                entry["truncated"] = True
                break
        return {"status": "success", "files": results, "files_requested": len(requests), "files_returned": len(results)}

    def _git(self, root: Path, argv: list[str], timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
        # Fixed read-only argv assembled server-side; the caller can only choose which
        # configured root to inspect, never the git subcommand or its flags.
        return subprocess.run(
            ["git", "-C", str(root), *argv], capture_output=True, text=True, timeout=timeout,
        )

    def git_status(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_root(str(arguments.get("path") or ""))
        status = self._git(root, ["status", "--short", "--branch"])
        if status.returncode != 0:
            return {"status": "execution_error", "root": str(root), "message": status.stderr.strip()[:500] or "git status failed"}
        head = self._git(root, ["rev-parse", "HEAD"])
        log = self._git(root, ["log", "--oneline", "-5"])
        return {
            "status": "success", "root": str(root),
            "summary": status.stdout.strip()[: self.GIT_OUTPUT_CHAR_LIMIT],
            "head": head.stdout.strip()[:64] if head.returncode == 0 else "",
            "recent_commits": log.stdout.strip()[:2000] if log.returncode == 0 else "",
        }

    def git_diff(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_root(str(arguments.get("path") or ""))
        argv = ["diff", "--stat"] if arguments.get("stat_only") else ["diff"]
        result = self._git(root, argv)
        if result.returncode != 0:
            return {"status": "execution_error", "root": str(root), "message": result.stderr.strip()[:500] or "git diff failed"}
        output = result.stdout
        return {
            "status": "success" if output.strip() else "no_result", "root": str(root),
            "diff": output[: self.GIT_OUTPUT_CHAR_LIMIT], "truncated": len(output) > self.GIT_OUTPUT_CHAR_LIMIT,
        }

    def tests_inspect(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_root(str(arguments.get("path") or ""))
        inventory: list[dict[str, Any]] = []
        for path, relative in self._walk(root):
            if len(inventory) >= self.MAX_LIST:
                break
            rel_posix = relative.as_posix()
            if "test" not in rel_posix.casefold() or path.suffix.casefold() not in self.SOURCE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > self.MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            inventory.append({
                "path": rel_posix,
                "bytes": path.stat().st_size,
                "test_definitions": len(self.TEST_DEF_RE.findall(text)),
            })
        return {
            "status": "success" if inventory else "no_result", "root": str(root),
            "test_files": inventory, "file_count": len(inventory), "truncated": len(inventory) >= self.MAX_LIST,
        }
