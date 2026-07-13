"""Job bundle paths, schema versions, statuses, and domain errors."""

JOB_MANIFEST = "job_manifest.json"
RUNS_DIR = ".runs"
WORK_DIR = "work"
RAW_DIR = "raw"
RESULT_DIR = "result"
LOGS_DIR = "logs"
QUERY_MANIFEST = f"{WORK_DIR}/query_manifest.csv"
RAW_ATTEMPTS = "raw/attempts.jsonl"
GEO_JOB_V1 = "geo-job-v1"
GEO_JOB_V2 = "geo-job-v2"
GEO_JOB_V3 = "geo-job-v3"
QUERY_MANIFEST_V1 = "query-manifest-v1"
RUN_SUMMARY = "logs/run_summary.json"
DIAGNOSTIC_RUN_SUMMARY = "logs/diagnostic_run_summary.json"
CLEANUP_SUMMARY = "logs/cleanup_summary.json"
BUNDLE_LOCK = "logs/bundle.lock"

JOB_CONFIG_KEYS = {
    "target_brand",
    "target_aliases",
    "owned_domains",
    "industry",
    "market",
    "queries",
    "repeats",
    "model",
    "web_search_limit",
    "adapter",
    "adapter_options",
    "analysis_model",
    "analysis_adapter",
    "concurrency",
    "start_interval_seconds",
}

ALLOWED_STATUSES = {
    "built",
    "running",
    "ran",
    "ran_partial",
    "run_failed",
    "interrupted",
    "analyzing",
    "analyzed",
    "analyzed_partial",
    "analysis_failed",
    "analyzed_cleaned",
    "analyzed_partial_cleaned",
    "cleaned",
}


class JobError(ValueError):
    """Invalid job configuration, state, or bundle content."""


class QueryManifestIntegrityError(JobError):
    """A frozen query manifest no longer matches its recorded fingerprint."""


class QueryManifestSourceError(JobError):
    """An external query manifest source is outside trusted study roots."""
