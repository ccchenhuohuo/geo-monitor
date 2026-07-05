from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any


QUERY_MANIFEST_SCHEMA_VERSION = "query-manifest-v1"
DEFAULT_FANOUT_VERSION = "template-v1"
FANOUT_FIELDS = [
    "query_id",
    "variant_id",
    "seed_id",
    "seed_query",
    "category",
    "intent",
    "persona",
    "template_id",
    "query",
    "language",
    "generation_method",
    "fanout_version",
    "manifest_version",
    "locked_at",
]
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")

PERSONA_TEMPLATES = {
    "budget_sensitive": ("budget_limit", "预算有限的情况下，{seed_query}"),
    "quality_oriented": ("quality_focus", "更看重品质和长期使用体验，{seed_query}"),
    "comparison_shopper": ("compare_options", "{seed_query}，请对比几个主流选择"),
    "beginner": ("beginner_help", "我是新手，{seed_query}"),
    "convenience_first": ("convenience", "希望省心省事，{seed_query}"),
}


class FanoutError(ValueError):
    pass


def build_query_manifest(
    input_path: str | Path,
    output_path: str | Path,
    *,
    force: bool = False,
    fanout_version: str = DEFAULT_FANOUT_VERSION,
    manifest_version: str = "v1",
    locked_at: str = "",
) -> dict[str, Any]:
    source = Path(input_path)
    target = Path(output_path)
    if target.exists() and not force:
        raise FanoutError(f"输出 manifest 已存在：{target}。如需覆盖请使用 --force")
    rows = fanout_seed_prompts(source, fanout_version=fanout_version, manifest_version=manifest_version, locked_at=locked_at)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as f:
        writer = csv.DictWriter(f, fieldnames=FANOUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return {"output": str(target), "row_count": len(rows), "schema_version": QUERY_MANIFEST_SCHEMA_VERSION}


def fanout_seed_prompts(
    input_path: str | Path,
    *,
    fanout_version: str = DEFAULT_FANOUT_VERSION,
    manifest_version: str = "v1",
    locked_at: str = "",
) -> list[dict[str, str]]:
    data = _load_yaml(Path(input_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("seeds"), list) or not data["seeds"]:
        raise FanoutError("seed_prompts.yaml 必须包含非空 seeds 数组")
    rows: list[dict[str, str]] = []
    seen_query_ids: set[str] = set()
    seen_variant_ids: set[str] = set()
    for seed in data["seeds"]:
        if not isinstance(seed, dict):
            raise FanoutError("seeds 中每一项都必须是对象")
        seed_id = _required_slug(seed, "seed_id")
        seed_query = _required_text(seed, "seed_query")
        personas = seed.get("personas")
        if not isinstance(personas, list) or not personas:
            raise FanoutError(f"{seed_id}.personas 必须是非空数组")
        for persona_value in personas:
            persona = _validate_slug(str(persona_value).strip(), "persona")
            template_id, template = PERSONA_TEMPLATES.get(persona, ("default", "{seed_query}"))
            _validate_slug(template_id, "template_id")
            digest = hashlib.sha256(f"{seed_id}|{persona}|{template_id}|{seed_query}|{fanout_version}".encode("utf-8")).hexdigest()[:8]
            variant_id = f"{seed_id}__{persona}__{template_id}__{digest}"
            _validate_slug(variant_id, "variant_id")
            if variant_id in seen_variant_ids:
                raise FanoutError(f"variant_id 重复：{variant_id}")
            if variant_id in seen_query_ids:
                raise FanoutError(f"query_id 重复：{variant_id}")
            seen_variant_ids.add(variant_id)
            seen_query_ids.add(variant_id)
            rows.append(
                {
                    "query_id": variant_id,
                    "variant_id": variant_id,
                    "seed_id": seed_id,
                    "seed_query": seed_query,
                    "category": str(seed.get("category") or ""),
                    "intent": str(seed.get("intent") or ""),
                    "persona": persona,
                    "template_id": template_id,
                    "query": template.format(seed_query=seed_query),
                    "language": str(seed.get("language") or "zh-CN"),
                    "generation_method": "template",
                    "fanout_version": fanout_version,
                    "manifest_version": manifest_version,
                    "locked_at": locked_at,
                }
            )
    return sorted(rows, key=lambda row: (row["seed_id"], row["persona"], row["template_id"], row["query_id"]))


def validate_slug(value: str, field: str) -> str:
    return _validate_slug(value, field)


def _required_slug(row: dict[str, Any], field: str) -> str:
    return _validate_slug(str(row.get(field) or "").strip(), field)


def _required_text(row: dict[str, Any], field: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise FanoutError(f"{field} 不能为空")
    return value


def _validate_slug(value: str, field: str) -> str:
    if not value:
        raise FanoutError(f"{field} 不能为空")
    if not SLUG_RE.fullmatch(value):
        raise FanoutError(f"{field} 只能包含 [a-zA-Z0-9_-]：{value}")
    return value


def _load_yaml(text: str) -> Any:
    try:
        import yaml
    except ModuleNotFoundError:
        return _load_seed_yaml_subset(text)
    return yaml.safe_load(text)


def _load_seed_yaml_subset(text: str) -> dict[str, Any]:
    seeds: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    list_key: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped == "seeds:":
            continue
        if stripped.startswith("- ") and line.startswith("  - "):
            current = {}
            seeds.append(current)
            list_key = None
            item = stripped[2:].strip()
            if item:
                key, value = _split_yaml_scalar(item)
                current[key] = value
            continue
        if current is None:
            raise FanoutError("无法解析 seed_prompts.yaml；请安装 PyYAML 以支持更完整 YAML")
        if stripped.startswith("- ") and list_key:
            current.setdefault(list_key, []).append(_unquote(stripped[2:].strip()))
            continue
        key, value = _split_yaml_scalar(stripped)
        if value == "":
            current[key] = []
            list_key = key
        else:
            current[key] = value
            list_key = None
    return {"seeds": seeds}


def _split_yaml_scalar(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise FanoutError("无法解析 seed_prompts.yaml；请安装 PyYAML 以支持更完整 YAML")
    key, value = text.split(":", 1)
    return key.strip(), _unquote(value.strip())


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
