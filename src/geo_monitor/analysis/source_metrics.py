"""Aggregate parsed source coverage and ordering metrics."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any
from urllib.parse import urlparse

from .fact_utils import as_positive_int, pct


def compute_source_stats(records: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    domain_occ: Counter = Counter()
    domain_responses: dict[str, set[str]] = defaultdict(set)
    domain_queries: dict[str, set[str]] = defaultdict(set)
    domain_ranks: dict[str, list[int]] = defaultdict(list)
    domain_urls: dict[str, Counter] = defaultdict(Counter)
    url_occ: Counter = Counter()
    url_meta: dict[str, dict[str, str]] = {}
    by_query_domain: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"repeats": set(), "occ": 0, "ranks": [], "urls": Counter()})

    for idx, record in enumerate(records):
        qid = str(record.get("query_id"))
        rep = int(record.get("repeat_index") or 1)
        response_key = f"{qid}#{rep}#{idx}"
        seen_sources: set[tuple[str, str]] = set()
        for source in record.get("sources", []) or []:
            if hasattr(source, "model_dump"):
                source = source.model_dump(mode="json")
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "")
            domain = _normalize_source_domain(str(source.get("domain") or ""), url)
            source_key = (domain, url)
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            title = str(source.get("title") or "")
            rank = as_positive_int(source.get("rank"))
            domain_occ[domain] += 1
            domain_responses[domain].add(response_key)
            domain_queries[domain].add(qid)
            if rank is not None:
                domain_ranks[domain].append(rank)
            if url:
                domain_urls[domain][url] += 1
                url_occ[url] += 1
                url_meta[url] = {"domain": domain, "title": title}
            cell = by_query_domain[(qid, domain)]
            cell["repeats"].add(rep)
            cell["occ"] += 1
            if rank is not None:
                cell["ranks"].append(rank)
            if url:
                cell["urls"][url] += 1

    total_records = len(records) or 1
    query_count = int(manifest.get("query_count") or len({record.get("query_id") for record in records}) or 1)
    domain_rows = []
    for domain, count in domain_occ.most_common():
        domain_rows.append(
            {
                "domain": domain,
                "parsed_source_occurrences": count,
                "response_coverage": len(domain_responses[domain]),
                "response_coverage_rate": pct(len(domain_responses[domain]) / total_records),
                "query_coverage": len(domain_queries[domain]),
                "query_coverage_rate": pct(len(domain_queries[domain]) / query_count),
                "avg_source_order": round(statistics.mean(domain_ranks[domain]), 2) if domain_ranks[domain] else "",
                "best_source_order": min(domain_ranks[domain]) if domain_ranks[domain] else "",
                "distinct_source_url_count": len(domain_urls[domain]),
                "top_urls": " | ".join(url for url, _ in domain_urls[domain].most_common(3)),
            }
        )
    url_rows = [
        {
            "url": url,
            "domain": url_meta[url]["domain"],
            "title": url_meta[url]["title"],
            "parsed_source_occurrences": count,
        }
        for url, count in url_occ.most_common()
    ]
    by_query_rows = []
    for (qid, domain), cell in sorted(by_query_domain.items()):
        qrecs = [record for record in records if record.get("query_id") == qid]
        by_query_rows.append(
            {
                "query_id": qid,
                "domain": domain,
                "repeat_coverage": len(cell["repeats"]),
                "repeat_coverage_rate": pct(len(cell["repeats"]) / (len(qrecs) or 1)),
                "parsed_source_occurrences": cell["occ"],
                "avg_source_order": round(statistics.mean(cell["ranks"]), 2) if cell["ranks"] else "",
                "distinct_source_url_count": len(cell["urls"]),
                "top_urls": " | ".join(url for url, _ in cell["urls"].most_common(3)),
            }
        )
    return {"source_domains": domain_rows, "source_urls": url_rows, "source_by_query": by_query_rows}


def _normalize_source_domain(domain: str, url: str = "") -> str:
    candidate = (domain or "").strip().lower()
    if not candidate and url:
        candidate = urlparse(url).netloc.lower()
    if ":" in candidate:
        host, _, port = candidate.partition(":")
        if port in {"80", "443"}:
            candidate = host
    if candidate.startswith("www."):
        candidate = candidate[4:]
    return candidate or "unknown"
