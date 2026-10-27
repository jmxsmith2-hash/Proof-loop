"""Read persona source data (CSV, JSON, text) and anonymize it on the way in."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synthux.core.logging import get_logger
from synthux.personas.anonymize import AnonymizationReport, anonymize_record, anonymize_text

log = get_logger(__name__)

COUNT_COLUMNS = ("sessions", "users", "count", "visits", "pageviews", "total_users", "active_users")
ANALYTICS_DIMENSIONS = ("device", "devicecategory", "device_category", "age", "agebracket", "age_bracket",
                        "user_age_bracket", "platform")


@dataclass
class SourceRecord:
    source_file: str
    kind: str                      # survey | review | notes | analytics
    text: str                      # anonymized free text
    fields: dict[str, Any] = field(default_factory=dict)  # anonymized structured fields
    weight: float = 1.0


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _free_text(row: dict[str, Any]) -> str:
    parts = [str(v) for v in row.values() if isinstance(v, str) and len(v.split()) >= 4]
    return " ".join(parts)


def _rows_to_records(rows: list[dict[str, Any]], source: str, report: AnonymizationReport) -> list[SourceRecord]:
    if not rows:
        return []
    keys = {_norm(k) for k in rows[0]}
    is_analytics = bool(keys & set(COUNT_COLUMNS)) and bool(keys & set(ANALYTICS_DIMENSIONS))
    records = []
    for raw in rows:
        clean = {_norm(k): v for k, v in anonymize_record(raw, report).items()}
        if is_analytics:
            weight = next((_to_float(clean[c]) for c in COUNT_COLUMNS if c in clean), 1.0) or 1.0
            records.append(SourceRecord(source, "analytics", "", clean, weight))
        else:
            kind = "review" if keys & {"rating", "stars", "review", "review_text"} else "survey"
            records.append(SourceRecord(source, kind, _free_text(clean), clean))
    return records


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _split_text_entries(text: str) -> list[str]:
    chunks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*•]|\d+[.)]|review\s*\d*:|participant\s*\w*:|p\d+:))", text, flags=re.I)
    return [c.strip(" \n-*•") for c in chunks if len(c.split()) >= 4]


def load_source(path: Path | str, report: AnonymizationReport | None = None) -> list[SourceRecord]:
    """Load one file. Every value is anonymized before it is returned."""
    path = Path(path)
    report = report if report is not None else AnonymizationReport()
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            records = _rows_to_records(list(csv.DictReader(fh)), path.name, report)
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [data])
        rows = [d for d in data if isinstance(d, dict)]
        texts = [d for d in data if isinstance(d, str)]
        records = _rows_to_records(rows, path.name, report)
        records += [SourceRecord(path.name, "notes", anonymize_text(t, report)) for t in texts]
    elif suffix in (".txt", ".md", ".text"):
        entries = _split_text_entries(path.read_text(encoding="utf-8"))
        kind = "review" if "review" in path.stem.lower() else "notes"
        records = [SourceRecord(path.name, kind, anonymize_text(e, report)) for e in entries]
    else:
        raise ValueError(f"unsupported source file type: {path.suffix} ({path})")
    log.info("loaded %d records from %s", len(records), path.name)
    return records


def load_sources(paths: list[Path | str]) -> tuple[list[SourceRecord], AnonymizationReport]:
    report = AnonymizationReport()
    records: list[SourceRecord] = []
    for p in paths:
        records.extend(load_source(p, report))
    if report.total:
        log.info("anonymization removed %d items: %s", report.total, report.replaced)
    return records, report
