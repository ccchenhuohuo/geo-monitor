import json
from pathlib import Path

import pytest

from geo_monitor.config import Settings
from geo_monitor.job import JobError, build_job_bundle, load_job_manifest, run_job_bundle
from geo_monitor.jobs.profiles import build_comparability_profile
from geo_monitor.request_fingerprint import analysis_fingerprint, base_url_fingerprint


def _build_bundle(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "job.json"
    bundle = tmp_path / "bundle"
    config_path.write_text(
        json.dumps(
            {
                "target_brand": "TestBrand",
                "target_aliases": ["Test Alias"],
                "owned_domains": ["example.com"],
                "industry": "TestIndustry",
                "market": "TestMarket",
                "queries": ["best providers"],
                "repeats": 2,
                "model": "test-model",
            }
        ),
        encoding="utf-8",
    )
    build_job_bundle(config_path, bundle, settings=Settings(llm_api_key=None))
    return bundle, bundle / "job_manifest.json"


def _tamper(path: Path, mutate) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def _rebuild_comparability(manifest: dict) -> None:
    manifest["comparability_profile"] = build_comparability_profile(
        manifest.get("query_manifest") or {},
        manifest.get("queries") or [],
        int(manifest["repeats"]),
        manifest["sampling_profile"],
        manifest["analysis_profile"],
        target_brand=manifest["target_brand"],
        target_aliases=manifest.get("target_aliases") or [],
        owned_domains=manifest.get("owned_domains") or [],
        industry=manifest["industry"],
        market=manifest.get("market") or "",
    )


def test_legacy_generic_manifest_without_profiles_migrates_transport_preserving_name(tmp_path):
    bundle, manifest_path = _build_bundle(tmp_path)

    def mutate(manifest):
        manifest["schema_version"] = "geo-job-v2"
        manifest["adapter"] = "openai_responses_web_search"
        manifest.pop("sampling_profile")
        manifest.pop("analysis_profile")
        manifest.pop("comparability_profile")

    _tamper(manifest_path, mutate)

    explicit_settings = Settings(llm_base_url="https://custom.example/v1")
    migrated = load_job_manifest(bundle, settings=explicit_settings)

    assert migrated["adapter"] == "openai_compatible_responses_web_search"
    assert migrated["sampling_profile"]["adapter"] == "openai_compatible_responses_web_search"
    assert migrated["sampling_profile"]["inferred_from_legacy"] is True
    assert migrated["sampling_profile"]["base_url_fingerprint"] == base_url_fingerprint(
        "https://custom.example/v1"
    )

    result = run_job_bundle(bundle, mock=True, settings=explicit_settings)

    assert result["errors"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("web_search_required", False),
        ("web_search_limit_effective", True),
        ("request_fingerprint_version", "attacker-version"),
    ],
)
def test_manifest_rejects_sampling_semantic_downgrade_even_with_recomputed_comparability(tmp_path, field, value):
    bundle, manifest_path = _build_bundle(tmp_path)

    def mutate(manifest):
        manifest["sampling_profile"][field] = value
        _rebuild_comparability(manifest)

    _tamper(manifest_path, mutate)

    with pytest.raises(JobError, match=field):
        load_job_manifest(bundle)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("sampling_profile", "provider", "deepseek"),
        ("sampling_profile", "provider_sdk", "unexpected-sdk"),
        ("analysis_profile", "provider", "deepseek"),
        ("analysis_profile", "adapter", "deepseek_chat_completions_text"),
    ],
)
def test_manifest_rejects_provider_adapter_identity_tampering(tmp_path, section, field, value):
    bundle, manifest_path = _build_bundle(tmp_path)
    _tamper(manifest_path, lambda manifest: manifest[section].__setitem__(field, value))

    with pytest.raises(JobError, match="profile identity mismatch"):
        load_job_manifest(bundle)


def test_manifest_rejects_stale_analysis_fingerprint(tmp_path):
    bundle, manifest_path = _build_bundle(tmp_path)
    _tamper(manifest_path, lambda manifest: manifest["analysis_profile"].__setitem__("model", "tampered-model"))

    with pytest.raises(JobError, match=r"analysis_profile\.analysis_fingerprint 与 profile 内容不一致"):
        load_job_manifest(bundle)


def test_manifest_rejects_analysis_and_comparability_fingerprint_drift(tmp_path):
    bundle, manifest_path = _build_bundle(tmp_path)

    def mutate(manifest):
        profile = manifest["analysis_profile"]
        profile["model"] = "tampered-model"
        profile["analysis_fingerprint"] = analysis_fingerprint(profile)

    _tamper(manifest_path, mutate)

    with pytest.raises(JobError, match=r"comparability_profile\.analysis_fingerprint"):
        load_job_manifest(bundle)


@pytest.mark.parametrize(
    "field",
    [
        "query_manifest_sha256",
        "repeats",
        "source_grain",
        "study_fingerprint",
        "sampling_fingerprint",
    ],
)
def test_manifest_rejects_comparability_profile_tampering(tmp_path, field):
    bundle, manifest_path = _build_bundle(tmp_path)
    _tamper(manifest_path, lambda manifest: manifest["comparability_profile"].__setitem__(field, "tampered"))

    with pytest.raises(JobError, match=rf"comparability_profile\.{field}"):
        load_job_manifest(bundle)
