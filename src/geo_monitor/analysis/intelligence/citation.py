"""Citation URL, source-type, attribution, and gap intelligence."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .common import as_bool, mean, response_key, safe_div, score100, trace_fields

TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "msclkid",
    "ref",
    "referrer",
    "spm",
    "yclid",
}

SOURCE_TYPES = {
    "official_website",
    "ecommerce",
    "content_community",
    "forum_qa",
    "media_review",
    "wiki_knowledge",
    "video_platform",
    "social_media",
    "blog",
    "unknown",
}


def canonicalize_url(url: Any) -> str | None:
    """Canonicalize an HTTP(S) URL for counting, not for fetching."""

    text = str(url or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return None
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    default_port = port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if default_port else f"{display_host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def classify_source_type(
    url: Any,
    *,
    domain: str = "",
    title: str = "",
    owned_domains: set[str] | None = None,
) -> str:
    canonical = canonicalize_url(url)
    host = _domain(canonical) or _normalize_domain(domain)
    owned = {_normalize_domain(value) for value in (owned_domains or set())}
    if host and any(host == value or host.endswith(f".{value}") for value in owned if value):
        return "official_website"
    value = f"{host} {title}".lower()
    if any(token in value for token in ("jd.com", "tmall.com", "taobao.com", "amazon.", "shop", "store")):
        return "ecommerce"
    if any(token in value for token in ("zhihu.com", "quora.com", "stackoverflow.com", "reddit.com")):
        return "forum_qa"
    if any(token in value for token in ("wikipedia.org", "baike.baidu.com", "百科")):
        return "wiki_knowledge"
    if any(token in value for token in ("youtube.com", "youtu.be", "bilibili.com", "vimeo.com")):
        return "video_platform"
    if any(token in value for token in ("weibo.com", "douyin.com", "tiktok.com", "instagram.com", "facebook.com", "x.com")):
        return "social_media"
    if any(token in value for token in ("xiaohongshu.com", "smzdm.com", "medium.com", "substack.com")):
        return "content_community"
    if any(token in value for token in ("review", "news", "media", "评测", "测评")):
        return "media_review"
    if "blog" in value:
        return "blog"
    return "unknown"


def aggregate_citations(
    source_rows: list[dict[str, Any]],
    brand_attempt_facts: list[dict[str, Any]] | None = None,
    *,
    owned_domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate canonical URL facts with anchor-first attribution."""

    brands_by_response: dict[tuple[str, str, int | str], set[str]] = defaultdict(set)
    for row in brand_attempt_facts or []:
        if "stats_included" in row and not as_bool(row.get("stats_included")):
            continue
        brand = str(row.get("brand_name_canonical") or row.get("brand_name_raw") or "").strip()
        if brand:
            brands_by_response[response_key(row)].add(brand)

    owned = {_normalize_domain(value) for value in (owned_domains or set())}
    events: dict[tuple[tuple[str, str, int | str], str, str], dict[str, Any]] = {}
    for source in source_rows:
        canonical = canonicalize_url(source.get("url"))
        if canonical is None:
            continue
        key = response_key(source)
        explicit_brands = _explicit_brands(source)
        if explicit_brands:
            brands = explicit_brands
            method = "anchor"
        else:
            brands = sorted(brands_by_response.get(key) or {""})
            method = "answer_cooccurrence" if brands != [""] else "unattributed"
        domain = _domain(canonical)
        explicit_type = str(source.get("source_type") or "").strip().lower()
        source_type = (
            explicit_type
            if explicit_type in SOURCE_TYPES
            else classify_source_type(
                canonical,
                domain=domain,
                title=str(source.get("title") or ""),
                owned_domains=owned,
            )
        )
        for brand in brands:
            event_key = (key, canonical, brand)
            candidate = {
                **dict(source),
                "brand_name_canonical": brand,
                "canonical_url": canonical,
                "domain": domain,
                "source_type": source_type,
                "source_attribution_method": method,
                "owned_source": int(any(domain == value or domain.endswith(f".{value}") for value in owned if value)),
            }
            previous = events.get(event_key)
            if previous is None or (previous["source_attribution_method"] != "anchor" and method == "anchor"):
                events[event_key] = candidate

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events.values():
        grouped[(str(event["brand_name_canonical"]), str(event["canonical_url"]))].append(event)
    output: list[dict[str, Any]] = []
    for (brand, canonical), rows in sorted(grouped.items()):
        methods = {str(row["source_attribution_method"]) for row in rows}
        output.append(
            {
                "brand_name_canonical": brand,
                "canonical_url": canonical,
                "domain": str(rows[0]["domain"]),
                "source_type": str(rows[0]["source_type"]),
                "source_attribution_method": "anchor" if "anchor" in methods else sorted(methods)[0],
                "owned_source": int(any(row["owned_source"] for row in rows)),
                "citation_occurrences": len(rows),
                "response_count": len({response_key(row) for row in rows}),
                "citation_denominator": len({response_key(row) for row in source_rows}),
                "trace_response_keys": [list(key) for key in sorted({response_key(row) for row in rows})],
                **_aggregate_trace(rows),
            }
        )
    return output


def summarize_citations(
    citation_rows: list[dict[str, Any]],
    attempt_facts: list[dict[str, Any]] | None = None,
    brand_facts: list[dict[str, Any]] | None = None,
    *,
    owned_domains_configured: bool | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in citation_rows:
        grouped[str(row.get("brand_name_canonical") or "")].append(row)
    for row in brand_facts or []:
        brand = str(row.get("brand_name_canonical") or row.get("brand_name_raw") or "").strip()
        if brand:
            grouped.setdefault(brand, [])
    eligible = len({response_key(row) for row in attempt_facts if as_bool(row.get("stats_included"), default=True)}) if attempt_facts is not None else None
    output = []
    for brand, rows in sorted(grouped.items()):
        citation_count = sum(int(row.get("citation_occurrences") or 0) for row in rows)
        urls = {str(row.get("canonical_url") or "") for row in rows if row.get("canonical_url")}
        domains = {str(row.get("domain") or "") for row in rows if row.get("domain")}
        response_keys = {tuple(key) for row in rows for key in (row.get("trace_response_keys") or [])}
        responses = len(response_keys) if response_keys else sum(int(row.get("response_count") or 0) for row in rows)
        coverage = safe_div(responses, eligible) if eligible is not None else None
        diversity = safe_div(len(domains), citation_count)
        owned_observable = owned_domains_configured
        if owned_observable is None:
            owned_observable = any(as_bool(row.get("owned_source")) for row in rows)
        owned_rate = (
            safe_div(sum(int(row.get("citation_occurrences") or 0) for row in rows if row.get("owned_source")), citation_count) if owned_observable else None
        )
        components = [value for value in (coverage, diversity, owned_rate) if value is not None]
        output.append(
            {
                "brand_name_canonical": brand,
                "eligible_attempts": eligible,
                "citation_count": citation_count,
                "distinct_source_url_count": len(urls),
                "distinct_source_domain_count": len(domains),
                "source_coverage_rate": coverage,
                "source_diversity_score": diversity,
                "owned_source_rate": owned_rate,
                "source_score": score100(mean(components)) if citation_count else None,
                **_aggregate_trace(rows),
            }
        )
    return output


def compute_source_gaps(citation_rows: list[dict[str, Any]], target_brand: str) -> list[dict[str, Any]]:
    target_key = _brand_key(target_brand)
    target_urls = {
        str(row.get("canonical_url"))
        for row in citation_rows
        if _brand_key(str(row.get("brand_name_canonical") or "")) == target_key and row.get("canonical_url")
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in citation_rows:
        brand = str(row.get("brand_name_canonical") or "")
        if brand and _brand_key(brand) != target_key and row.get("canonical_url") not in target_urls:
            grouped[brand].append(row)
    output = []
    for competitor, rows in sorted(grouped.items()):
        denominator = sum(int(row.get("citation_occurrences") or 0) for row in citation_rows if str(row.get("brand_name_canonical") or "") == competitor)
        for row in sorted(rows, key=lambda item: str(item.get("canonical_url") or "")):
            count = int(row.get("citation_occurrences") or 0)
            output.append(
                {
                    "target_brand": target_brand,
                    "competitor_brand": competitor,
                    "canonical_url": row.get("canonical_url"),
                    "domain": row.get("domain"),
                    "source_type": row.get("source_type"),
                    "competitor_citation_occurrences": count,
                    "competitor_citation_denominator": denominator,
                    "source_gap_rate": safe_div(count, denominator),
                    "source_attribution_method": row.get("source_attribution_method"),
                    **{key: list(row.get(key) or []) for key in ("trace_job_ids", "trace_query_ids", "trace_attempt_ids")},
                }
            )
    return output


def build_citation_intelligence(
    source_rows: list[dict[str, Any]],
    brand_attempt_facts: list[dict[str, Any]] | None = None,
    *,
    owned_domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    return aggregate_citations(source_rows, brand_attempt_facts, owned_domains=owned_domains)


def _explicit_brands(row: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for field in ("attributed_brand", "anchor_brand", "brand_name_canonical"):
        if row.get(field):
            values.append(row[field])
    anchor_brands = row.get("anchor_brands")
    if isinstance(anchor_brands, list):
        values.extend(anchor_brands)
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _domain(url: str | None) -> str:
    return _normalize_domain(urlsplit(url).hostname or "") if url else ""


def _normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def _brand_key(value: str) -> str:
    return "".join(value.casefold().split())


def _aggregate_trace(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    direct = trace_fields(rows)
    for field in ("trace_job_ids", "trace_query_ids", "trace_attempt_ids", "trace_repeat_indices"):
        nested = {value for row in rows for value in (row.get(field) or []) if value not in (None, "")}
        direct[field] = sorted(set(direct[field]) | nested, key=str)
    return direct
