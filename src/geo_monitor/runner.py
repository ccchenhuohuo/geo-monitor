from __future__ import annotations

import time
import warnings
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal
from uuid import uuid4

from .adapters import OpenAICompatibleClientFactory, ProviderRequest, build_sampling_profile, get_adapter
from .config import Settings, redact_secret
from .exporters import append_jsonl, successful_result_hashes
from .llm_client import retry_api_call
from .mock_client import build_mock_response
from .query_meta import query_record_meta
from .request_fingerprint import base_url_fingerprint, legacy_payload_hash
from .schemas import ErrorRecord, MonitorResult, QueryRecord, utc_now_iso

RepeatOrder = Literal["round-robin", "grouped"]
CIRCUIT_BREAKER_MIN_SAMPLES = 5


@dataclass
class _CircuitBreaker:
    enabled: bool
    max_consecutive_errors: int
    max_error_rate: float
    min_samples: int = CIRCUIT_BREAKER_MIN_SAMPLES
    observed: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    tripped: bool = False
    reason: str | None = None
    trigger_observed: int = 0
    trigger_errors: int = 0
    trigger_consecutive_errors: int = 0

    def observe(self, result: MonitorResult) -> bool:
        if not self.enabled or self.tripped:
            return self.tripped
        self.observed += 1
        if result.status == "error":
            self.errors += 1
            self.consecutive_errors += 1
        else:
            self.consecutive_errors = 0
        if self.consecutive_errors >= self.max_consecutive_errors:
            self._trip("consecutive_errors")
        elif self.observed >= self.min_samples and self.errors / self.observed >= self.max_error_rate:
            self._trip("error_rate")
        return self.tripped

    def _trip(self, reason: str) -> None:
        self.tripped = True
        self.reason = reason
        self.trigger_observed = self.observed
        self.trigger_errors = self.errors
        self.trigger_consecutive_errors = self.consecutive_errors

    def summary(self, *, planned: int, executed: int) -> dict[str, Any]:
        return {
            "circuit_breaker": self.tripped,
            "circuit_breaker_reason": self.reason,
            "circuit_breaker_min_samples": self.min_samples,
            "circuit_breaker_trigger_observed": self.trigger_observed,
            "circuit_breaker_trigger_errors": self.trigger_errors,
            "circuit_breaker_trigger_consecutive_errors": self.trigger_consecutive_errors,
            "circuit_breaker_trigger_error_rate": (self.trigger_errors / self.trigger_observed if self.trigger_observed else 0.0),
            "planned": planned,
            "executed": executed,
            "not_started": max(0, planned - executed),
        }


def make_run_id() -> str:
    return f"run_{utc_now_iso().replace(':', '').replace('+0000', 'Z')}_{uuid4().hex[:8]}"


def compute_request_hash(payload: dict) -> str:
    return legacy_payload_hash(payload)


class MonitorRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_run_info: dict[str, Any] = {}

    def run(
        self,
        queries: Iterable[QueryRecord],
        *,
        output_path: str | Path,
        job_id: str | None = None,
        run_id: str | None = None,
        run_execution_id: str | None = None,
        run_generation: int = 1,
        diagnostic_generation: int | None = None,
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
        if run_generation < 0:
            raise ValueError("run_generation 不能为负数")
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
        planned_units = len(query_list) * repeats
        if planned_units > self.settings.max_job_units:
            raise ValueError(f"计划执行 {planned_units} 个单元，超过 MAX_JOB_UNITS={self.settings.max_job_units}；请缩小 query/repeats 范围")
        actual_run_execution_id = run_execution_id or make_run_id()
        actual_run_id = run_id or actual_run_execution_id
        execution_mode = "dry_run" if dry_run else "mock" if mock else "live"
        actual_diagnostic_generation = (diagnostic_generation or 1) if execution_mode != "live" else None
        actual_job_id = job_id
        actual_sampling_profile = sampling_profile or build_sampling_profile(
            adapter_name="openai_responses_web_search",
            model=model or self.settings.llm_model,
            settings=self.settings,
            web_search_limit=web_search_limit,
        )
        actual_adapter_options = dict(adapter_options or {})
        _validate_sampling_profile_inputs(
            actual_sampling_profile,
            model=model,
            web_search_limit=web_search_limit,
            settings=self.settings,
            adapter_options=actual_adapter_options,
        )
        adapter = get_adapter(str(actual_sampling_profile.get("adapter") or "openai_responses_web_search"))
        adapter.validate_options(actual_adapter_options)
        done_hashes = successful_result_hashes(output_path) if resume else {}
        work_items = []
        results: list[MonitorResult] = []
        breaker = _CircuitBreaker(
            enabled=not dry_run and not mock,
            max_consecutive_errors=self.settings.max_consecutive_errors,
            max_error_rate=self.settings.max_error_rate,
        )

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
            self.last_run_info = breaker.summary(planned=0, executed=0)
            return results

        if dry_run or mock:
            client = None
        else:
            _validate_runtime_endpoint(actual_sampling_profile, self.settings)
            client = OpenAICompatibleClientFactory(self.settings).create()
        if actual_concurrency == 1 or len(work_items) <= 1:
            for item in work_items:
                result = self._run_one(
                    item["query"],
                    job_id=actual_job_id,
                    run_id=actual_run_id,
                    run_execution_id=actual_run_execution_id,
                    run_generation=run_generation,
                    diagnostic_generation=actual_diagnostic_generation,
                    execution_mode=execution_mode,
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
                if breaker.observe(result):
                    break
                if sleep_seconds:
                    time.sleep(sleep_seconds)
            self.last_run_info = breaker.summary(planned=len(work_items), executed=len(results))
            return results

        with ThreadPoolExecutor(max_workers=actual_concurrency) as executor:
            pending = set()
            next_index = 0

            def fill_available_slots() -> None:
                nonlocal next_index
                while next_index < len(work_items) and len(pending) < actual_concurrency and not breaker.tripped:
                    if next_index > 0 and start_interval_seconds:
                        time.sleep(start_interval_seconds)
                    item = work_items[next_index]
                    next_index += 1
                    pending.add(
                        executor.submit(
                            self._run_one_with_sleep,
                            item["query"],
                            job_id=actual_job_id,
                            run_id=actual_run_id,
                            run_execution_id=actual_run_execution_id,
                            run_generation=run_generation,
                            diagnostic_generation=actual_diagnostic_generation,
                            execution_mode=execution_mode,
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

            fill_available_slots()
            while pending:
                # Observe a bounded batch before submitting more work. Refilling
                # after a single completion can overshoot a tripped breaker by an
                # unbounded stream of fast failures while slower calls remain.
                done, pending = wait(pending)
                for future in done:
                    result = future.result()
                    append_jsonl(output_path, result)
                    results.append(result)
                    breaker.observe(result)
                if breaker.tripped:
                    for future in list(pending):
                        if future.cancel():
                            pending.remove(future)
                else:
                    fill_available_slots()
        self.last_run_info = breaker.summary(planned=len(work_items), executed=len(results))
        return results

    def _run_one_with_sleep(
        self,
        query: QueryRecord,
        *,
        job_id: str | None,
        run_id: str,
        run_execution_id: str,
        run_generation: int,
        diagnostic_generation: int | None,
        execution_mode: str,
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
            run_execution_id=run_execution_id,
            run_generation=run_generation,
            diagnostic_generation=diagnostic_generation,
            execution_mode=execution_mode,
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
        run_execution_id: str,
        run_generation: int,
        diagnostic_generation: int | None,
        execution_mode: str,
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
        logical_unit_id = f"{run_scope_id}__{query.query_id}__r{repeat_index}__{request_hash}"
        attempt_id = f"{logical_unit_id}__{run_execution_id}__{uuid4().hex[:8]}"

        common = {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "logical_unit_id": logical_unit_id,
            "run_id": run_id,
            "run_execution_id": run_execution_id,
            "run_generation": run_generation,
            "diagnostic_generation": diagnostic_generation,
            "execution_mode": execution_mode,
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
        api_attempt_count = 0

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

            def send_request() -> Any:
                nonlocal api_attempt_count
                api_attempt_count += 1
                return adapter.send(client, provider_request)

            response = retry_api_call(send_request, self.settings)
            normalized = adapter.normalize_response(response, provider_request)
            _ensure_live_response_valid(normalized.raw, normalized.text)
            provider_meta = dict(normalized.provider_meta)
            provider_meta.update({"api_attempt_count": api_attempt_count, "retry_count": max(0, api_attempt_count - 1)})
            return MonitorResult(
                **common,
                status="success",
                response_text=normalized.text,
                sources=normalized.sources,
                usage=normalized.usage,
                raw_response=normalized.raw,
                provider_meta=provider_meta,
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
                provider_meta={
                    "api_attempt_count": api_attempt_count,
                    "retry_count": max(0, api_attempt_count - 1),
                },
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


def _validate_sampling_profile_inputs(
    sampling_profile: dict[str, Any],
    *,
    model: str | None,
    web_search_limit: int | None,
    settings: Settings,
    adapter_options: dict[str, Any],
) -> None:
    if model is not None and str(sampling_profile.get("model") or "") != str(model):
        raise ValueError("sampling_profile.model 与运行参数 model 不一致")
    if web_search_limit is not None and int(sampling_profile.get("web_search_limit") or 0) != int(web_search_limit):
        raise ValueError("sampling_profile.web_search_limit 与运行参数 web_search_limit 不一致")
    effective = sampling_profile.get("effective_runtime")
    if isinstance(effective, dict):
        expected_max_tool_calls = effective.get("max_tool_calls")
        actual_max_tool_calls = (
            int(adapter_options.get("max_tool_calls", settings.max_tool_calls)) if str(sampling_profile.get("api_family") or "") == "responses" else None
        )
        if expected_max_tool_calls is not None and int(expected_max_tool_calls) != actual_max_tool_calls:
            raise ValueError("sampling_profile.effective_runtime.max_tool_calls 与运行时配置不一致")
        expected_max_output_tokens = effective.get("max_output_tokens")
        if expected_max_output_tokens is not None and int(expected_max_output_tokens) != settings.max_output_tokens:
            raise ValueError("sampling_profile.effective_runtime.max_output_tokens 与运行时配置不一致")
        if dict(effective.get("adapter_options") or {}) != adapter_options:
            raise ValueError("sampling_profile.effective_runtime.adapter_options 与运行参数不一致")


def _validate_runtime_endpoint(sampling_profile: dict[str, Any], settings: Settings) -> None:
    expected = str(sampling_profile.get("base_url_fingerprint") or "")
    actual = base_url_fingerprint(settings.llm_base_url)
    if expected and expected != actual:
        raise ValueError("运行时 LLM_BASE_URL 与 sampling_profile.base_url_fingerprint 不一致；请重新构建 job")


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
