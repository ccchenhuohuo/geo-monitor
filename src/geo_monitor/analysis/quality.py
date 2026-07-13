"""Evaluate raw-attempt contracts and decide which records are safe for statistics."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ..exporters import canonical_request_hash, latest_terminal_records, safe_result_key
from ..request_fingerprint import REQUEST_FINGERPRINT_VERSION, legacy_payload_hash, request_fingerprint


def evaluate_data_quality(
    raw_records: list[dict[str, Any]],
    analysis_records: list[dict[str, Any]],
    raw_read_errors: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    analysis_statuses: set[str],
) -> dict[str, Any]:
    expected_units = {(str(query["query_id"]), repeat) for query in manifest["queries"] for repeat in range(1, int(manifest["repeats"]) + 1)}
    missing_unknown_units_count = 0
    if manifest.get("_query_universe_incomplete"):
        missing_unknown_units_count = int(manifest.get("_missing_unknown_query_count") or 0) * int(manifest["repeats"])
    latest_terminal = latest_terminal_records(raw_records)
    latest_failed_units = [
        {"query_id": key[0], "repeat_index": key[1], "status": str(record.get("status") or ""), "error": _record_error_message(record)}
        for record in latest_terminal
        if (key := safe_result_key(record)) is not None and record.get("status") == "error"
    ]
    actual_units = {key for record in analysis_records if (key := safe_result_key(record)) is not None}
    manifest_ids = {str(query["query_id"]) for query in manifest["queries"]}
    manifest_queries = {str(query["query_id"]): str(query["query"]) for query in manifest["queries"]}
    raw_units: list[tuple[str, int, str]] = []
    raw_execution_units: list[tuple[str, int, str, str]] = []
    invalid_records: list[dict[str, Any]] = []
    analysis_signatures = {_record_attempt_signature(record) for record in analysis_records if safe_result_key(record) is not None}
    raw_records_by_signature: dict[tuple[Any, ...], tuple[int, dict[str, Any]]] = {}
    contract_mismatches: list[dict[str, Any]] = []
    superseded_contract_mismatches: list[dict[str, Any]] = []
    for index, record in enumerate(raw_records, start=1):
        key = safe_result_key(record)
        status = str(record.get("status") or "")
        if key is None:
            invalid_records.append({"record_index": index, "reason": "invalid query_id/repeat_index", "status": status})
            continue
        qid, repeat = key
        raw_units.append((qid, repeat, status))
        raw_execution_units.append(
            (
                qid,
                repeat,
                status,
                str(record.get("run_execution_id") or record.get("run_id") or "__legacy"),
            )
        )
        signature = _record_attempt_signature(record)
        raw_records_by_signature[signature] = (index, record)
        if status in analysis_statuses and signature not in analysis_signatures:
            superseded_contract_mismatches.extend(_record_contract_mismatches(record, manifest, manifest_queries, qid, repeat, index))
    for record in analysis_records:
        key = safe_result_key(record)
        if key is None:
            continue
        qid, repeat = key
        signature = _record_attempt_signature(record)
        record_index, contract_record = raw_records_by_signature.get(signature, (0, record))
        contract_mismatches.extend(_record_contract_mismatches(contract_record, manifest, manifest_queries, qid, repeat, record_index))
    duplicate_counts = Counter((qid, repeat, execution_id) for qid, repeat, status, execution_id in raw_execution_units if status in analysis_statuses)
    duplicate_units = [
        {"query_id": qid, "repeat_index": repeat, "run_execution_id": execution_id, "count": count}
        for (qid, repeat, execution_id), count in duplicate_counts.items()
        if count > 1
    ]
    execution_counts: dict[tuple[str, int], set[str]] = defaultdict(set)
    for qid, repeat, status, execution_id in raw_execution_units:
        if status in analysis_statuses:
            execution_counts[(qid, repeat)].add(execution_id)
    historical_reexecution_units = [
        {"query_id": qid, "repeat_index": repeat, "execution_count": len(executions)}
        for (qid, repeat), executions in sorted(execution_counts.items())
        if len(executions) > 1
    ]
    missing_units = [{"query_id": qid, "repeat_index": repeat} for qid, repeat in sorted(expected_units - actual_units)]
    extra_units = [{"query_id": qid, "repeat_index": repeat} for qid, repeat in sorted(actual_units - expected_units)]
    extra_query_ids = sorted({qid for qid, _, _ in raw_units if qid not in manifest_ids and qid != "None"})
    profile_quality_flags = _profile_quality_flags(manifest)
    web_search_quality_flags = _web_search_quality_flags(analysis_records, manifest)
    source_quality_flags = _source_quality_flags(analysis_records, manifest)
    partial = bool(
        latest_failed_units
        or missing_units
        or missing_unknown_units_count
        or extra_units
        or duplicate_units
        or raw_read_errors
        or invalid_records
        or contract_mismatches
        or profile_quality_flags
        or web_search_quality_flags
        or source_quality_flags
    )
    conclusion_strength = "observational" if partial else "strong"
    result = {
        "planned_units": len(expected_units) + missing_unknown_units_count,
        "analysis_record_count": len(analysis_records),
        "analysis_statuses": sorted(analysis_statuses),
        "partial_sample": partial,
        "conclusion_strength": conclusion_strength,
        "missing_units": missing_units,
        "extra_units": extra_units,
        "extra_query_ids": extra_query_ids,
        "latest_failed_units": latest_failed_units,
        "duplicate_units": duplicate_units,
        "historical_reexecution_units": historical_reexecution_units,
        "invalid_records": invalid_records,
        "contract_mismatches": contract_mismatches,
        "superseded_contract_mismatches": superseded_contract_mismatches,
        "profile_quality_flags": profile_quality_flags,
        "web_search_quality_flags": web_search_quality_flags,
        "source_quality_flags": source_quality_flags,
        "raw_read_errors": raw_read_errors,
    }
    if missing_unknown_units_count:
        result["query_manifest_unavailable"] = True
        result["observed_query_count"] = int(manifest.get("_observed_query_count") or len(manifest["queries"]))
        result["expected_query_count"] = int(manifest.get("query_count") or len(manifest["queries"]))
        result["missing_unknown_units_count"] = missing_unknown_units_count
    return result


def _record_attempt_signature(record: dict[str, Any]) -> tuple[Any, ...]:
    key = safe_result_key(record)
    return (
        key,
        str(record.get("completed_at") or ""),
        str(record.get("attempt_id") or ""),
        str(canonical_request_hash(record) or ""),
        str(record.get("response_text") or ""),
    )


def _record_error_message(record: dict[str, Any]) -> str:
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    return str(error.get("message") or error.get("type") or "")


def _profile_quality_flags(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    flags = []
    for name in ["sampling_profile", "analysis_profile", "comparability_profile"]:
        profile = manifest.get(name) if isinstance(manifest.get(name), dict) else {}
        if profile.get("inferred_from_legacy"):
            flags.append({"type": "inferred_from_legacy", "profile": name, "status": "observational"})
    return flags


def _web_search_quality_flags(records: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    manifest_profile = manifest.get("sampling_profile") if isinstance(manifest.get("sampling_profile"), dict) else {}
    for record in records:
        sampling_profile = record.get("sampling_profile") if isinstance(record.get("sampling_profile"), dict) else manifest_profile
        required = bool(sampling_profile.get("web_search_required", manifest_profile.get("web_search_required", True)))
        status = record.get("web_search_requirement_status")
        if status in (None, ""):
            if required:
                flags.append(
                    {
                        "query_id": record.get("query_id", ""),
                        "repeat_index": record.get("repeat_index", 1),
                        "status": "not_verifiable",
                        "evidence": record.get("web_search_evidence", ""),
                        "reason": "missing_web_search_requirement_status",
                    }
                )
            continue
        if status in {"satisfied", "not_applicable"}:
            if required and status == "satisfied" and not record.get("web_search_evidence"):
                flags.append(
                    {
                        "query_id": record.get("query_id", ""),
                        "repeat_index": record.get("repeat_index", 1),
                        "status": "not_verifiable",
                        "evidence": "",
                        "reason": "missing_web_search_evidence",
                    }
                )
            continue
        flags.append(
            {
                "query_id": record.get("query_id", ""),
                "repeat_index": record.get("repeat_index", 1),
                "status": status,
                "evidence": record.get("web_search_evidence", ""),
            }
        )
    return flags


def _source_quality_flags(records: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    manifest_profile = manifest.get("sampling_profile") if isinstance(manifest.get("sampling_profile"), dict) else {}
    for record in records:
        sampling_profile = record.get("sampling_profile") if isinstance(record.get("sampling_profile"), dict) else manifest_profile
        source_grain = str(sampling_profile.get("source_grain") or manifest_profile.get("source_grain") or "unknown")
        status = record.get("source_parse_status")
        if status in (None, ""):
            if source_grain == "url":
                flags.append(
                    {
                        "query_id": record.get("query_id", ""),
                        "repeat_index": record.get("repeat_index", 1),
                        "status": "missing",
                        "reason": "missing_source_parse_status",
                    }
                )
            continue
        if status in {"parsed", "provider_returned_empty", "unsupported_by_protocol", "not_applicable"}:
            continue
        flags.append(
            {
                "query_id": record.get("query_id", ""),
                "repeat_index": record.get("repeat_index", 1),
                "status": status,
            }
        )
    return flags


STAT_EXCLUDING_CONTRACT_FIELDS = {
    "input_query",
    "model",
    "repeat_total",
    "raw_request.model",
    "raw_request.input",
    "request_hash",
    "raw_response",
}


def records_for_stats(records: list[dict[str, Any]], data_quality: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    expected_units = {(str(query["query_id"]), repeat) for query in manifest["queries"] for repeat in range(1, int(manifest["repeats"]) + 1)}
    excluded_units = {(str(item["query_id"]), int(item["repeat_index"])) for item in data_quality.get("extra_units", [])}
    for mismatch in data_quality.get("contract_mismatches", []):
        if mismatch.get("field") in STAT_EXCLUDING_CONTRACT_FIELDS:
            excluded_units.add((str(mismatch.get("query_id")), int(mismatch.get("repeat_index") or 1)))
    valid: list[dict[str, Any]] = []
    for record in records:
        key = safe_result_key(record)
        if key is None or key not in expected_units or key in excluded_units:
            continue
        valid.append(record)
    data_quality["stats_record_count"] = len(valid)
    data_quality["excluded_from_stats_count"] = max(0, len(records) - len(valid))
    if len(valid) != len(records):
        data_quality["partial_sample"] = True
        data_quality["conclusion_strength"] = "observational"
    return valid


def _record_contract_mismatches(
    record: dict[str, Any],
    manifest: dict[str, Any],
    manifest_queries: dict[str, str],
    qid: str,
    repeat: int,
    record_index: int,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    expected_query = manifest_queries.get(qid)
    raw_request = record.get("raw_request") if isinstance(record.get("raw_request"), dict) else {}
    raw_response = record.get("raw_response")
    if not isinstance(raw_response, dict) or not raw_response:
        mismatches.append(_contract_mismatch(record_index, qid, repeat, "raw_response", type(raw_response).__name__, "non-empty object"))
    checks = [
        ("input_query", record.get("input_query"), expected_query),
        ("model", record.get("model"), manifest.get("model")),
        ("repeat_total", record.get("repeat_total"), manifest.get("repeats")),
        ("raw_request.model", raw_request.get("model"), manifest.get("model")),
        ("raw_request.input", _raw_request_input(raw_request), expected_query),
    ]
    if raw_request:
        stored_hash = str(record.get("request_hash") or "")
        computed_hash = _record_request_hash(record, raw_request)
        if computed_hash and not stored_hash:
            mismatches.append(_contract_mismatch(record_index, qid, repeat, "request_hash", "", computed_hash))
        elif stored_hash and computed_hash and stored_hash != computed_hash:
            mismatches.append(_contract_mismatch(record_index, qid, repeat, "request_hash", stored_hash, computed_hash))
    for field, actual, expected in checks:
        if expected is None:
            continue
        if str(actual) != str(expected):
            mismatches.append(_contract_mismatch(record_index, qid, repeat, field, actual, expected))
    return mismatches


def _record_request_hash(record: dict[str, Any], raw_request: dict[str, Any]) -> str | None:
    if isinstance(record.get("request_fingerprint_basis"), dict) and record.get("request_fingerprint_version") == REQUEST_FINGERPRINT_VERSION:
        return request_fingerprint(record["request_fingerprint_basis"])
    if not raw_request:
        return None
    return legacy_payload_hash(raw_request)


def _raw_request_input(raw_request: dict[str, Any]) -> Any:
    if "input" in raw_request:
        return raw_request.get("input")
    messages = raw_request.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user":
                return message.get("content")
    return None


def _contract_mismatch(record_index: int, qid: str, repeat: int, field: str, actual: Any, expected: Any) -> dict[str, Any]:
    return {
        "record_index": record_index,
        "query_id": qid,
        "repeat_index": repeat,
        "field": field,
        "actual": str(actual),
        "expected": str(expected),
    }


def apply_extraction_quality(data_quality: dict[str, Any], errors: list[dict[str, Any]], analysis_record_count: int) -> None:
    error_row_count = len(errors)
    has_global_error = any(str(error.get("scope") or "").lower() == "global" for error in errors)
    error_record_count = analysis_record_count if has_global_error else len(_error_record_keys(errors))
    error_record_count = min(error_record_count, analysis_record_count)
    data_quality["extraction_error_record_count"] = error_record_count
    data_quality["extraction_error_row_count"] = error_row_count
    data_quality["extraction_error_rate"] = f"{error_record_count / (analysis_record_count or 1):.1%}"
    quarantine_count = sum(1 for error in errors if str(error.get("type") or "") == "TraceabilityQuarantine")
    if quarantine_count:
        data_quality["traceability_quarantine_count"] = quarantine_count
    if error_row_count:
        data_quality["partial_sample"] = True
        data_quality["conclusion_strength"] = "observational"


def _error_record_keys(errors: list[dict[str, Any]]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for index, error in enumerate(errors):
        query_id = str(error.get("query_id") or f"__unknown_{index}")
        try:
            repeat_index = int(error.get("repeat_index") or 1)
        except Exception:
            repeat_index = 1
        keys.add((query_id, repeat_index))
    return keys
