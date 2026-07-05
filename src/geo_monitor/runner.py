from __future__ import annotations

import hashlib
import json
import time
import warnings
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Iterable, Literal
from uuid import uuid4

from .llm_client import LLMResponsesClient, build_responses_payload
from .config import Settings, redact_secret
from .exporters import append_jsonl, successful_result_hashes
from .mock_client import build_mock_response
from .response_parser import parse_response, response_to_dict
from .schemas import ErrorRecord, MonitorResult, QueryRecord, utc_now_iso


RepeatOrder = Literal["round-robin", "grouped"]


def make_run_id() -> str:
    return f"run_{utc_now_iso().replace(':', '').replace('+0000', 'Z')}_{uuid4().hex[:8]}"


def compute_request_hash(payload: dict) -> str:
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


class MonitorRunner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(
        self,
        queries: Iterable[QueryRecord],
        *,
        output_path: str | Path,
        job_id: str | None = None,
        run_id: str | None = None,
        dry_run: bool = False,
        mock: bool = False,
        resume: bool = False,
        model: str | None = None,
        web_search_limit: int | None = None,
        repeats: int = 1,
        repeat_order: RepeatOrder = "round-robin",
        sleep_seconds: float = 0.0,
        start_interval_seconds: float = 0.0,
        concurrency: int | None = None,
    ) -> list[MonitorResult]:
        if dry_run and mock:
            raise ValueError("dry-run 和 mock 不能同时启用")
        if repeats < 1:
            raise ValueError("repeats 必须大于等于 1")
        if repeat_order not in {"round-robin", "grouped"}:
            raise ValueError("repeat_order 只能是 round-robin 或 grouped")
        if sleep_seconds < 0:
            raise ValueError("sleep_seconds 不能为负数")
        if start_interval_seconds < 0:
            raise ValueError("start_interval_seconds 不能为负数")
        actual_concurrency = concurrency if concurrency is not None else self.settings.concurrency
        if actual_concurrency < 1 or actual_concurrency > 8:
            raise ValueError("concurrency 必须在 1 到 8 之间")

        query_list = list(queries)
        actual_run_id = run_id or make_run_id()
        actual_job_id = job_id
        done_hashes = successful_result_hashes(output_path) if resume else {}
        work_items = []
        results: list[MonitorResult] = []

        for query, repeat_index in _iter_units(query_list, repeats, repeat_order):
            payload = build_responses_payload(query, self.settings, model=model, web_search_limit=web_search_limit)
            request_hash = compute_request_hash(payload)
            done_key = (query.query_id, repeat_index)
            existing_hashes = done_hashes.get(done_key, set())
            if request_hash in existing_hashes:
                continue
            if existing_hashes:
                warnings.warn(
                    f"resume request_hash changed for {query.query_id}#{repeat_index}; rerunning",
                    RuntimeWarning,
                    stacklevel=2,
                )
            work_items.append(
                {
                    "query": query,
                    "repeat_index": repeat_index,
                    "payload": payload,
                    "request_hash": request_hash,
                }
            )

        if not work_items:
            return results

        client = None if dry_run or mock else LLMResponsesClient(self.settings)
        if actual_concurrency == 1 or len(work_items) <= 1:
            for item in work_items:
                result = self._run_one(
                    item["query"],
                    job_id=actual_job_id,
                    run_id=actual_run_id,
                    client=client,
                    dry_run=dry_run,
                    mock=mock,
                    model=model,
                    web_search_limit=web_search_limit,
                    repeat_index=item["repeat_index"],
                    repeat_total=repeats,
                    payload=item["payload"],
                    request_hash=item["request_hash"],
                )
                append_jsonl(output_path, result)
                results.append(result)
                if sleep_seconds:
                    time.sleep(sleep_seconds)
            return results

        with ThreadPoolExecutor(max_workers=actual_concurrency) as executor:
            pending = set()

            def drain_completed(*, wait_for_one: bool = False) -> None:
                nonlocal pending
                if not pending:
                    return
                timeout = None if wait_for_one else 0
                done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    append_jsonl(output_path, result)
                    results.append(result)

            for index, item in enumerate(work_items):
                if index > 0 and start_interval_seconds:
                    drain_completed()
                    time.sleep(start_interval_seconds)
                    drain_completed()
                pending.add(
                    executor.submit(
                        self._run_one_with_sleep,
                        item["query"],
                        job_id=actual_job_id,
                        run_id=actual_run_id,
                        client=client,
                        dry_run=dry_run,
                        mock=mock,
                        model=model,
                        web_search_limit=web_search_limit,
                        repeat_index=item["repeat_index"],
                        repeat_total=repeats,
                        payload=item["payload"],
                        request_hash=item["request_hash"],
                        sleep_seconds=sleep_seconds,
                    )
                )
            while pending:
                drain_completed(wait_for_one=True)
        return results

    def _run_one_with_sleep(
        self,
        query: QueryRecord,
        *,
        job_id: str | None,
        run_id: str,
        client: LLMResponsesClient | None,
        dry_run: bool,
        mock: bool,
        model: str | None,
        web_search_limit: int | None,
        repeat_index: int,
        repeat_total: int,
        payload: dict,
        request_hash: str,
        sleep_seconds: float,
    ) -> MonitorResult:
        result = self._run_one(
            query,
            job_id=job_id,
            run_id=run_id,
            client=client,
            dry_run=dry_run,
            mock=mock,
            model=model,
            web_search_limit=web_search_limit,
            repeat_index=repeat_index,
            repeat_total=repeat_total,
            payload=payload,
            request_hash=request_hash,
        )
        if sleep_seconds:
            time.sleep(sleep_seconds)
        return result

    def _run_one(
        self,
        query: QueryRecord,
        *,
        job_id: str | None,
        run_id: str,
        client: LLMResponsesClient | None,
        dry_run: bool,
        mock: bool,
        model: str | None,
        web_search_limit: int | None,
        repeat_index: int,
        repeat_total: int,
        payload: dict | None = None,
        request_hash: str | None = None,
    ) -> MonitorResult:
        started_at = utc_now_iso()
        start = time.perf_counter()
        payload = payload or build_responses_payload(query, self.settings, model=model, web_search_limit=web_search_limit)
        request_hash = request_hash or compute_request_hash(payload)
        model_name = payload["model"]
        query_meta = _query_meta(query)
        run_scope_id = job_id or run_id
        attempt_id = f"{run_scope_id}__{query.query_id}__r{repeat_index}__{request_hash}"

        common = {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "query_id": query.query_id,
            "repeat_index": repeat_index,
            "repeat_total": repeat_total,
            "request_hash": request_hash,
            "model": model_name,
            "query": query.query,
            "input_query": query.query,
            "raw_request": payload,
            "metadata": query.metadata_with_tags(),
            "query_meta": query_meta,
            "started_at": started_at,
        }

        try:
            if dry_run:
                return MonitorResult(
                    **common,
                    status="dry_run",
                    raw_response=None,
                    latency_ms=_elapsed_ms(start),
                    completed_at=utc_now_iso(),
                )

            if mock:
                raw_response = build_mock_response(query)
                text, sources, usage, raw = parse_response(raw_response)
                return MonitorResult(
                    **common,
                    status="mock",
                    response_text=text,
                    sources=sources,
                    usage=usage,
                    raw_response=raw,
                    latency_ms=_elapsed_ms(start),
                    completed_at=utc_now_iso(),
                )

            if client is None:
                raise RuntimeError("真实调用需要 LLMResponsesClient")
            response = client.create_response(payload)
            text, sources, usage, raw = parse_response(response)
            _ensure_live_response_valid(raw, text)
            return MonitorResult(
                **common,
                status="success",
                response_text=text,
                sources=sources,
                usage=usage,
                raw_response=raw,
                latency_ms=_elapsed_ms(start),
                completed_at=utc_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            raw_response = None
            if 'raw' in locals():
                raw_response = raw
            return MonitorResult(
                **common,
                status="error",
                raw_response=raw_response,
                latency_ms=_elapsed_ms(start),
                error=ErrorRecord(type=exc.__class__.__name__, message=redact_secret(str(exc), self.settings) or ""),
                completed_at=utc_now_iso(),
            )


def _ensure_live_response_valid(raw: dict, text: str | None) -> None:
    status = str(raw.get("status") or "").lower()
    if status and status not in {"completed"}:
        raise RuntimeError(f"ResponseNotCompleted: status={status}")
    if raw.get("error"):
        raise RuntimeError(f"ResponseError: {raw.get('error')}")
    if raw.get("incomplete_details"):
        raise RuntimeError(f"IncompleteResponse: {raw.get('incomplete_details')}")
    if not (text and text.strip()):
        raise RuntimeError("EmptyResponseText")


def _iter_units(
    queries: list[QueryRecord],
    repeats: int,
    repeat_order: RepeatOrder,
) -> Iterable[tuple[QueryRecord, int]]:
    if repeat_order == "round-robin":
        for repeat_index in range(1, repeats + 1):
            for query in queries:
                yield query, repeat_index
    else:
        for query in queries:
            for repeat_index in range(1, repeats + 1):
                yield query, repeat_index


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _query_meta(query: QueryRecord) -> dict[str, str]:
    metadata = query.metadata_with_tags()

    def text(key: str, default: str = "") -> str:
        value = metadata.get(key, default)
        if value in (None, ""):
            return default
        return str(value)

    return {
        "schema_version": "query-meta-v1",
        "variant_id": text("variant_id"),
        "seed_id": text("seed_id"),
        "seed_query": text("seed_query"),
        "category": str(query.category or metadata.get("category") or ""),
        "intent": text("intent"),
        "persona": text("persona"),
        "template_id": text("template_id"),
        "language": text("language", str(query.locale or "")),
        "generation_method": text("generation_method", "config"),
        "fanout_version": text("fanout_version"),
        "manifest_version": text("manifest_version"),
    }
