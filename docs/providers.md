# Provider and adapter contract

Provider SDK transport and request semantics are separate boundaries:

- a **provider** owns credentials, endpoint validation, SDK construction,
  transport errors, retry isolation, and client lifecycle;
- an **adapter** builds a provider request and normalizes its text, usage,
  search trace, and sources into the project contract.

No provider-specific adapter is implemented by silently routing through the
generic OpenAI-compatible provider.

## Supported adapters

| Adapter | Provider SDK | Purpose | Native web search |
|---|---|---|---|
| `openai_compatible_responses_web_search` | `openai` | sampling against a Responses-compatible endpoint | endpoint-dependent |
| `openai_compatible_responses_text` | `openai` | text analysis against a Responses-compatible endpoint | no |
| `doubao_ark_responses_web_search` | `volcenginesdkarkruntime.Ark` | Doubao/Ark sampling | yes |
| `doubao_ark_responses_text` | `volcenginesdkarkruntime.Ark` | Doubao/Ark analysis | no |
| `qwen_dashscope_generation_web_search` | `dashscope.Generation` | Qwen sampling | yes |
| `qwen_dashscope_generation_text` | `dashscope.Generation` | Qwen analysis | no |
| `deepseek_chat_completions_text` | `openai` as prescribed by DeepSeek | DeepSeek analysis | no |

DeepSeek currently documents the OpenAI SDK as its Python client transport; it
does not publish a standalone official Python SDK. Its API has no native web
search/source contract, so this project deliberately does not register a
DeepSeek sampling adapter. See the [DeepSeek API quick
start](https://api-docs.deepseek.com/) and [tool-call
guide](https://api-docs.deepseek.com/guides/tool_calls).

Provider SDKs stay optional because the Volcengine distribution is large:

```bash
pip install -e '.[doubao]'
pip install -e '.[qwen]'
# or both
pip install -e '.[providers-all]'
```

The generic and DeepSeek providers use the core `openai` dependency.

## Credentials and endpoints

`LLM_API_KEY` and `LLM_BASE_URL` configure the generic provider. `LLM_API_KEY`
also remains a key fallback for native providers, while each native provider
uses its stable official endpoint unless its own base URL is overridden.
Dedicated variables allow sampling and analysis to use different providers in
one job:

| Provider | Key | Optional endpoint override | Official default |
|---|---|---|---|
| Doubao | `ARK_API_KEY` | `ARK_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` |
| Qwen | `DASHSCOPE_API_KEY` | `DASHSCOPE_BASE_URL` | `https://dashscope.aliyuncs.com/api/v1` |
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |

Never put keys in job JSON or commit them. A Qwen native adapter rejects
`/compatible-mode/v1`; a Doubao native adapter requires `/api/v3`. A DeepSeek
provider accepts only the official host. Native providers always require HTTPS,
and endpoint overrides remain limited to their provider's official domains;
use `openai_compatible_*` for a proxy or custom host.

Example: native Ark sampling and native Ark analysis:

```bash
export ARK_API_KEY='<ARK_API_KEY>'
```

```json
{
  "model": "<ARK_MODEL_OR_ENDPOINT_ID>",
  "adapter": "doubao_ark_responses_web_search",
  "adapter_options": {
    "sources": ["search_engine"],
    "max_tool_calls": 2
  },
  "analysis_model": "<ARK_MODEL_OR_ENDPOINT_ID>",
  "analysis_adapter": "doubao_ark_responses_text"
}
```

Example: Qwen sampling with DeepSeek analysis:

```bash
export DASHSCOPE_API_KEY='<DASHSCOPE_API_KEY>'
export DEEPSEEK_API_KEY='<DEEPSEEK_API_KEY>'
```

```json
{
  "model": "qwen-plus",
  "adapter": "qwen_dashscope_generation_web_search",
  "analysis_model": "deepseek-v4-flash",
  "analysis_adapter": "deepseek_chat_completions_text"
}
```

DeepSeek V4 analysis explicitly disables thinking and requests a JSON object to
avoid unnecessary reasoning cost and accidental chain-of-thought persistence.
The retired `deepseek-chat` and `deepseek-reasoner` names are rejected.

## Native search options

Ark sends the SDK-native tool shape. Its `sources` value defaults to
`["search_engine"]` and may contain only `toutiao`, `douyin`, `moji`, or
`search_engine`. `limit`, `user_location`, `max_keyword`, `tool_choice`, and
`max_tool_calls` are the only supported request controls.

Qwen sends `enable_search=true` and defaults to:

```json
{
  "forced_search": true,
  "enable_source": true,
  "enable_citation": true,
  "citation_format": "[ref_<number>]"
}
```

Supported DashScope values belong inside `adapter_options.search_options` and
are validated before a request is sent: `forced_search`, `enable_source`,
`enable_citation`, `citation_format`, `search_strategy`, `freshness`,
`assigned_site_list`, `intention_options`, and `enable_search_extension`.
Unknown fields, invalid enums, and invalid cross-field combinations fail closed.
Required search rejects `forced_search=false` and `enable_source=false`.

The native Qwen text adapter accepts the stable text `Generation` families
`qwen-plus`, `qwen-max`, `qwen-flash`, and `qwen-turbo`, including dated
variants. The search adapter is stricter: it accepts only the stable/latest
Plus, Max, and Flash aliases plus exact `qwen-turbo`, avoiding old snapshots
whose web-search support is not guaranteed. Qwen 3.5/3.6/3.7, `qwen3-max`,
multimodal, and coder families have current interface- or mode-specific
contracts; they are rejected instead of being routed through the wrong SDK
method. Those families should receive separate native adapters if needed.

`WEB_SEARCH_LIMIT` is sent only by the Ark adapter, whose profile records
`web_search_limit_effective=true`. Other profiles keep the validated value for
cross-provider metadata but record `false`; do not compare it as an effective
condition for those providers.

## Runtime safety and retry boundary

All SDK retries are disabled. The application owns one bounded retry policy for
transport failures and HTTP 408, 409, 429, 500, 502, 503, and 504 responses.
Provider SDK exceptions are translated at the provider boundary.

Ark documents its synchronous client as thread-unsafe. Concurrent runs use a
thread-local Ark client and close every created client after the worker pool
finishes. Other providers may share their immutable/thread-safe runtime client.

Unknown adapter options fail closed. Effective payloads, SDK identity, endpoint
fingerprints, model, adapter version, and output/tool limits are frozen into
the request and comparability profiles. A changed condition cannot silently
resume an incompatible success.

## Migration

Version 0.3 intentionally removes the old runtime names
`openai_responses_*`, `doubao_responses_web_search`, `qwen_chat_enable_search`,
and `qwen_responses_web_search_basic`. They are not aliases because doing so
would make an old experiment name silently select a different SDK transport.
Create a new job with an explicit 0.3 adapter instead.

Live sampling and live analysis require `--confirm-cost`. Use
`geo-monitor doctor` to inspect the redacted effective configuration before a
small live smoke test.
