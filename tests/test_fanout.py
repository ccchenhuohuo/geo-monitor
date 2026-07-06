import csv

from geo_monitor.fanout import FANOUT_FIELDS, REGISTRY_AUDIT_FIELDS, FanoutError, build_query_manifest


def _write_seed(path, *, persona="beginner"):
    path.write_text(
        f"""
seeds:
  - seed_id: sample_beginner
    category: sample_category
    intent: product_recommendation
    seed_query: "推荐一款适合新手的示例产品"
    personas:
      - {persona}
""".strip(),
        encoding="utf-8",
    )


def _write_registry(path, *, template="请以专业买手视角回答：{seed_query}", fallback=False):
    fallback_text = "\nfallback:\n  template_id: default\n  template: \"兜底回答：{seed_query}\"" if fallback else ""
    path.write_text(
        f"""
schema_version: persona-template-registry-v1
registry_id: custom_zh_cn
registry_version: v1
personas:
  beginner:
    template_id: expert_buyer
    template: "{template}"
{fallback_text}
""".strip(),
        encoding="utf-8",
    )


def _read_rows(path):
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_fanout_writes_byte_stable_manifest(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    out = tmp_path / "manifests" / "query_manifest.v1.csv"
    seed.write_text(
        """
seeds:
  - seed_id: sample_beginner
    category: sample_category
    intent: product_recommendation
    seed_query: "推荐一款适合新手的示例产品"
    personas:
      - beginner
      - budget_sensitive
""".strip(),
        encoding="utf-8",
    )

    build_query_manifest(seed, out)
    first = out.read_bytes()
    build_query_manifest(seed, out, force=True)
    second = out.read_bytes()

    assert first == second
    rows = _read_rows(out)
    assert rows
    assert list(rows[0].keys()) == FANOUT_FIELDS
    assert [row["persona"] for row in rows] == sorted(row["persona"] for row in rows)
    assert all(row["query_id"] == row["variant_id"] for row in rows)


def test_fanout_does_not_overwrite_by_default(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    out = tmp_path / "query_manifest.csv"
    seed.write_text("seeds:\n  - seed_id: a\n    seed_query: q\n    personas:\n      - beginner\n", encoding="utf-8")
    build_query_manifest(seed, out)

    try:
        build_query_manifest(seed, out)
    except FanoutError as exc:
        assert "--force" in str(exc)
    else:
        raise AssertionError("expected FanoutError")


def test_fanout_rejects_non_slug_ids(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    out = tmp_path / "query_manifest.csv"
    seed.write_text("seeds:\n  - seed_id: bad/id\n    seed_query: q\n    personas:\n      - beginner\n", encoding="utf-8")

    try:
        build_query_manifest(seed, out)
    except FanoutError as exc:
        assert "seed_id" in str(exc)
    else:
        raise AssertionError("expected FanoutError")


def test_external_persona_template_registry_writes_audit_columns(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    registry = tmp_path / "persona_templates.yaml"
    out = tmp_path / "query_manifest.csv"
    _write_seed(seed)
    _write_registry(registry)

    result = build_query_manifest(seed, out, persona_template_registry_path=registry)
    first = out.read_bytes()
    build_query_manifest(seed, out, force=True, persona_template_registry_path=registry)
    second = out.read_bytes()
    row = _read_rows(out)[0]

    assert first == second
    assert list(row.keys()) == FANOUT_FIELDS + REGISTRY_AUDIT_FIELDS
    assert row["template_id"] == "expert_buyer"
    assert row["query"] == "请以专业买手视角回答：推荐一款适合新手的示例产品"
    assert row["template_source"] == "registry"
    assert row["template_registry_id"] == "custom_zh_cn"
    assert row["template_registry_version"] == "v1"
    assert row["template_registry_schema_version"] == "persona-template-registry-v1"
    assert len(row["template_registry_sha256"]) == 64
    assert len(row["template_hash"]) == 64
    assert result["template_registry_id"] == "custom_zh_cn"
    assert result["template_registry_persona_count"] == 1


def test_registry_template_text_changes_query_id(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    registry = tmp_path / "persona_templates.yaml"
    first_out = tmp_path / "first.csv"
    second_out = tmp_path / "second.csv"
    _write_seed(seed)
    _write_registry(registry, template="第一版：{seed_query}")
    build_query_manifest(seed, first_out, persona_template_registry_path=registry)
    first_id = _read_rows(first_out)[0]["query_id"]

    _write_registry(registry, template="第二版：{seed_query}")
    build_query_manifest(seed, second_out, persona_template_registry_path=registry)
    second_id = _read_rows(second_out)[0]["query_id"]

    assert first_id != second_id


def test_registry_requires_known_persona_or_explicit_fallback(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    registry = tmp_path / "persona_templates.yaml"
    out = tmp_path / "query_manifest.csv"
    _write_seed(seed, persona="new_persona")
    _write_registry(registry)

    try:
        build_query_manifest(seed, out, persona_template_registry_path=registry)
    except FanoutError as exc:
        assert "new_persona" in str(exc)
    else:
        raise AssertionError("expected FanoutError")

    _write_registry(registry, fallback=True)
    build_query_manifest(seed, out, persona_template_registry_path=registry)
    row = _read_rows(out)[0]
    assert row["template_source"] == "registry_fallback"
    assert row["template_id"] == "default"


def test_registry_rejects_invalid_templates(tmp_path):
    seed = tmp_path / "seed_prompts.yaml"
    registry = tmp_path / "persona_templates.yaml"
    out = tmp_path / "query_manifest.csv"
    _write_seed(seed)
    _write_registry(registry, template="缺少占位符")

    try:
        build_query_manifest(seed, out, persona_template_registry_path=registry)
    except FanoutError as exc:
        assert "seed_query" in str(exc)
    else:
        raise AssertionError("expected FanoutError")

    _write_registry(registry, template="未知 {brand} 占位符 {seed_query}")
    try:
        build_query_manifest(seed, out, persona_template_registry_path=registry)
    except FanoutError as exc:
        assert "brand" in str(exc)
    else:
        raise AssertionError("expected FanoutError")
