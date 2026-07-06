from __future__ import annotations

import csv
import hashlib
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any


QUERY_MANIFEST_SCHEMA_VERSION = "query-manifest-v1"
DEFAULT_FANOUT_VERSION = "template-v1"
PERSONA_TEMPLATE_REGISTRY_SCHEMA_VERSION = "persona-template-registry-v1"
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
REGISTRY_AUDIT_FIELDS = [
    "template_source",
    "template_registry_id",
    "template_registry_version",
    "template_registry_schema_version",
    "template_registry_sha256",
    "template_hash",
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


@dataclass(frozen=True)
class RegistryTemplate:
    template_id: str
    template: str
    template_hash: str


@dataclass(frozen=True)
class PersonaTemplateRegistry:
    registry_id: str
    registry_version: str
    schema_version: str
    sha256: str
    personas: dict[str, RegistryTemplate]
    fallback: RegistryTemplate | None = None


@dataclass(frozen=True)
class ResolvedPersonaTemplate:
    template_id: str
    template: str
    template_source: str = ""
    template_registry_id: str = ""
    template_registry_version: str = ""
    template_registry_schema_version: str = ""
    template_registry_sha256: str = ""
    template_hash: str = ""


def build_query_manifest(
    input_path: str | Path,
    output_path: str | Path,
    *,
    force: bool = False,
    fanout_version: str = DEFAULT_FANOUT_VERSION,
    manifest_version: str = "v1",
    locked_at: str = "",
    persona_template_registry_path: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(input_path)
    target = Path(output_path)
    if target.exists() and not force:
        raise FanoutError(f"输出 manifest 已存在：{target}。如需覆盖请使用 --force")
    registry = _load_persona_template_registry(persona_template_registry_path)
    data = _load_yaml(source.read_text(encoding="utf-8"))
    rows = _fanout_seed_rows(data, fanout_version=fanout_version, manifest_version=manifest_version, locked_at=locked_at, registry=registry)
    fieldnames = FANOUT_FIELDS + REGISTRY_AUDIT_FIELDS if registry is not None else FANOUT_FIELDS
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    result: dict[str, Any] = {"output": str(target), "row_count": len(rows), "schema_version": QUERY_MANIFEST_SCHEMA_VERSION}
    if registry is not None:
        result.update(registry_metadata(registry))
    return result


def fanout_seed_prompts(
    input_path: str | Path,
    *,
    fanout_version: str = DEFAULT_FANOUT_VERSION,
    manifest_version: str = "v1",
    locked_at: str = "",
    persona_template_registry_path: str | Path | None = None,
) -> list[dict[str, str]]:
    registry = _load_persona_template_registry(persona_template_registry_path)
    data = _load_yaml(Path(input_path).read_text(encoding="utf-8"))
    return _fanout_seed_rows(data, fanout_version=fanout_version, manifest_version=manifest_version, locked_at=locked_at, registry=registry)


def validate_slug(value: str, field: str) -> str:
    return _validate_slug(value, field)


def registry_metadata(registry: PersonaTemplateRegistry) -> dict[str, Any]:
    return {
        "template_registry_id": registry.registry_id,
        "template_registry_version": registry.registry_version,
        "template_registry_schema_version": registry.schema_version,
        "template_registry_sha256": registry.sha256,
        "template_registry_persona_count": len(registry.personas),
        "template_registry_has_fallback": registry.fallback is not None,
    }


def _fanout_seed_rows(
    data: Any,
    *,
    fanout_version: str,
    manifest_version: str,
    locked_at: str,
    registry: PersonaTemplateRegistry | None,
) -> list[dict[str, str]]:
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
            resolved = _resolve_persona_template(persona, seed_id=seed_id, registry=registry)
            digest_parts = [seed_id, persona, resolved.template_id, seed_query, fanout_version]
            if registry is not None:
                digest_parts = [seed_id, persona, resolved.template_id, resolved.template_hash, seed_query, fanout_version]
            digest = hashlib.sha256("|".join(digest_parts).encode("utf-8")).hexdigest()[:8]
            variant_id = f"{seed_id}__{persona}__{resolved.template_id}__{digest}"
            _validate_slug(variant_id, "variant_id")
            if variant_id in seen_variant_ids:
                raise FanoutError(f"variant_id 重复：{variant_id}")
            if variant_id in seen_query_ids:
                raise FanoutError(f"query_id 重复：{variant_id}")
            seen_variant_ids.add(variant_id)
            seen_query_ids.add(variant_id)
            row = {
                "query_id": variant_id,
                "variant_id": variant_id,
                "seed_id": seed_id,
                "seed_query": seed_query,
                "category": str(seed.get("category") or ""),
                "intent": str(seed.get("intent") or ""),
                "persona": persona,
                "template_id": resolved.template_id,
                "query": resolved.template.format(seed_query=seed_query),
                "language": str(seed.get("language") or "zh-CN"),
                "generation_method": "template",
                "fanout_version": fanout_version,
                "manifest_version": manifest_version,
                "locked_at": locked_at,
            }
            if registry is not None:
                row.update(
                    {
                        "template_source": resolved.template_source,
                        "template_registry_id": resolved.template_registry_id,
                        "template_registry_version": resolved.template_registry_version,
                        "template_registry_schema_version": resolved.template_registry_schema_version,
                        "template_registry_sha256": resolved.template_registry_sha256,
                        "template_hash": resolved.template_hash,
                    }
                )
            rows.append(row)
    return sorted(rows, key=lambda row: (row["seed_id"], row["persona"], row["template_id"], row["query_id"]))


def _resolve_persona_template(persona: str, *, seed_id: str, registry: PersonaTemplateRegistry | None) -> ResolvedPersonaTemplate:
    if registry is None:
        template_id, template = PERSONA_TEMPLATES.get(persona, ("default", "{seed_query}"))
        _validate_template(template)
        return ResolvedPersonaTemplate(template_id=_validate_slug(template_id, "template_id"), template=template)
    source = "registry"
    template = registry.personas.get(persona)
    if template is None:
        template = registry.fallback
        source = "registry_fallback"
    if template is None:
        raise FanoutError(f"{seed_id}.personas 包含 registry 未定义 persona：{persona}")
    return ResolvedPersonaTemplate(
        template_id=template.template_id,
        template=template.template,
        template_source=source,
        template_registry_id=registry.registry_id,
        template_registry_version=registry.registry_version,
        template_registry_schema_version=registry.schema_version,
        template_registry_sha256=registry.sha256,
        template_hash=template.template_hash,
    )


def _load_persona_template_registry(path: str | Path | None) -> PersonaTemplateRegistry | None:
    if path is None:
        return None
    registry_path = Path(path)
    raw = registry_path.read_bytes()
    text = raw.decode("utf-8")
    data = _load_registry_yaml(text)
    if not isinstance(data, dict):
        raise FanoutError("persona template registry 必须是对象")
    schema_version = _required_text(data, "schema_version")
    if schema_version != PERSONA_TEMPLATE_REGISTRY_SCHEMA_VERSION:
        raise FanoutError(f"不支持的 persona template registry schema_version：{schema_version}")
    registry_id = _required_slug(data, "registry_id")
    registry_version = _required_text(data, "registry_version")
    personas_data = data.get("personas")
    if not isinstance(personas_data, dict) or not personas_data:
        raise FanoutError("persona template registry 必须包含非空 personas mapping")
    personas: dict[str, RegistryTemplate] = {}
    for persona, item in personas_data.items():
        persona_slug = _validate_slug(str(persona).strip(), "persona")
        if persona_slug in personas:
            raise FanoutError(f"persona template registry 中 persona 重复：{persona_slug}")
        personas[persona_slug] = _load_registry_template(item, f"personas.{persona_slug}")
    fallback = None
    if "fallback" in data and data["fallback"] is not None:
        fallback = _load_registry_template(data["fallback"], "fallback")
    return PersonaTemplateRegistry(
        registry_id=registry_id,
        registry_version=registry_version,
        schema_version=schema_version,
        sha256=hashlib.sha256(raw).hexdigest(),
        personas=personas,
        fallback=fallback,
    )


def _load_registry_template(item: Any, field: str) -> RegistryTemplate:
    if not isinstance(item, dict):
        raise FanoutError(f"{field} 必须是对象")
    template_id = _required_slug(item, "template_id")
    template = _required_text(item, "template")
    _validate_template(template)
    return RegistryTemplate(template_id=template_id, template=template, template_hash=hashlib.sha256(template.encode("utf-8")).hexdigest())


def _load_registry_yaml(text: str) -> Any:
    try:
        import yaml
    except ModuleNotFoundError:
        return _load_registry_yaml_subset(text)
    return yaml.safe_load(text)


def _load_registry_yaml_subset(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    personas: dict[str, dict[str, str]] = {}
    fallback: dict[str, str] | None = None
    current_section: str | None = None
    current_persona: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent == 0:
            key, value = _split_yaml_scalar(stripped)
            current_persona = None
            if key == "personas" and value == "":
                data["personas"] = personas
                current_section = "personas"
            elif key == "fallback" and value == "":
                fallback = {}
                data["fallback"] = fallback
                current_section = "fallback"
            else:
                data[key] = value
                current_section = None
            continue
        if current_section == "personas" and indent == 2:
            if not stripped.endswith(":"):
                raise FanoutError("无法解析 persona template registry；请安装 PyYAML 以支持更完整 YAML")
            current_persona = stripped[:-1].strip()
            personas[current_persona] = {}
            continue
        if current_section == "personas" and indent == 4 and current_persona:
            key, value = _split_yaml_scalar(stripped)
            personas[current_persona][key] = value
            continue
        if current_section == "fallback" and indent == 2 and fallback is not None:
            key, value = _split_yaml_scalar(stripped)
            fallback[key] = value
            continue
        raise FanoutError("无法解析 persona template registry；请安装 PyYAML 以支持更完整 YAML")
    return data


def _validate_template(template: str) -> None:
    formatter = string.Formatter()
    fields = [field_name for _, field_name, _, _ in formatter.parse(template) if field_name]
    unsupported = [field for field in fields if field != "seed_query"]
    if unsupported:
        raise FanoutError(f"template 只支持 {{seed_query}} 占位符，不支持：{', '.join(unsupported)}")
    if "seed_query" not in fields:
        raise FanoutError("template 必须包含 {seed_query} 占位符")
    try:
        template.format(seed_query="test")
    except (IndexError, KeyError, ValueError) as exc:
        raise FanoutError(f"template 格式无效：{exc}") from exc


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
