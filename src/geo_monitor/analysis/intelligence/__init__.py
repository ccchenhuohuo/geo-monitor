"""Pure-function GEO intelligence layer.

All public aggregators accept and return ``list[dict]``.  They do not read or
write files and do not depend on pipeline or DuckDB internals.
"""

from .citation import (
    aggregate_citations,
    build_citation_intelligence,
    canonicalize_url,
    classify_source_type,
    compute_source_gaps,
    summarize_citations,
)
from .competitor import build_competitor_edges, compute_competitor_intelligence
from .opportunities import (
    build_messaging_opportunities,
    build_opportunity_tables,
    build_persona_opportunities,
    build_query_opportunities,
    build_source_opportunities,
)
from .orchestration import INTELLIGENCE_BASE_FIELDS, INTELLIGENCE_TABLE_NAMES, build_intelligence_outputs
from .overview import build_overview_scores, compute_overview_scores
from .perception import aggregate_perception, build_perception_intelligence, perception_quality_flags
from .recommendation import (
    RECOMMENDATION_WEIGHTS,
    aggregate_recommendations,
    build_recommendation_intelligence,
    classify_recommendation_type,
    recommendation_weight,
    strongest_attempt_brand_rows,
)
from .situation import aggregate_situations, build_situation_intelligence
from .trends import (
    build_trend_intelligence,
    compute_presence_volatility,
    compute_run_deltas,
    compute_topk_drift,
    compute_volatility,
)

INTELLIGENCE_SCHEMA_VERSION = "geo-intelligence-v1"

__all__ = [
    "INTELLIGENCE_SCHEMA_VERSION",
    "INTELLIGENCE_BASE_FIELDS",
    "INTELLIGENCE_TABLE_NAMES",
    "RECOMMENDATION_WEIGHTS",
    "aggregate_citations",
    "aggregate_perception",
    "aggregate_recommendations",
    "aggregate_situations",
    "build_citation_intelligence",
    "build_competitor_edges",
    "build_messaging_opportunities",
    "build_intelligence_outputs",
    "build_opportunity_tables",
    "build_overview_scores",
    "build_perception_intelligence",
    "build_persona_opportunities",
    "build_query_opportunities",
    "build_recommendation_intelligence",
    "build_situation_intelligence",
    "build_source_opportunities",
    "build_trend_intelligence",
    "canonicalize_url",
    "classify_recommendation_type",
    "classify_source_type",
    "compute_competitor_intelligence",
    "compute_overview_scores",
    "compute_presence_volatility",
    "compute_run_deltas",
    "compute_source_gaps",
    "compute_topk_drift",
    "compute_volatility",
    "perception_quality_flags",
    "recommendation_weight",
    "strongest_attempt_brand_rows",
    "summarize_citations",
]
