# Provider And Runtime Contract

GEO Brand Monitor uses the OpenAI Python SDK as a transport for
OpenAI-compatible APIs. The selected adapter, rather than the SDK package,
defines the provider request and response contract.

## OpenAI-Compatible And Doubao Ark Modes

Use `openai_responses_web_search` for a generic Responses API implementation.
Use `doubao_responses_web_search` when the endpoint is Doubao/Volcengine Ark and
the provider-specific web-search request/trace behavior should be preserved.
Both modes use the same `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`
environment variables; never put a key in a job config or commit it.

Ark exposes an OpenAI-compatible endpoint at:

```bash
export LLM_API_KEY='<ARK_API_KEY>'
export LLM_BASE_URL='https://ark.cn-beijing.volces.com/api/v3'
export LLM_MODEL='<ARK_MODEL_OR_ENDPOINT_ID>'
```

The placeholders above are intentional. Model and endpoint IDs can change, so
freeze the exact value used by a study in its job manifest.

Generic OpenAI-compatible sampling:

```json
{
  "model": "<ARK_MODEL_OR_ENDPOINT_ID>",
  "adapter": "openai_responses_web_search",
  "analysis_model": "<ARK_MODEL_OR_ENDPOINT_ID>",
  "analysis_adapter": "openai_responses_text",
  "adapter_options": {
    "tool_choice": "required"
  }
}
```

Doubao-specific sampling:

```json
{
  "model": "<ARK_MODEL_OR_ENDPOINT_ID>",
  "adapter": "doubao_responses_web_search",
  "analysis_model": "<ARK_MODEL_OR_ENDPOINT_ID>",
  "analysis_adapter": "openai_responses_text",
  "adapter_options": {
    "web_search_options": {},
    "tool_choice": "required",
    "max_tool_calls": 2
  }
}
```

`include` is deliberately omitted in the Ark examples: Ark rejects the
OpenAI-specific `web_search_call.action.sources` include value. It remains an
explicit adapter option for endpoints that document support for it.

The analysis adapter is text-only and still uses the configured compatible
endpoint. A sampling success does not prove source parsing succeeded: inspect
`web_search_requirement_status`, `web_search_evidence`, and
`source_parse_status` in facts and quality outputs.

## Effective Settings

| Setting | Effective behavior |
|---|---|
| `MAX_TOOL_CALLS` | Sent to Responses adapters and frozen into the sampling profile. The Doubao adapter can override it with `adapter_options.max_tool_calls`. |
| `MAX_OUTPUT_TOKENS` | Sent for sampling responses and frozen into the request/comparability contract. |
| `ANALYSIS_MAX_OUTPUT_TOKENS` | Sent to text-only extraction/canonicalization calls. |
| `REQUEST_TIMEOUT_SECONDS` | SDK request timeout. |
| `RETRY_MAX_ATTEMPTS` | Total application attempts for retryable transport, timeout, rate-limit, and transient 5xx errors. SDK retries are disabled. |
| `MAX_JOB_UNITS` | Refuses a query × repeat plan above this bound before execution. |
| `MAX_CONSECUTIVE_ERRORS` | Trips the live circuit breaker after this many consecutive failed units. |
| `MAX_ERROR_RATE` | Trips after at least five observed live units when the cumulative error rate reaches the threshold. |
| `CONCURRENCY` | Worker count, bounded to 1–8. In-flight calls may finish after a breaker trips. |

`WEB_SEARCH_LIMIT` is retained for config and historical manifest compatibility,
and is validated as an integer from 1 to 20. No current adapter exposes a
provider-independent hard result-count parameter, so sampling profiles record
`web_search_limit_effective=false` and the value is not sent as a search result
limit. Do not interpret it as the number of sources returned, and do not use it
as an effective comparison condition. `MAX_TOOL_CALLS` is the effective bound.

Adapter options are fail-closed: unknown keys are rejected. Provider request
payloads, effective options, endpoint fingerprints, and output limits are
included in request/comparability provenance so a changed request is rerun
instead of silently reused.

## Retry, Circuit Breaker, And Cost Boundary

The OpenAI SDK is constructed with `max_retries=0`; one application retry
policy covers every adapter. Network/timeout failures and HTTP 408, 409, 429,
500, 502, 503, and 504 are retried with bounded exponential backoff. Validation,
authentication, and other permanent errors fail immediately.

The circuit breaker is active only for live sampling. Dry runs and mocks neither
spend provider budget nor influence live breaker state. A tripped run is marked
partial and retains every completed/error attempt for audit. Live commands also
require explicit `--confirm-cost` (or `confirm_cost=True` in Python).

## Resume And Audit Identity

The following identities have different purposes:

| Field | Meaning |
|---|---|
| `job_id` / `run_id` | Stable bundle identity for the study run. |
| `run_execution_id` | Unique identity for one invocation of `run-job`, including a no-op resume. |
| `run_generation` | Live data generation. It advances only when live units execute and invalidates stale analysis artifacts. |
| `diagnostic_generation` | Separate dry-run/mock generation; diagnostics do not invalidate an analyzed live generation. |
| `logical_unit_id` | Stable job + query + repeat + request-fingerprint identity. |
| `attempt_id` | Unique physical attempt identity containing the execution identity plus random suffix. |

Resume selects the latest terminal live record for each `(query_id,
repeat_index)` using parsed timestamps with file order as a fallback. Only a
latest `success` with a matching request fingerprint is skipped. A newer
`error` or `interrupted` record supersedes an older success, so the unit is
retried. Changes to the endpoint, model, adapter, payload, effective options, or
token/tool limits cannot silently reuse an incompatible success.

`Ctrl-C` moves an executing live job to `interrupted`; already written attempts
remain resumable. Raw requests/responses and analysis-call audit events may
contain sensitive text or URLs. Keep the study workspace private and never
publish its raw bundle without review.

## Endpoint Safety

Live mode requires an HTTPS endpoint by default, rejects URL credentials and
fragments, and redacts sensitive query parameters in diagnostics. Plain HTTP is
available only through the explicit `ALLOW_INSECURE_HTTP=true` development
override. Use `geo-monitor doctor` before a live run to review the redacted
effective configuration.
