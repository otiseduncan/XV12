from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .adas_inventory import AdasSourceInventory


class AdasSICapability:
    """ADAS SI evidence retrieval and managed annotations; OEM originals stay immutable."""

    def __init__(self, source_root: Path, cache_path: Path, artifacts: Any | None = None) -> None:
        self.source_root = source_root.resolve()
        self.cache_path = cache_path.resolve()
        self.managed_root = self.source_root / "_xv12_managed"
        self.artifacts = artifacts
        self.source_inventory = AdasSourceInventory(self.source_root)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        if self.source_root.is_dir():
            self.managed_root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_path) as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS pages(path TEXT,page INTEGER,text TEXT,source_mtime_ns INTEGER,PRIMARY KEY(path,page)); CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(path,page UNINDEXED,text,content=''); CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT); INSERT OR REPLACE INTO meta VALUES('schema_version','1');""")

    def inventory(self, _arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        if not self.source_root.is_dir():
            return {"status": "unavailable", "authoritative_path": str(self.source_root), "documents": [], "applications": [], "message": "Authoritative ADAS SI source is unavailable."}
        result = self.source_inventory.snapshot()
        result.update({"managed_path": str(self.managed_root), "cache_path": str(self.cache_path)})
        return result

    @staticmethod
    def _tokens(query: str) -> list[str]:
        ignored = {
            "the", "for", "show", "procedure", "calibration", "calibrate", "system", "specific",
            "specifically", "please", "find", "look", "lookup", "need", "want", "vehicle", "model",
        }
        return [
            token for token in re.findall(r"[A-Za-z0-9]+", query.casefold())
            if len(token) > 2 and token not in ignored
        ]

    def _candidate_matches(self, query: str) -> list[dict[str, Any]]:
        matches = self.source_inventory.matching_documents(query, limit=8)
        if matches:
            return matches
        ranked = []
        tokens = self._tokens(query)
        for path in self.source_root.rglob("*.pdf"):
            name = path.name.casefold()
            score = sum(token in name for token in tokens)
            ranked.append((score, path.stat().st_size, path))
        ordered = sorted(ranked, key=lambda item: (-item[0], item[1], str(item[2]).casefold()))[:8]
        return [
            {
                "score": score,
                "path": path,
                "descriptor": self.source_inventory.describe_document(self.source_root, path),
            }
            for score, _size, path in ordered
        ]

    def _candidates(self, query: str) -> list[Path]:
        return [item["path"] for item in self._candidate_matches(query)]

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

    @staticmethod
    def _headings(pages: list[tuple[int, str]]) -> list[dict[str, Any]]:
        headings: list[dict[str, Any]] = []
        pattern = re.compile(r"(?m)^\s*(\d+(?:\.\d+)*)\s+([A-Z][^\n]{2,120})\s*$")
        for page, text in pages:
            for match in pattern.finditer(text):
                if int(match.group(1).split(".")[0]) > 50:
                    continue
                title = re.sub(r"\s+", " ", match.group(2)).strip(" .")
                title = re.sub(r"\s+(?:\.\s*){3,}\d+\s*$", "", title).strip(" .")
                if "copyright" not in title.casefold():
                    headings.append({"number": match.group(1), "title": title, "page": page})
        return headings

    @staticmethod
    def _procedure_marker_score(text: str) -> int:
        folded = text.casefold()
        markers = (
            ("calibrat", 8), ("alignment", 7), ("align ", 5), ("adjustment", 6),
            ("adjust ", 4), ("procedure", 4), ("target", 3), ("diagnostic", 2),
            ("scan tool", 2), ("service function", 3), ("learn", 2), ("aim", 2),
            ("horizontal", 2), ("vertical", 2), ("azimuth", 3), ("elevation", 3),
        )
        return sum(weight for marker, weight in markers if marker in folded)

    @classmethod
    def _procedure_scope(cls, pages: list[tuple[int, str]], best_page: int, query: str) -> dict[str, Any]:
        headings = cls._headings(pages)
        query_tokens = {token for token in re.findall(r"[a-z0-9]+", query.casefold()) if len(token) > 2}
        candidates = []
        for heading in headings:
            if int(heading["page"]) > best_page:
                continue
            title_tokens = {token for token in re.findall(r"[a-z0-9]+", str(heading["title"]).casefold()) if len(token) > 2}
            overlap = len(query_tokens & title_tokens)
            calibration_bonus = 4 if "calibrat" in str(heading["title"]).casefold() and "calibrat" in query.casefold() else 0
            alignment_bonus = 3 if "align" in str(heading["title"]).casefold() else 0
            candidates.append((overlap + calibration_bonus + alignment_bonus, int(heading["page"]), heading))
        selected = max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates and max(item[0] for item in candidates) > 0 else None
        if not selected:
            return {
                "page_start": best_page, "page_end": best_page, "section_page_start": best_page,
                "section_page_end": best_page, "artifact_title": "Relevant source page",
                "section_title": "Relevant source page", "subsection_title": None, "procedure_heading": None,
            }
        number = str(selected["number"])
        level = len(number.split("."))
        start = int(selected["page"])
        next_peer = next(
            (item for item in headings if int(item["page"]) > start and len(str(item["number"]).split(".")) <= level),
            None,
        )
        end = min((int(next_peer["page"]) - 1) if next_peer else start + 8, pages[-1][0], start + 12)
        parent_number = number.split(".")[0]
        parent = next(
            (item for item in reversed(headings) if int(item["page"]) <= start and str(item["number"]) == parent_number),
            selected,
        )
        section_start = int(parent["page"])
        next_section = next(
            (item for item in headings if int(item["page"]) > section_start and str(item["number"]).split(".")[0] != parent_number),
            None,
        )
        section_end = min((int(next_section["page"]) - 1) if next_section else end, pages[-1][0])
        raw_title = str(selected["title"])
        artifact_title = re.sub(r",\s*Calibrating\b", " — Calibration", raw_title, flags=re.IGNORECASE)
        subsection = "Calibration" if "calibrat" in raw_title.casefold() else raw_title
        procedure_heading = None
        for page, text in pages:
            if start <= page <= end:
                match = re.search(r"(?mi)^\s*((?:Calibrating\s+)?Procedure)\s*$", text)
                if match:
                    procedure_heading = re.sub(r"\s+", " ", match.group(1)).strip()
                    break
        return {
            "page_start": start, "page_end": max(start, end),
            "section_page_start": section_start, "section_page_end": max(section_start, section_end),
            "artifact_title": artifact_title, "section_title": str(parent["title"]),
            "subsection_title": subsection, "procedure_heading": procedure_heading,
        }

    @staticmethod
    def _descriptor_tokens(descriptor: dict[str, Any]) -> set[str]:
        values = [descriptor.get("year"), descriptor.get("make"), descriptor.get("model"), descriptor.get("drivetrain")]
        return {
            token for value in values if value
            for token in re.findall(r"[a-z0-9]+", str(value).casefold())
            if len(token) > 2
        }

    def search(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        if not self.source_root.is_dir():
            return {"status": "unavailable", "query": query, "results": [], "artifacts": [], "source": "ADAS SI", "message": "Authoritative ADAS SI source is unavailable."}

        query_tokens = self._tokens(query)
        candidate_matches = self._candidate_matches(query)
        candidates = [item["path"] for item in candidate_matches]
        match_by_name = {item["path"].name: item for item in candidate_matches}
        source_lookup = {path.name: path for path in candidates}
        page_lookup: dict[str, list[tuple[int, str]]] = {}
        results: list[dict[str, Any]] = []

        for path in candidates:
            match = match_by_name[path.name]
            descriptor = dict(match.get("descriptor") or {})
            filename_score = int(match.get("score") or 0)
            identity_tokens = self._descriptor_tokens(descriptor)
            content_tokens = [token for token in query_tokens if token not in identity_tokens]
            try:
                pages = self._pages(path)
                page_lookup[path.name] = pages
                for page, text in pages:
                    folded = text.casefold()
                    lexical_score = sum(min(folded.count(token), 3) for token in content_tokens)
                    marker_score = self._procedure_marker_score(text)
                    score = (lexical_score * 4) + marker_score
                    if filename_score >= 10 and marker_score > 0:
                        score += min(filename_score // 3, 8)
                    elif filename_score >= 10 and lexical_score > 0:
                        score += min(filename_score // 4, 6)
                    threshold = 2 if filename_score >= 10 else max(2, min(4, max(1, len(content_tokens)) // 2))
                    if score < threshold:
                        continue
                    positions = [folded.find(token) for token in content_tokens if folded.find(token) >= 0]
                    if not positions:
                        marker_positions = [
                            folded.find(marker) for marker in ("calibrat", "alignment", "adjustment", "procedure", "target")
                            if folded.find(marker) >= 0
                        ]
                        positions = marker_positions
                    start = max(0, (min(positions) if positions else 0) - 350)
                    excerpt = re.sub(r"\s+", " ", text[start:start + 1800]).strip()
                    if excerpt:
                        results.append(
                            {
                                "source": path.name,
                                "title": path.stem,
                                "page": page,
                                "excerpt": excerpt,
                                "match_score": score,
                                "source_match_score": filename_score,
                                "vehicle": {
                                    key: descriptor.get(key)
                                    for key in ("year", "make", "model", "drivetrain", "platform_code", "topic")
                                    if descriptor.get(key) is not None
                                },
                            }
                        )
            except Exception as error:
                results.append({"source": path.name, "status": "partial_success", "error": type(error).__name__, "source_match_score": filename_score})

        ranked = sorted(
            results,
            key=lambda item: (int(item.get("source_match_score", 0)), int(item.get("match_score", 0))),
            reverse=True,
        )[:8]
        artifacts: list[dict[str, Any]] = []
        best = next((item for item in ranked if item.get("excerpt") and item.get("source") in source_lookup), None)

        if best and self.artifacts is not None:
            try:
                scope = self._procedure_scope(page_lookup[str(best["source"])], int(best["page"]), query)
                scoped_text = "\n\n".join(
                    text for page, text in page_lookup[str(best["source"])]
                    if int(scope["page_start"]) <= page <= int(scope["page_end"])
                )
                artifacts.append(
                    self.artifacts.register_file(
                        user_id=_user["id"], capability_id="adas.si.search",
                        source_path=source_lookup[str(best["source"])], title=str(scope["artifact_title"]),
                        source_title=str(best["source"]), source_label="ADAS SI", requested_scope=query,
                        scope_kind="procedure", page_start=int(scope["page_start"]), page_end=int(scope["page_end"]),
                        section_title=str(scope["section_title"]), subsection_title=scope["subsection_title"],
                        section_page_start=int(scope["section_page_start"]), section_page_end=int(scope["section_page_end"]),
                        relevant_text=scoped_text,
                        metadata={
                            "query": query, "evidence_page": int(best["page"]),
                            "procedure_heading": scope["procedure_heading"],
                            "source_match_score": int(best.get("source_match_score") or 0),
                        },
                    )
                )
            except ValueError:
                pass

        strongest = candidate_matches[0] if candidate_matches else None
        exact_source_matched = bool(strongest and int(strongest.get("score") or 0) >= 10)
        if not best and exact_source_matched and self.artifacts is not None:
            try:
                descriptor = dict(strongest.get("descriptor") or {})
                path = strongest["path"]
                artifacts.append(
                    self.artifacts.register_file(
                        user_id=_user["id"], capability_id="adas.si.search", source_path=path,
                        title=str(descriptor.get("title") or path.name), source_title=path.name,
                        source_label="ADAS SI", requested_scope=query, scope_kind="full",
                        metadata={"query": query, "source_match_score": int(strongest.get("score") or 0), "text_evidence_extracted": False},
                    )
                )
            except ValueError:
                pass

        matched_documents = [
            {
                "title": str(item["descriptor"].get("title") or item["path"].stem),
                "source": item["path"].name,
                "source_match_score": int(item.get("score") or 0),
                "year": item["descriptor"].get("year"),
                "make": item["descriptor"].get("make"),
                "model": item["descriptor"].get("model"),
                "drivetrain": item["descriptor"].get("drivetrain"),
                "platform_code": item["descriptor"].get("platform_code"),
                "topic": item["descriptor"].get("topic"),
            }
            for item in candidate_matches[:5]
        ]

        status = "success" if best else "partial_success" if exact_source_matched else "no_result"
        return {
            "status": status,
            "query": query,
            "results": ranked,
            "matched_documents": matched_documents,
            "exact_source_matched": exact_source_matched,
            "artifacts": artifacts,
            "source": "ADAS SI",
            "broader_search_performed": True,
            "message": None if best else "The requested ADAS SI source document was matched, but no extractable procedure text was found; use the returned source artifact rather than claiming the source is absent." if exact_source_matched else None,
            "evidence_contract": {
                "authoritative_records_only": True,
                "specific_facts_traceable_to_results": True,
                "do_not_infer_missing_records": True,
                "matched_source_is_not_a_no_result": True,
            },
        }

    def write(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        if not self.source_root.is_dir():
            return {"status": "unavailable", "executed": False, "message": "Authoritative ADAS SI source is unavailable."}
        self.managed_root.mkdir(parents=True, exist_ok=True)
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
        if not self.source_root.is_dir():
            return {"status": "unavailable", "executed": False, "message": "Authoritative ADAS SI source is unavailable."}
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
