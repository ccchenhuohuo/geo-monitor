"""Build the stable report document from analysis results."""

from __future__ import annotations

from typing import Any

from .report_model import ReportModel, ReportSection, bullets, paragraph, table


def build_report_model(summary: dict[str, Any]) -> ReportModel:
    """Create the single semantic source consumed by every report renderer."""

    quality = summary.get("data_quality") if isinstance(summary.get("data_quality"), dict) else {}
    intelligence = summary.get("intelligence") if isinstance(summary.get("intelligence"), dict) else {}
    sections = (
        _executive_section(summary, quality),
        _configuration_section(summary),
        _quality_section(summary, quality),
        _visibility_section(summary),
        _target_section(summary),
        _intelligence_section(intelligence),
        _sources_section(summary, intelligence),
        _situation_section(intelligence),
        _perception_section(intelligence),
        _trend_opportunity_section(intelligence),
        _query_section(summary),
        _methodology_section(summary),
    )
    return ReportModel(
        title=str(summary.get("title") or "GEO 分析报告"),
        job_id=str(summary.get("job_id") or ""),
        generated_at=str(summary.get("generated_at") or summary.get("completed_at") or ""),
        sample_mode=str(summary.get("sample_mode") or "live"),
        conclusion_strength=str(quality.get("conclusion_strength") or summary.get("job_conclusion_strength") or "observational"),
        sections=sections,
        metadata={
            "target_brand": summary.get("target_brand") or "",
            "industry": summary.get("industry") or "",
            "market": summary.get("market") or "",
            "expected_queries": summary.get("expected_queries"),
            "expected_repeats": summary.get("expected_repeats"),
            "intelligence_schema_version": summary.get("intelligence_schema_version") or "",
        },
    )


def _executive_section(summary: dict[str, Any], quality: dict[str, Any]) -> ReportSection:
    items: list[str] = []
    if summary.get("sample_mode") == "mock":
        items.append("当前报告基于 mock 样本，只用于验收交付链路，不构成业务结论。")
    if quality.get("conclusion_strength") == "observational":
        items.append("当前样本完整性或抽取质量不足，结果应作为观察线索，不宜作为强排名结论。")
    if int(summary.get("success_record_count") or 0) == 0:
        items.append("当前没有可分析的成功样本，不输出品牌表现结论。")
    elif not summary.get("brand_summary"):
        items.append("当前样本中未抽取到明确品牌、公司或机构名称。")
    else:
        top = summary["brand_summary"][0]
        items.append(
            f"品牌命中事件份额最高的是 {top.get('brand_name_canonical', '')}，"
            f"份额 {top.get('sov_event_share', 'N/A')}，Query 覆盖率 {top.get('query_coverage_rate', 'N/A')}。"
        )
        diagnosis = summary.get("target_diagnosis") or {}
        if diagnosis.get("target_detected"):
            target_share = diagnosis.get("target_sov_event_share", diagnosis.get("target_sov_response_share", "N/A"))
            items.append(
                f"目标品牌 {summary.get('target_brand', '')} 的品牌命中事件份额为 {target_share}，样本内排序第 {diagnosis.get('target_rank_by_sov', 'N/A')}。"
            )
        else:
            items.append(f"目标品牌 {summary.get('target_brand', '')} 在当前抽取口径下未命中，需要复核别名与原始回答。")
    unstable = [row for row in summary.get("query_stability", []) if row.get("brand_set_jaccard_avg") not in {"", 1, 1.0}]
    if unstable:
        items.append(f"{len(unstable)} 个 Query 在重复采样中存在品牌集合波动，建议人工复核。")
    return ReportSection(key="executive_summary", title="Executive Summary", blocks=(bullets(*items),))


def _configuration_section(summary: dict[str, Any]) -> ReportSection:
    rows = [
        ("目标品牌", summary.get("target_brand")),
        ("行业", summary.get("industry")),
        ("市场", summary.get("market")),
        ("Query 数", summary.get("expected_queries")),
        ("每 Query 重复次数", summary.get("expected_repeats")),
        ("成功回答数", summary.get("success_record_count")),
        ("抽取品牌提及数", summary.get("extracted_mention_count")),
        ("抽取异常回答数", summary.get("extraction_error_record_count", summary.get("extraction_error_count"))),
        ("样本模式", summary.get("sample_mode", "live")),
    ]
    return ReportSection(key="configuration", title="任务配置", blocks=(table("configuration", ["项目", "值"], rows),))


def _quality_section(summary: dict[str, Any], quality: dict[str, Any]) -> ReportSection:
    rows = [
        ("计划采样单元", quality.get("planned_units", summary.get("expected_units"))),
        ("可分析样本数", quality.get("analysis_record_count", summary.get("success_record_count"))),
        ("缺失采样单元", len(quality.get("missing_units", []))),
        ("额外采样单元", len(quality.get("extra_units", []))),
        ("重复采样单元", len(quality.get("duplicate_units", []))),
        ("请求契约不一致", len(quality.get("contract_mismatches", []))),
        ("Raw 读取错误", len(quality.get("raw_read_errors", []))),
        ("追溯隔离项数", quality.get("traceability_quarantine_count", 0)),
        ("抽取错误率", quality.get("extraction_error_rate", summary.get("extraction_error_rate", "0.0%"))),
        ("结论强度", quality.get("conclusion_strength", "observational")),
    ]
    return ReportSection(
        key="data_quality",
        title="Data Quality",
        blocks=(
            paragraph("质量分只表示样本可信度，不混入业务表现；partial sample 下业务指标保留，但必须降级解释。"),
            table("data_quality", ["项目", "值"], rows),
        ),
    )


def _visibility_section(summary: dict[str, Any]) -> ReportSection:
    rows = [
        (
            row.get("sov_rank"),
            row.get("brand_name_canonical"),
            row.get("sov_event_share"),
            row.get("response_mention_rate"),
            row.get("query_coverage_rate"),
            "是" if row.get("is_target_brand") else "",
        )
        for row in summary.get("brand_summary", [])[:20]
    ]
    blocks = [paragraph("SOV 是当前 LLM 回答样本内的品牌命中事件份额，不代表市场份额。")]
    if rows:
        blocks.append(table("brand_visibility", ["排名", "品牌/机构", "SOV", "回答提及率", "Query 覆盖率", "目标品牌"], rows))
    else:
        blocks.append(paragraph("暂无可展示的品牌发现结果。"))
    return ReportSection(key="visibility", title="Brand Visibility / SOV", blocks=tuple(blocks))


def _target_section(summary: dict[str, Any]) -> ReportSection:
    diagnosis = summary.get("target_diagnosis") or {}
    blocks = []
    if int(summary.get("success_record_count") or 0) == 0:
        blocks.append(paragraph("当前没有成功样本，不能判断目标品牌是否缺失或弱势。"))
    elif diagnosis.get("target_detected"):
        blocks.append(
            table(
                "target_diagnosis",
                ["指标", "值"],
                [
                    ("目标品牌命中事件份额", diagnosis.get("target_sov_event_share", diagnosis.get("target_sov_response_share"))),
                    ("样本内份额排序", diagnosis.get("target_rank_by_sov")),
                    ("与第一名差距", diagnosis.get("target_sov_gap_to_leader")),
                    ("与 Top3 平均差距", diagnosis.get("target_sov_gap_to_top3_avg")),
                    ("回答提及率", diagnosis.get("target_response_mention_rate")),
                    ("提及后推荐率", diagnosis.get("target_recommended_rate_when_mentioned", diagnosis.get("target_recommended_rate"))),
                    ("全样本推荐率", diagnosis.get("target_recommended_rate_over_success", "0.0%")),
                    ("Query 覆盖率", diagnosis.get("target_query_coverage_rate")),
                ],
            )
        )
    else:
        blocks.append(paragraph("目标品牌未命中。优先检查 Query 场景、target_aliases 和原始回答中的未识别别名。"))
    missing = diagnosis.get("missing_queries") or []
    if missing:
        blocks.append(bullets(*(f"{item.get('query_id', '')}: {item.get('query', '')}" for item in missing[:10])))
    return ReportSection(key="target_diagnosis", title="Target Brand Diagnosis", blocks=tuple(blocks))


def _intelligence_section(intelligence: dict[str, Any]) -> ReportSection:
    blocks = [paragraph("五项得分相互独立；Source Score 无可观测引用时为 N/A，Quality Score 不参与业务得分。")]
    overview = intelligence.get("geo_overview_scores") or []
    if overview:
        blocks.append(
            table(
                "overview_scores",
                ["品牌", "Visibility", "Recommendation", "Competitor", "Source", "Quality"],
                [
                    (
                        row.get("brand_name_canonical"),
                        _metric(row.get("visibility_score")),
                        _metric(row.get("recommendation_score")),
                        _metric(row.get("competitor_score")),
                        _metric(row.get("source_score")),
                        _metric(row.get("quality_score")),
                    )
                    for row in overview[:15]
                ],
            )
        )
    recommendations = intelligence.get("recommendation_summary") or []
    if recommendations:
        blocks.append(
            table(
                "recommendations",
                ["品牌", "观测回答", "推荐转化率", "Top Pick 率", "加权推荐分"],
                [
                    (
                        row.get("brand_name_canonical"),
                        row.get("recommendation_denominator", 0),
                        _ratio(row.get("recommendation_conversion")),
                        _ratio(row.get("top_pick_rate")),
                        _metric(row.get("weighted_recommendation_score")),
                    )
                    for row in recommendations[:15]
                ],
            )
        )
    competitors = intelligence.get("competitor_edges") or []
    if competitors:
        blocks.append(
            table(
                "competitors",
                ["竞品", "共现", "目标胜", "竞品胜", "目标胜率", "替代风险"],
                [
                    (
                        row.get("competitor_brand"),
                        row.get("co_occurrence_count", 0),
                        row.get("target_wins", 0),
                        row.get("competitor_wins", 0),
                        _ratio(row.get("target_win_rate")),
                        _ratio(row.get("replacement_risk")),
                    )
                    for row in competitors[:15]
                ],
            )
        )
    return ReportSection(key="intelligence", title="GEO Intelligence Layer", blocks=tuple(blocks))


def _sources_section(summary: dict[str, Any], intelligence: dict[str, Any]) -> ReportSection:
    rows = [
        (
            row.get("domain"),
            row.get("parsed_source_occurrences", row.get("citation_occurrences")),
            row.get("distinct_source_url_count", ""),
            row.get("response_coverage_rate"),
            row.get("query_coverage_rate"),
            row.get("avg_source_order", row.get("avg_rank")),
        )
        for row in summary.get("source_domains", [])[:15]
    ]
    blocks = []
    if rows:
        blocks.append(table("source_domains", ["来源域名", "引用次数", "去重 URL", "回答覆盖率", "Query 覆盖率", "平均序号"], rows))
    else:
        blocks.append(paragraph("当前样本没有解析到来源引用。"))
    gaps = intelligence.get("source_gaps") or []
    if gaps:
        blocks.append(
            table(
                "source_gaps",
                ["竞品", "域名", "来源类型", "Gap Rate"],
                [(row.get("competitor_brand"), row.get("domain"), row.get("source_type"), _ratio(row.get("source_gap_rate"))) for row in gaps[:15]],
            )
        )
    return ReportSection(key="sources", title="Source & Citation Opportunities", blocks=tuple(blocks))


def _situation_section(intelligence: dict[str, Any]) -> ReportSection:
    rows = intelligence.get("visibility_by_persona") or []
    blocks = [paragraph("主口径为 Query 等权宏平均；Micro 仅作为采样层诊断。")]
    if rows:
        blocks.append(
            table(
                "persona_visibility",
                ["Persona", "Query 数", "Macro Visibility", "Micro Visibility", "Persona Gap", "Quality"],
                [
                    (
                        row.get("segment_value") or "(未标注)",
                        row.get("query_count", 0),
                        _ratio(row.get("visibility_rate_macro_by_query")),
                        _ratio(row.get("visibility_rate_micro")),
                        _ratio(row.get("persona_gap")),
                        _metric(row.get("quality_score")),
                    )
                    for row in rows[:15]
                ],
            )
        )
    return ReportSection(key="situation", title="Situation Intelligence", blocks=tuple(blocks))


def _perception_section(intelligence: dict[str, Any]) -> ReportSection:
    rows = [
        *intelligence.get("perception_strengths", []),
        *intelligence.get("perception_weaknesses", []),
        *intelligence.get("perception_pricing", []),
        *intelligence.get("perception_audience_fit", []),
    ]
    blocks = []
    if rows:
        blocks.append(
            table(
                "perception",
                ["品牌", "类型", "事实", "回答率", "平均置信度"],
                [
                    (
                        row.get("brand_name_canonical"),
                        row.get("claim_type"),
                        row.get("representative_claim_text"),
                        _ratio(row.get("response_rate")),
                        _metric(row.get("avg_confidence")),
                    )
                    for row in rows[:15]
                ],
            )
        )
    else:
        blocks.append(paragraph("当前没有满足证据与置信度门槛的品牌感知事实。"))
    return ReportSection(key="perception", title="Perception Intelligence", blocks=tuple(blocks))


def _trend_opportunity_section(intelligence: dict[str, Any]) -> ReportSection:
    blocks = []
    opportunities = [
        *intelligence.get("opportunity_query_gaps", []),
        *intelligence.get("opportunity_persona_gaps", []),
        *intelligence.get("opportunity_source_gaps", []),
        *intelligence.get("opportunity_messaging_gaps", []),
    ]
    if opportunities:
        ranked = sorted(opportunities, key=lambda item: -float(item.get("opportunity_score") or 0))[:20]
        blocks.append(
            table(
                "opportunities",
                ["类型", "对象", "得分"],
                [
                    (
                        row.get("opportunity_type"),
                        row.get("query_id") or row.get("persona") or row.get("domain") or row.get("claim_canonical") or row.get("competitor_brand"),
                        _metric(row.get("opportunity_score")),
                    )
                    for row in ranked
                ],
                note="机会分来自确定性规则，不是大模型自由生成的内容建议。",
            )
        )
    trends = intelligence.get("trend_deltas") or []
    if trends:
        blocks.append(
            table(
                "trends",
                ["品牌", "指标", "基线", "当前", "Delta"],
                [
                    (
                        row.get("brand_name_canonical"),
                        row.get("metric"),
                        _metric(row.get("baseline_value")),
                        _metric(row.get("current_value")),
                        _metric(row.get("absolute_delta")),
                    )
                    for row in trends[:20]
                ],
            )
        )
    if not blocks:
        blocks.append(paragraph("当前没有足够的可比历史或规则化机会事实。"))
    return ReportSection(key="trends_opportunities", title="Trend & Opportunity Intelligence", blocks=tuple(blocks))


def _query_section(summary: dict[str, Any]) -> ReportSection:
    blocks = []
    rows = summary.get("brand_by_query") or []
    if rows:
        blocks.append(
            table(
                "query_findings",
                ["Query ID", "品牌/机构", "提及回答数", "Query 内提及率", "提及后推荐率"],
                [
                    (
                        row.get("query_id"),
                        row.get("brand_name_canonical"),
                        row.get("responses_mentioned"),
                        row.get("mention_rate_within_query"),
                        row.get("recommended_rate_when_mentioned_within_query", row.get("recommended_rate_within_query")),
                    )
                    for row in rows[:30]
                ],
            )
        )
    stability = summary.get("query_stability") or []
    if stability:
        blocks.append(
            table(
                "query_stability",
                ["Query ID", "成功重复数", "样本充足", "品牌集合 Jaccard", "Top Brands"],
                [
                    (
                        row.get("query_id"),
                        row.get("successful_repeats"),
                        row.get("sample_sufficient"),
                        row.get("brand_set_jaccard_avg"),
                        row.get("top_brands"),
                    )
                    for row in stability[:30]
                ],
            )
        )
    if not blocks:
        blocks.append(paragraph("暂无 Query 级品牌命中数据。"))
    return ReportSection(key="query_findings", title="Query-Level Findings", blocks=tuple(blocks))


def _methodology_section(summary: dict[str, Any]) -> ReportSection:
    files = []
    for key, value in (summary.get("analysis_files") or {}).items():
        files.append(f"分析附件 {key}: {value}")
    for key, value in (summary.get("aggregate_files") or {}).items():
        files.append(f"跨 Run 聚合 {key}: {value}")
    return ReportSection(
        key="methodology",
        title="Methodology & Evidence",
        blocks=(
            bullets(
                "真实采样请求只发送 Query 文本；目标品牌、行业和市场仅用于任务记录与后处理。",
                "品牌发现来自对 response_text 的开放式实体抽取，不依赖预置竞品列表。",
                "SOV 只表示当前回答样本内的品牌命中事件份额，不等同于市场份额或产品真实排名。",
                "推荐、排名、情感和品牌感知均受 evidence、confidence 与 traceability 门槛约束。",
                "Raw attempts 是事实源；CSV、报告、DuckDB 和 HTML 都是可重建派生物。",
            ),
            bullets(*files) if files else paragraph("详细事实与证据索引保存在当前 Job workspace。"),
        ),
    )


def _metric(value: Any) -> str:
    if value in (None, ""):
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _ratio(value: Any) -> str:
    if value in (None, ""):
        return "N/A"
    text = str(value).strip()
    if text.endswith("%"):
        return text
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    if abs(number) > 1:
        number /= 100.0
    return f"{number * 100:.1f}%"
