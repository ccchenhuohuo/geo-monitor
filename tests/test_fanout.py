import csv

from geo_monitor.fanout import FANOUT_FIELDS, FanoutError, build_query_manifest


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
    with out.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
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
