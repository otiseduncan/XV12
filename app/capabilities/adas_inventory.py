from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any


class AdasSourceInventory:
    """Enumerate ADAS SI source documents and derive vehicle applications without conflating entity types."""

    _KNOWN_MAKES = tuple(
        sorted(
            {
                "Alfa Romeo",
                "Aston Martin",
                "Land Rover",
                "Mercedes-Benz",
                "Mercedes Benz",
                "Rolls-Royce",
                "Acura",
                "Audi",
                "BMW",
                "Buick",
                "Cadillac",
                "Chevrolet",
                "Chrysler",
                "Dodge",
                "Fiat",
                "Ford",
                "Genesis",
                "GMC",
                "Honda",
                "Hyundai",
                "Infiniti",
                "Jaguar",
                "Jeep",
                "Kia",
                "Lexus",
                "Lincoln",
                "Mazda",
                "Mini",
                "Mitsubishi",
                "Nissan",
                "Pontiac",
                "Porsche",
                "Ram",
                "Subaru",
                "Tesla",
                "Toyota",
                "Volkswagen",
                "Volvo",
            },
            key=len,
            reverse=True,
        )
    )

    _TOPIC_PATTERN = re.compile(
        r"\b(?:"
        r"electronics?|communication(?:s)?|acc|adaptive\s+cruise(?:\s+control)?|"
        r"awd\s+lkas|lkas|lane\s+(?:keep|keeping|change)\s+assist(?:ance)?|"
        r"parking\s+assist(?:ance)?(?:\s+sensor)?|blind\s+spot(?:\s+monitoring)?|"
        r"front\s+camera|rear\s+camera|camera|radar|millimeter\s+wave|"
        r"collision\s+avoidance|driver\s+assist(?:ance)?|adas|service\s+information|"
        r"body\s+electrical|electrical\s+equipment|calibration(?:s)?"
        r")\b",
        re.IGNORECASE,
    )

    def __init__(self, source_root: Path) -> None:
        self.source_root = source_root.resolve()

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", value.replace("_", " ").replace("- ", "-")).strip(" ._-")

    @classmethod
    def _split_make(cls, remainder: str) -> tuple[str | None, str]:
        folded = remainder.casefold()
        for make in cls._KNOWN_MAKES:
            make_folded = make.casefold()
            if folded == make_folded:
                return make, ""
            if folded.startswith(make_folded + " "):
                return make, remainder[len(make) :].strip()
        first, _, tail = remainder.partition(" ")
        return (first or None), tail.strip()

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

        topic_match = cls._TOPIC_PATTERN.search(after_make)
        if topic_match:
            model = cls._clean(after_make[: topic_match.start()])
            topic = cls._clean(after_make[topic_match.start() :])
            confidence = "high" if model else "low"
        else:
            model = cls._clean(after_make)
            topic = None
            confidence = "medium" if model else "low"

        descriptor.update(
            {
                "year": year,
                "make": make,
                "model": model or None,
                "topic": topic,
                "application_parsed": bool(model),
                "parse_confidence": confidence,
            }
        )
        return descriptor

    def snapshot(self) -> dict[str, Any]:
        paths = sorted(self.source_root.rglob("*.pdf"), key=lambda item: str(item).casefold())
        documents = [self.describe_document(self.source_root, path) for path in paths]
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
                "vehicle_applications": "Unique year/make/model applications derived from source-document identity",
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
