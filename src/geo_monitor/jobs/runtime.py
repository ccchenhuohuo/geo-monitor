"""Runtime accounting and resume matching for job execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.registry import get_adapter
from ..config import Settings
from ..exporters import latest_live_terminal_records, latest_records, read_jsonl, successful_result_hashes
from ..runner import compute_request_hash
from ..schemas import QueryRecord


def run_completion_statuses(*, dry_run: bool, mock: bool) -> set[str]:
    if dry_run:
        return {"dry_run"}
    if mock:
        return {"mock"}
    return {"success"}


def expected_units_for_queries(queries: list[Any], repeats: int) -> set[tuple[str, int]]:
    return {(str(query.query_id), repeat) for query in queries for repeat in range(1, repeats + 1)}


def completed_unit_count(raw_path: Path, statuses: set[str], *, expected_units: set[tuple[str, int]] | None = None) -> int:
    if not raw_path.exists():
        return 0
    if statuses == {"success"}:
        records = latest_live_terminal_records(read_jsonl(raw_path, strict=False))
    elif statuses == {"mock"}:
        records = latest_records(read_jsonl(raw_path, strict=False), statuses={"mock"})
    elif statuses == {"dry_run"}:
        records = latest_records(read_jsonl(raw_path, strict=False), statuses={"dry_run"})
    else:
        records = latest_records(read_jsonl(raw_path, strict=False), statuses=set(statuses))
    records = [record for record in records if record.get("status") in statuses]
    if expected_units is None:
        return len(records)
    return sum(1 for record in records if (str(record.get("query_id")), int(record.get("repeat_index") or 1)) in expected_units)


def resume_matched_unit_count(raw_path: Path, queries: list[QueryRecord], manifest: dict[str, Any], settings: Settings) -> int:
    done_hashes = successful_result_hashes(raw_path)
    if not done_hashes:
        return 0
    count = 0
    repeats = int(manifest["repeats"])
    sampling_profile = dict(manifest.get("sampling_profile") or {})
    adapter = get_adapter(str(sampling_profile.get("adapter") or "openai_compatible_responses_web_search"))
    adapter_options = dict(manifest.get("adapter_options") or {})
    for query in queries:
        provider_request = adapter.build_request(query, sampling_profile, settings, adapter_options)
        request_hashes = {provider_request.request_hash, compute_request_hash(provider_request.payload), *provider_request.legacy_request_hashes}
        for repeat_index in range(1, repeats + 1):
            if request_hashes & done_hashes.get((query.query_id, repeat_index), set()):
                count += 1
    return count
