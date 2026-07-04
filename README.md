# GEO Brand Monitor MVP

这是一个最小可用的 GEO 品牌监测原型：读取任务级 query 配置，通过 OpenAI-compatible Responses API 调用支持 Web Search tool 的模型，收集模型联网回答和引用来源。

默认模型占位：`provider-model`。真实运行前请在 `.env` 或 job 配置中替换为供应商实际模型或 endpoint id。

## 功能范围

已包含：

- job 配置输入；
- query 基础校验；
- dry-run：只生成请求，不调用 API；
- mock-run：使用模拟回答验证采集链路，并可配合 `analyze-job --include-mock` 验收 demo 报告；
- live-run：调用 OpenAI-compatible Responses API；
- Web Search tool 请求参数；
- JSONL 审计输出；
- CSV 汇总导出；
- 固定 query 重复采样、断点续跑和可调并发；
- 品牌提及率、品牌命中事件份额、推荐/排名/情感抽取指标、来源域名共现和稳定性报告；
- 任务驱动的 `build-job / run-job / analyze-job` 工作流；
- 对 response_text 做 LLM 开放式品牌/机构抽取，不再要求预置竞品 alias；
- source/citation 域名与 URL 分析；
- job 配置校验、live 成本预检、子集 smoke 运行；
- 单元测试。

暂不包含：

- App 自动化采集或 App 真实排名校准；
- 真实市场份额、App 端真实 SOV、事实核验；
- 自动 query 生成；
- 数据库和看板；
- 定时任务；
- 自动化前端采集。

## 安装

```bash
cd geo-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

建议把项目放在稳定的本地目录，不要放在会导致目录枚举或文件读取阻塞的同步盘、FileProvider 异常目录或未完全本地化目录中。

## 配置

复制环境变量样例：

```bash
cp .env.example .env
```

在 `.env` 中填写 LLM provider API key、base URL 和模型：

```bash
LLM_API_KEY=
LLM_BASE_URL=https://api.example.com/v1
LLM_MODEL=provider-model
WEB_SEARCH_LIMIT=5
MAX_TOOL_CALLS=2
REQUEST_TIMEOUT_SECONDS=90
RETRY_MAX_ATTEMPTS=3
CONCURRENCY=1
```

安全要求：

- 不要把 `.env` 提交到版本库；
- 不要把真实 API key 写进代码、README 或测试文件；
- 如果 key 曾经暴露在聊天、文档或截图中，建议去对应供应商控制台轮换。

## 输入数据格式

新版推荐使用 job 配置作为入口。任务配置会保存品牌、行业和可选市场等业务上下文，但 runner 真实请求只会发送 query 文本。`target_brand`、`industry` 和 `queries` 必填；`market` 可选，省略时记录为 `未指定市场`。

仓库不会内置真实任务数据、历史品牌 alias 或固定行业 query。真实任务请在本机准备 `job_config.local.json`，该类本地文件已在 `.gitignore` 中排除。

```json
{
  "target_brand": "<TARGET_BRAND>",
  "target_aliases": ["<OPTIONAL_TARGET_ALIAS>"],
  "industry": "<INDUSTRY>",
  "market": "<OPTIONAL_MARKET>",
  "queries": [
    {
      "query_id": "q001",
      "query": "<QUERY_TEXT>",
      "persona": "<OPTIONAL_PERSONA>",
      "tags": ["<OPTIONAL_TAG>"]
    },
    "<QUERY_TEXT>"
  ],
  "repeats": 20,
  "model": "<MODEL_OR_ENDPOINT_ID>",
  "web_search_limit": 5,
  "concurrency": 4,
  "start_interval_seconds": 0
}
```

`build-job` 会自动生成 `work/query_manifest.csv`。真实请求只读取其中的 `query` 文本；文件可保留 `query_id`、`persona`、`stage`、`tags` 等审计元数据，供 runner 分配采样单元和后处理使用。

```csv
query_id,query
q001,<QUERY_TEXT>
q002,<QUERY_TEXT>
```

目标品牌、行业和市场不会进入真实采样 prompt，只保存在 `job_manifest.json` 中用于后处理。无明确区域属性的品牌可以不填写 `market`。
配置契约可参考 `data/job_config.schema.json`。根级未知字段会被拒绝，避免 `repeat`、`websearch_limit` 这类 typo 被静默忽略；query 对象内部可保留 persona、stage、tags 等运行元数据。

默认情况下，任务会生成到项目内 `.runs/{job_id}`。如果传入 `--out-dir`，则使用指定目录。
指定目录已存在且非空时默认拒绝覆盖；`--force` 只允许覆盖已识别为 `geo-job-v1` 的旧任务目录，拒绝覆盖项目根、Home 等危险路径。

## 常用命令

### 配置检查

```bash
geo-monitor doctor
```

默认不调用真实 API。

### 新版任务工作流

准备本地任务配置后，可按以下顺序执行新版任务工作流。本地配置文件建议命名为 `job_config.local.json`，不要提交到版本库。

```bash
geo-monitor validate-job-config job_config.local.json

geo-monitor build-job job_config.local.json

geo-monitor run-job .runs/job_YYYYMMDDTHHMMSSZ_xxxxxx \
  --resume \
  --confirm-cost

geo-monitor analyze-job .runs/job_YYYYMMDDTHHMMSSZ_xxxxxx \
  --confirm-cost
```

`run-job` 读取 `job_manifest.json` 和 `work/query_manifest.csv`，真实 API 请求中的 `input` 只等于 query 文本。live 模式会先输出预检信息，包括计划采样单元、已完成单元、预计 live 请求数、后续分析 LLM 请求数、并发和启动间隔；仍有待采样 live 请求时必须显式加 `--confirm-cost`。
`analyze-job` 对 live success 做 LLM 品牌抽取和归一化，也会先输出分析预检；预计分析 LLM 请求数大于 0 时同样需要 `--confirm-cost`。
`concurrency` 写在 job 配置中；未填写时读取 `CONCURRENCY`，范围为 1-8。默认值是 1；真实 API 批量采样时建议先从 2 或 4 开始，并结合 `sleep_seconds` 和 `start_interval_seconds` 控制限流风险。并发输出会在单进程内按完成顺序串行写入 JSONL，减少慢请求阻塞已完成结果落盘的窗口。
断点续跑只会跳过同 `query_id + repeat_index` 且 `request_hash` 一致的成功记录。如果模型、query 文本或 Web Search 参数变化，会重新采集该采样单元。

真实小样本 smoke 也应使用已经替换成真实品牌和 query 的配置，再限制本次运行范围：

```bash
geo-monitor run-job .runs/job_YYYYMMDDTHHMMSSZ_xxxxxx \
  --limit 1 \
  --confirm-cost

geo-monitor run-job .runs/job_YYYYMMDDTHHMMSSZ_xxxxxx \
  --only-query-id q001,q003 \
  --confirm-cost
```

子集运行只采集选中的 query，不会把整个 job 标记为完整完成。`analyze-job` 会对 live success 的 `response_text` 逐条做 LLM 品牌/机构抽取，并生成开放式品牌发现报告。

如需不消耗 API 成本验收完整报告链路，可使用 mock + demo 分析：

```bash
geo-monitor run-job .runs/job_YYYYMMDDTHHMMSSZ_xxxxxx --mock
geo-monitor analyze-job .runs/job_YYYYMMDDTHHMMSSZ_xxxxxx --include-mock
```

`--include-mock` 只用于 demo/验收，报告会标注 `sample_mode=mock`，不应作为业务结论。如果同一 job 同时存在 live success 和 mock 样本，分析默认只使用 live success，避免混样。

`analyze-job` 成功后默认清理 `work/` 中间文件。调试时可保留：

```bash
geo-monitor analyze-job .runs/job_YYYYMMDDTHHMMSSZ_xxxxxx --keep-work --confirm-cost
```

也可以手动清理：

```bash
geo-monitor cleanup-job .runs/job_YYYYMMDDTHHMMSSZ_xxxxxx
```

## 输出 JSONL 字段

`run-job` 会向 `.runs/{job_id}/raw/attempts.jsonl` 写入每个采样单元的审计记录。每行是一条 JSON：

```json
{
  "run_id": "run_xxx",
  "query_id": "q001",
  "model": "<MODEL_OR_ENDPOINT_ID>",
  "input_query": "<QUERY_TEXT>",
  "status": "success",
  "response_text": "...",
  "sources": [
    {
      "title": "...",
      "url": "https://...",
      "domain": "...",
      "snippet": "...",
      "source_type": "url_citation",
      "rank": 1,
      "raw": {}
    }
  ],
  "usage": {},
  "latency_ms": 1234,
  "error": null,
  "raw_request": {},
  "raw_response": {},
  "started_at": "2026-07-03T00:00:00+00:00",
  "completed_at": "2026-07-03T00:00:01+00:00"
}
```

如果 Web Search source 未被 parser 解析到，仍会保留完整 `raw_response`，便于后续根据真实返回结构调整解析器。

新版 job 工作流输出结构：

```text
.runs/{job_id}/
job_manifest.json
work/
  query_manifest.csv                 # 运行时临时文件，默认分析完成后清理
  brand_mentions_raw.jsonl           # 运行时临时文件
  brand_canonical_map_work.json      # 运行时临时文件
raw/
  attempts.jsonl
logs/
  run_summary.json
  analysis_summary.json
  data_quality.json
  extraction_errors.jsonl
  raw_read_errors.jsonl
  cleanup_summary.json
result/
  discovered_brands.csv
  brand_mentions_extracted.csv
  brand_canonical_map.csv
  brand_summary.csv
  sov_summary.csv
  brand_by_query.csv
  query_stability.csv
  source_entity_mentions.csv
  source_domains.csv
  source_urls.csv
  source_by_query.csv
  report.md
  report.html
```

`work/` 是任务运行期 workspace；`raw/`、`result/`、`logs/` 和 `job_manifest.json` 是任务结束后保留的审计和交付文件。

## 测试

```bash
python -m pytest
```

## 注意事项

- 默认并发为 1，避免 API 成本和限流风险；批量采样可在 job 配置中显式设置 `concurrency`。
- `sleep_seconds` 是单次调用完成后的等待；并发模式下如需控制请求启动突发，使用 `start_interval_seconds`。
- 建议先跑 `--dry-run` 和 `--mock`，再用 `--limit 1` 做真实 smoke test。
- 如果供应商控制台要求 endpoint id，请把 `.env` 中的 `LLM_MODEL` 改成对应 endpoint id。
- 该系统只通过官方 API 获取模型回答和引用信息，不会自动抓取引用网页正文。
- 当前报告基于 API + Web Search 采样，不等同于 App 端真实用户界面、账号画像或客户端策略下的排名。
- 新版开放式品牌发现来自 LLM 对回答文本的实体抽取；它不依赖预置竞品 alias，但可通过 `target_aliases` 增强目标品牌命中，关键样本仍建议人工复核。
- SOV 主口径为品牌命中事件份额：品牌出现的成功回答数 / 所有品牌的成功回答命中数。一个回答可能命中多个品牌，因此它不等于真实市场份额、App 端真实排名或成功回答占比。
- `analyze-job` 会输出 `data_quality.json`。当样本不完整、raw 有坏行或抽取错误较高时，报告会降级为观察线索。
- `data_quality.json` 会检查采样单元完整性、重复单元、raw 读取错误，以及 raw 记录中的 query/model/web_search_limit/request_hash 是否与当前 job_manifest 契约一致。
- 契约不一致或不属于当前 job 计划的 success/mock 样本会保留在 `data_quality.json` 中，但不会进入品牌 SOV、source 和趋势统计。
- 品牌 SOV 会排除 `sov_eligible=false` 的实体；LLM 抽取结果缺省不进入 SOV，只有明确标记 eligible 且类型属于品牌、公司、企业、设计机构、工作室等业务主体时才进入品牌份额。媒体、来源、协会、政府、榜单、奖项、平台泛主体会进入 `source_entity_mentions.csv`，不进入品牌份额。
- 来源表基于响应结构中的 source URL 解析，`parsed_source_occurrences` 和 `avg_source_order` 不等同于网页真实引用次数或搜索排名。
- 每次 `analyze-job` 会幂等更新 `.runs/index.jsonl`、`.runs/aggregate/brand_trends.csv` 和 `.runs/aggregate/target_brand_trends.csv`。只有 model、query_set_hash、web_search_limit、repeats、sample_mode、comparability_key 等关键字段一致时，跨 job 趋势才具备可比性。
- 本工具只覆盖用户配置的固定 query 集合，不代表完整行业需求。

## 人工 App 抽检建议

本项目不做 App 自动化采集。若需要校准 API 与目标 App 的差异，建议每批固定抽检 5-10 条 query：

1. 在目标 App 手工输入同一 query。
2. 记录回答中出现的品牌集合、首屏品牌、明显排序、来源/引用和回答长度。
3. 与本工具的 API + Web Search 结果并排比较。
4. 若品牌集合或顺序差异明显，应在对外报告中降低结论强度，不把 API 结果表述为 App 排名。
