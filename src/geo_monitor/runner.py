from __future__ import annotations

import time
import warnings
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Iterable, Literal
from uuid import uuid4

from .adapters import OpenAICompatibleClientFactory, ProviderRequest, build_sampling_profile, get_adapter
from .config import Settings, redact_secret
from .exporters import append_jsonl, successful_result_hashes
from .llm_client import LLMResponsesClient
from .mock_client import build_mock_response
from .query_meta import query_record_meta
from .request_fingerprint import legacy_payload_hash
from .schemas import ErrorRecord, MonitorResult, QueryRecord, utc_now_iso


RepeatOrder = Literal["round-robin", "grouped"]


def make_run_id() -> str:
    return f"run_{utc_now_iso().replace(':', '').replace('+0000', 'Z')}_{uuid4().hex[:8]}"


def compute_request_hash(payload: dict) -> str:
    return legacy_payload_hash(payload)


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
        sampling_profile: dict[str, Any] | None = None,
        adapter_options: dict[str, Any] | None = None,
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
        actual_sampling_profile = sampling_profile or build_sampling_profile(
            adapter_name="openai_responses_web_search",
            model=model or self.settings.llm_model,
            settings=self.settings,
            web_search_limit=web_search_limit,
        )
        actual_adapter_options = dict(adapter_options or {})
        adapter = get_adapter(str(actual_sampling_profile.get("adapter") or "openai_responses_web_search"))
        adapter.validate_options(actual_adapter_options)
        done_hashes = successful_result_hashes(output_path) if resume else {}
        work_items = []
        results: list[MonitorResult] = []

        for query, repeat_index in _iter_units(query_list, repeats, repeat_order):
            provider_request = adapter.build_request(query, actual_sampling_profile, self.settings, actual_adapter_options)
            request_hash = provider_request.request_hash
            resume_hashes = {request_hash, compute_request_hash(provider_request.payload), *provider_request.legacy_request_hashes}
            done_key = (query.query_id, repeat_index)
            existing_hashes = done_hashes.get(done_key, set())
            if resume_hashes & existing_hashes:
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
                    "provider_request": provider_request,
                    "request_hash": request_hash,
                }
            )

        if not work_items:
            return results

        if dry_run or mock:
            client = None
        elif adapter.name == "openai_responses_web_search":
            client = LLMResponsesClient(self.settings)
        else:
            client = OpenAICompatibleClientFactory(self.settings).create()
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
                    adapter=adapter,
                    repeat_index=item["repeat_index"],
                    repeat_total=repeats,
                    provider_request=item["provider_request"],
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
                        adapter=adapter,
                        repeat_index=item["repeat_index"],
                        repeat_total=repeats,
                        provider_request=item["provider_request"],
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
        client: Any | None,
        dry_run: bool,
        mock: bool,
        model: str | None,
        web_search_limit: int | None,
        adapter: Any,
        repeat_index: int,
        repeat_total: int,
        provider_request: ProviderRequest,
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
            adapter=adapter,
            repeat_index=repeat_index,
            repeat_total=repeat_total,
            provider_request=provider_request,
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
        client: Any | None,
        dry_run: bool,
        mock: bool,
        model: str | None,
        web_search_limit: int | None,
        adapter: Any,
        repeat_index: int,
        repeat_total: int,
        provider_request: ProviderRequest | None = None,
        request_hash: str | None = None,
    ) -> MonitorResult:
        started_at = utc_now_iso()
        start = time.perf_counter()
        if provider_request is None:
            sampling_profile = build_sampling_profile(
                adapter_name="openai_responses_web_search",
                model=model or self.settings.llm_model,
                settings=self.settings,
                web_search_limit=web_search_limit,
            )
            adapter = get_adapter(str(sampling_profile.get("adapter")))
            provider_request = adapter.build_request(query, sampling_profile, self.settings, {})
        payload = provider_request.payload
        request_hash = request_hash or provider_request.request_hash
        model_name = provider_request.model
        query_meta = query_record_meta(query)
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
            "request_fingerprint_version": provider_request.request_fingerprint_version,
            "request_fingerprint_basis": provider_request.request_fingerprint_basis,
            "model": model_name,
            "query": query.query,
            "input_query": query.query,
            "raw_request": payload,
            "sampling_profile": provider_request.sampling_profile,
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
                    web_search_performed=None,
                    web_search_evidence="not_available",
                    web_search_requirement_status="not_applicable",
                    source_parse_status="not_applicable",
                    latency_ms=_elapsed_ms(start),
                    completed_at=utc_now_iso(),
                )

            if mock:
                raw_response = build_mock_response(query)
                normalized = adapter.normalize_response(raw_response, provider_request)
                return MonitorResult(
                    **common,
                    status="mock",
                    response_text=normalized.text,
                    sources=normalized.sources,
                    usage=normalized.usage,
                    raw_response=normalized.raw,
                    provider_meta=normalized.provider_meta,
                    web_search_performed=normalized.web_search_performed,
                    web_search_evidence=normalized.web_search_evidence,
                    web_search_requirement_status=normalized.web_search_requirement_status,
                    source_parse_status=normalized.source_parse_status,
                    latency_ms=_elapsed_ms(start),
                    completed_at=utc_now_iso(),
                )

            if client is None:
                raise RuntimeError("真实调用需要 OpenAI-compatible client")
            response = adapter.send(client, provider_request)
            normalized = adapter.normalize_response(response, provider_request)
            _ensure_live_response_valid(normalized.raw, normalized.text)
            return MonitorResult(
                **common,
                status="success",
                response_text=normalized.text,
                sources=normalized.sources,
                usage=normalized.usage,
                raw_response=normalized.raw,
                provider_meta=normalized.provider_meta,
                web_search_performed=normalized.web_search_performed,
                web_search_evidence=normalized.web_search_evidence,
                web_search_requirement_status=normalized.web_search_requirement_status,
                source_parse_status=normalized.source_parse_status,
                latency_ms=_elapsed_ms(start),
                completed_at=utc_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            raw_response = None
            if "normalized" in locals():
                raw_response = normalized.raw
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
