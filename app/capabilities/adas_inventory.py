from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any


class AdasSourceInventory:
    """Enumerate ADAS SI source documents without confusing vehicle, variant, and system identity."""

    _KNOWN_MAKES = tuple(
        sorted(
            {
                "Alfa Romeo", "Aston Martin", "Land Rover", "Mercedes-Benz", "Mercedes Benz",
                "Rolls-Royce", "Acura", "Audi", "BMW", "Buick", "Cadillac", "Chevrolet",
                "Chrysler", "Dodge", "Fiat", "Ford", "Genesis", "GMC", "Honda", "Hyundai",
                "Infiniti", "Jaguar", "Jeep", "Kia", "Lexus", "Lincoln", "Mazda", "Mini",
                "Mitsubishi", "Nissan", "Pontiac", "Porsche", "Ram", "Subaru", "Tesla",
                "Toyota", "Volkswagen", "Volvo",
            },
            key=len,
            reverse=True,
        )
    )
    _MAKE_ALIASES = {"chevy": "Chevrolet", "mercedes benz": "Mercedes-Benz"}
    _MODEL_MAKE_HINTS = {"forester": "Subaru"}
    _BODY_PREFIXES = {"truck", "car", "suv", "crossover"}
    _DRIVETRAINS = {"awd", "fwd", "rwd", "4wd", "2wd", "4x4"}
    _TOPIC_PATTERN = re.compile(
        r"\b(?:"
        r"360|acc|bsm|ccm|sodcm(?:c|d)?|lkas|scc|sas|eyesight|monocam|"
        r"parking\s+aid(?:\s+(?:azimuth|elevation))?|parking\s+assist(?:ance)?(?:\s+sensor)?|"
        r"azimuth|elevation|panoramic|mil(?:l)?imeter\s+wave|"
        r"electronics?|communication(?:s)?|adaptive\s+cruise(?:\s+control)?|"
        r"lane\s+(?:keep|keeping|change)\s+assist(?:ance)?|blind\s+spot(?:\s+monitoring)?|"
        r"front\s+camera|rear\s+camera|camera|radar|collision\s+avoidance|"
        r"driver\s+assist(?:ance)?|adas|service\s+information|body\s+electrical|"
        r"electrical\s+equipment|calibration(?:s)?"
        r")\b",
        re.IGNORECASE,
    )
    _PLATFORM_PATTERN = re.compile(r"\(([^()]{1,24})\)")

    def __init__(self, source_root: Path) -> None:
        self.source_root = source_root.resolve()

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", value.replace("_", " ").replace("- ", "-")).strip(" ._-")

    @staticmethod
    def _norm(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()

    @classmethod
    def _split_make(cls, remainder: str) -> tuple[str | None, str]:
        folded = cls._norm(remainder)
        for alias, canonical in cls._MAKE_ALIASES.items():
            if folded == alias or folded.startswith(alias + " "):
                consumed = len(remainder.split()[0]) if alias == "chevy" else len(alias)
                return canonical, remainder[consumed:].strip()
        for make in cls._KNOWN_MAKES:
            make_folded = cls._norm(make)
            if folded == make_folded:
                return make, ""
            if folded.startswith(make_folded + " "):
                raw_words = remainder.split()
                make_words = make.replace("-", " ").split()
                return make, " ".join(raw_words[len(make_words):]).strip()
        first = remainder.split()[0] if remainder.split() else ""
        hint = cls._MODEL_MAKE_HINTS.get(cls._norm(first))
        if hint:
            return hint, remainder
        first, _, tail = remainder.partition(" ")
        return (first or None), tail.strip()

    @staticmethod
    def _canonical_model(model: str) -> str:
        value = re.sub(r"\s+", " ", model).strip(" ._-")
        value = re.sub(r"\bF\s*[- ]?\s*150\b", "F-150", value, flags=re.IGNORECASE)
        if value.isupper():
            normalized = []
            for token in value.split():
                if any(character.isdigit() for character in token) or (token.isalpha() and len(token) <= 3):
                    normalized.append(token)
                else:
                    normalized.append(token.title())
            value = " ".join(normalized)
        return value

    @classmethod
    def describe_document(cls, source_root: Path, path: Path) -> dict[str, Any]:
        relative = path.relative_to(source_root)
        title = cls._clean(path.stem)
        descriptor: dict[str, Any] = {
            "title": title,
            "relative_path": str(relative),
            "size_bytes": path.stat().st_size,
            "year": None,
            "make": None,
            "model": None,
            "drivetrain": None,
            "platform_code": None,
            "topic": None,
            "application_parsed": False,
            "parse_confidence": "none",
        }
        match = re.match(r"^((?:19|20)\d{2})\s+(.+)$", title)
        if not match:
            return descriptor

        year = int(match.group(1))
        remainder = cls._clean(match.group(2))
        make, after_make = cls._split_make(remainder)
        if not make or not after_make:
            descriptor.update({"year": year, "make": make, "parse_confidence": "low"})
            return descriptor

        words = after_make.split()
        if words and words[0].casefold() in cls._BODY_PREFIXES:
            after_make = " ".join(words[1:]).strip()

        platform_match = cls._PLATFORM_PATTERN.search(after_make)
        platform_code = platform_match.group(1).strip() if platform_match else None
        topic_match = cls._TOPIC_PATTERN.search(after_make)
        drivetrain_match = re.search(r"\b(?:AWD|FWD|RWD|4WD|2WD|4X4)\b", after_make, flags=re.IGNORECASE)
        boundaries = [match.start() for match in (topic_match, drivetrain_match, platform_match) if match]
        boundary = min(boundaries) if boundaries else len(after_make)
        model = cls._canonical_model(after_make[:boundary])

        tail = after_make[boundary:]
        drivetrain_search = re.search(r"\b(?:AWD|FWD|RWD|4WD|2WD|4X4)\b", tail, flags=re.IGNORECASE)
        drivetrain = drivetrain_search.group(0).upper() if drivetrain_search else None
        topic_search = cls._TOPIC_PATTERN.search(tail)
        topic = cls._clean(topic_search.group(0)) if topic_search else None
        confidence = "high" if model and (topic or drivetrain or platform_code) else "medium" if model else "low"

        descriptor.update(
            {
                "year": year,
                "make": make,
                "model": model or None,
                "drivetrain": drivetrain,
                "platform_code": platform_code,
                "topic": topic,
                "application_parsed": bool(model),
                "parse_confidence": confidence,
            }
        )
        return descriptor

    def _documents(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.source_root.is_dir():
            return []
        paths = sorted(self.source_root.rglob("*.pdf"), key=lambda item: str(item).casefold())
        return [(path, self.describe_document(self.source_root, path)) for path in paths]

    def matching_documents(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Resolve source identity before page-content search."""
        query_norm = self._norm(query)
        query_tokens = set(query_norm.split())
        generic = {"show", "find", "display", "procedure", "calibration", "calibrate", "system", "specific", "specifically", "please", "the", "for"}
        useful = query_tokens - generic
        ranked: list[tuple[int, str, Path, dict[str, Any]]] = []
        for path, descriptor in self._documents():
            title_norm = self._norm(str(descriptor["title"]))
            title_tokens = set(title_norm.split())
            score = len(useful & title_tokens)
            year = descriptor.get("year")
            if year and str(year) in query_tokens:
                score += 4
            make = descriptor.get("make")
            if make and set(self._norm(str(make)).split()) <= query_tokens:
                score += 4
            model = descriptor.get("model")
            if model:
                model_tokens = set(self._norm(str(model)).split())
                if model_tokens and model_tokens <= query_tokens:
                    score += 7
            topic = descriptor.get("topic")
            if topic and set(self._norm(str(topic)).split()) & query_tokens:
                score += 6
            drivetrain = descriptor.get("drivetrain")
            if drivetrain and self._norm(str(drivetrain)) in query_tokens:
                score += 2
            if title_norm and title_norm in query_norm:
                score += 12
            if score > 0:
                ranked.append((score, title_norm, path, descriptor))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [
            {"score": score, "path": path, "descriptor": descriptor}
            for score, _title, path, descriptor in ranked[: max(1, min(limit, 25))]
        ]

    def snapshot(self) -> dict[str, Any]:
        document_pairs = self._documents()
        documents = [descriptor for _path, descriptor in document_pairs]
        grouped: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
        for document in documents:
            if not document["application_parsed"]:
                continue
            key = (int(document["year"]), str(document["make"]), str(document["model"]))
            grouped[key].append(document)

        applications = []
        for (year, make, model), supporting in sorted(
            grouped.items(), key=lambda item: (item[0][0], item[0][1].casefold(), item[0][2].casefold())
        ):
            applications.append(
                {
                    "year": year,
                    "make": make,
                    "model": model,
                    "document_count": len(supporting),
                    "drivetrains": sorted({str(item["drivetrain"]) for item in supporting if item.get("drivetrain")}),
                    "platform_codes": sorted({str(item["platform_code"]) for item in supporting if item.get("platform_code")}),
                    "topics": sorted({str(item["topic"]) for item in supporting if item.get("topic")}, key=str.casefold),
                    "source_documents": [item["title"] for item in supporting],
                    "source_paths": [item["relative_path"] for item in supporting],
                    "provenance": "adas_si_source_filename_inventory",
                }
            )

        unparsed = [item for item in documents if not item["application_parsed"]]
        return {
            "status": "success",
            "authoritative_path": str(self.source_root),
            "summary": {
                "document_count": len(documents),
                "vehicle_application_count": len(applications),
                "parsed_document_count": len(documents) - len(unparsed),
                "unparsed_document_count": len(unparsed),
            },
            "entity_semantics": {
                "documents": "OEM source PDF files in the authoritative ADAS SI library",
                "vehicle_applications": "Unique canonical year/make/model vehicles represented by one or more source documents",
                "document_topics": "ADAS system/topic suffixes such as CCM, BSM, LKAS, SODCM, SCC, 360, azimuth, and elevation are source-document topics, not separate vehicle models",
                "vehicle_variants": "Drivetrain and platform codes are retained as attributes and do not create a new year/make/model application",
                "verification": "Source-library operator verification is distinct from normalized-database verification records and review queues",
                "counts_are_not_interchangeable": True,
            },
            "verification": {
                "source_library_status": "operator_verified",
                "verified_by": "Otis",
                "asserted_on": "2026-08-09",
                "scope": "ADAS SI source entries present at the known-good baseline",
                "pipeline_metrics_are_separate": True,
            },
            "documents": documents,
            "applications": applications,
            "unparsed_documents": [item["title"] for item in unparsed],
            "evidence_contract": {
                "authoritative_records_only": True,
                "records_are_enumerated": True,
                "do_not_infer_records_from_counts": True,
            },
        }
