from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader


class AdasSICapability:
    """ADAS SI evidence retrieval and managed annotations; OEM originals stay immutable."""

    def __init__(self, source_root: Path, cache_path: Path) -> None:
        self.source_root = source_root.resolve()
        self.cache_path = cache_path.resolve()
        self.managed_root = self.source_root / "_xv12_managed"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.managed_root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_path) as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS pages(path TEXT,page INTEGER,text TEXT,source_mtime_ns INTEGER,PRIMARY KEY(path,page)); CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(path,page UNINDEXED,text,content=''); CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT); INSERT OR REPLACE INTO meta VALUES('schema_version','1');""")

    def inventory(self, _arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        documents = list(self.source_root.rglob("*.pdf"))
        return {"status": "success", "authoritative_path": str(self.source_root), "pdf_count": len(documents), "managed_path": str(self.managed_root), "cache_path": str(self.cache_path)}

    @staticmethod
    def _tokens(query: str) -> list[str]:
        return [token for token in re.findall(r"[A-Za-z0-9]+", query.casefold()) if len(token) > 2 and token not in {"the", "for", "show", "procedure", "calibration"}]

    def _candidates(self, query: str) -> list[Path]:
        tokens = self._tokens(query)
        ranked = []
        for path in self.source_root.rglob("*.pdf"):
            name = path.name.casefold()
            score = sum(token in name for token in tokens)
            ranked.append((score, path.stat().st_size, path))
        ordered = sorted(ranked, key=lambda item: (-item[0], item[1], str(item[2]).casefold()))
        named = [item[2] for item in ordered if item[0] > 0]
        return (named or [item[2] for item in ordered])[:8]

    def _pages(self, path: Path) -> list[tuple[int, str]]:
        mtime = path.stat().st_mtime_ns
        with sqlite3.connect(self.cache_path) as db:
            cached = db.execute("SELECT page,text FROM pages WHERE path=? AND source_mtime_ns=? ORDER BY page", (str(path), mtime)).fetchall()
            if cached:
                return [(int(page), str(text)) for page, text in cached]
        reader = PdfReader(str(path), strict=False)
        pages = [(number, (page.extract_text() or "")[:250000]) for number, page in enumerate(reader.pages, 1)]
        with sqlite3.connect(self.cache_path) as db:
            db.execute("DELETE FROM pages WHERE path=?", (str(path),))
            db.executemany("INSERT INTO pages(path,page,text,source_mtime_ns) VALUES(?,?,?,?)", [(str(path), number, text, mtime) for number, text in pages])
        return pages

    def search(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        tokens = self._tokens(query)
        results = []
        for path in self._candidates(query):
            try:
                for page, text in self._pages(path):
                    folded = text.casefold()
                    score = sum(token in folded for token in tokens)
                    if score < max(2, min(4, len(tokens) // 2)):
                        continue
                    positions = [folded.find(token) for token in tokens if folded.find(token) >= 0]
                    start = max(0, (min(positions) if positions else 0) - 350)
                    excerpt = re.sub(r"\s+", " ", text[start:start + 1400]).strip()
                    results.append({"source": str(path), "title": path.stem, "page": page, "excerpt": excerpt, "match_score": score})
            except Exception as error:
                results.append({"source": str(path), "status": "partial_success", "error": type(error).__name__})
        ranked = sorted(results, key=lambda item: int(item.get("match_score", 0)), reverse=True)[:8]
        return {"status": "success" if any("excerpt" in item for item in ranked) else "no_result", "query": query, "results": ranked, "authoritative_path": str(self.source_root), "broader_search_performed": True}

    def write(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        record_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(arguments.get("record_id") or ""))
        if not record_id:
            raise ValueError("record_id is required")
        target = self.managed_root / f"{record_id}.json"
        if target.exists():
            raise ValueError("Managed ADAS record already exists; use modify.")
        payload = {"record_id": record_id, "title": str(arguments.get("title") or ""), "content": str(arguments.get("content") or ""), "created_by": user["id"], "version": 1, "updated_at": datetime.now(UTC).isoformat()}
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return {"status": "success", "receipt": {"operation": "write", "path": str(target), "version": 1, "sha256": digest, "originals_modified": False}}

    def modify(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        record_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(arguments.get("record_id") or ""))
        target = self.managed_root / f"{record_id}.json"
        if not target.is_file():
            return {"status": "no_result", "record_id": record_id}
        payload = json.loads(target.read_text(encoding="utf-8"))
        expected = int(arguments.get("expected_version") or 0)
        if int(payload["version"]) != expected:
            raise ValueError("Managed record version conflict.")
        payload.update({"title": str(arguments.get("title", payload["title"])), "content": str(arguments.get("content", payload["content"])), "updated_by": user["id"], "version": expected + 1, "updated_at": datetime.now(UTC).isoformat()})
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {"status": "success", "receipt": {"operation": "modify", "path": str(target), "version": payload["version"], "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "originals_modified": False}}
