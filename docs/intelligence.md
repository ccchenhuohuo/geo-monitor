# GEO Intelligence Contract

The intelligence layer is a deterministic, rebuildable analysis layer over the
latest-terminal facts. It does not replace raw attempts and does not infer
market share, causality, factual correctness, or commercial impact.

## Eligibility And Denominators

All intelligence outputs start from these stable facts:

| File | Grain | Primary denominator role |
|---|---|---|
| `quality_summary.csv` | Job/generation | Overall sample mode, completeness, extraction health, and conclusion strength. |
| `attempt_facts.csv` | Latest terminal query/repeat | Whether an attempt completed, is valid, and is included in statistics. |
| `query_facts.csv` | Planned query | Planned, completed, valid, and stats-included attempts for macro aggregation. |
| `brand_attempt_facts.csv` | Brand/query/repeat | Traceable, SOV-eligible brand evidence linked to one included attempt. |

Common denominator fields retain their literal meaning:

- `planned_attempts`: expected units from the frozen manifest;
- `completed_attempts`: terminal units observed, whether usable or not;
- `valid_attempts`: terminal units satisfying the record contract;
- `eligible_attempts` / `stats_included_attempts`: valid responses admitted to
  the metric under the current live/mock and quality rules;
- `sample_completeness`: completed or eligible units divided by planned units,
  as named by the containing table;
- `usable_sample_rate`: stats-included units divided by planned units.

LLM-derived recommendation and perception rows are admitted only when the
source attempt is stats-included, the evidence is non-empty and traceable, and
confidence is finite in `[0, 1]` and at least `0.5`. Rejected/quarantined rows
are counted for quality but cannot improve business scores.

## Output Catalog

Analysis writes stable CSV names directly under `result/`. A valid empty output
keeps its header so “no qualifying rows” is distinguishable from a missing
artifact.

| Area | CSVs | Grain / purpose |
|---|---|---|
| Overview | `geo_overview_scores.csv`, `visibility_summary.csv` | Five target scores, component breakdowns, denominators, and supporting visibility facts. |
| Recommendation | `recommendations.csv`, `recommendation_summary.csv`, `recommendation_by_persona.csv` | Normalized response-brand signals, brand aggregates, and persona slices. |
| Competitor | `competitor_edges.csv`, `competitor_win_loss.csv`, `competitor_replacements.csv`, `rank_gap.csv` | Target/competitor co-occurrence, decisive outcomes, substitution risk, and observed rank gaps. |
| Citation | `source_types.csv`, `brand_source_domains.csv`, `brand_source_urls.csv`, `source_gaps.csv` | Canonical source attribution, type/domain/URL summaries, and competitor-only gaps. |
| Situation | `visibility_by_seed.csv`, `visibility_by_persona.csv`, `visibility_by_intent.csv`, `visibility_by_scenario.csv` | Equal-weight query macro performance plus diagnostic micro rates. |
| Perception | `perception_claims.csv`, `perception_strengths.csv`, `perception_weaknesses.csv`, `perception_pricing.csv`, `perception_audience_fit.csv` | Confidence/evidence-gated claim clusters. |
| Trend | `trend_deltas.csv`, `trend_drift.csv`, `trend_volatility.csv` | Adjacent-run changes, top-k set drift, between-run score dispersion, and within-run brand-presence dispersion computed from eligible attempt-level 0/1 facts. |
| Opportunity | `opportunity_query_gaps.csv`, `opportunity_persona_gaps.csv`, `opportunity_source_gaps.csv`, `opportunity_messaging_gaps.csv` | Positive rule-based gap rankings with factor breakdown and evidence trace. |

## Five Overview Scores

Scores are on a `0..100` scale. Every score row preserves its component values,
weights, observed denominators, and trace IDs. Business scores are not silently
multiplied by quality; `quality_score` is an independent guardrail that readers
must display beside them.

### Visibility

The default visibility score is a weighted sum of normalized `0..1`
components:

| Component | Weight |
|---|---:|
| Response mention rate | 0.30 |
| Query coverage | 0.25 |
| Prominence | 0.20 |
| Rank score | 0.15 |
| SOV response share | 0.10 |

If no direct prominence component exists, prominence is the mean of observed
top-3 rate and rank score. Rank score is `1 / average_rank` clamped to `[0, 1]`.
Missing visibility signals are conservative zeros.

### Recommendation

`recommendation_score` is the mean of the available recommendation conversion
rate and normalized weighted recommendation score. If neither is observable,
the score is N/A.

### Competitor

`competitor_score` is the mean of available target win rate and
`1 - replacement_risk`. If neither comparison is observable, it is N/A.

### Source

`source_score` averages available source coverage, source diversity, and owned
source rate. When no citation/source observation exists, the value is N/A—not
zero. This distinction prevents a provider that exposes no source trace from
looking equivalent to an observed answer that cites no target source.

### Quality

`quality_score` uses available components and renormalizes their present
weights:

| Component | Weight |
|---|---:|
| Usable sample rate | 0.45 |
| Sample completeness | 0.25 |
| Confidence health | 0.15 |
| Extraction success rate (`1 - extraction_error_rate`) | 0.15 |

N/A is serialized as an empty CSV value and remains SQL `NULL` in DuckDB. Do
not coalesce N/A to zero for ranking or provider comparison.

## Recommendation Intelligence

At most one strongest eligible row is retained for each response and canonical
brand. The explicit normalized types and weights are:

| Type | Weight |
|---|---:|
| `top_pick` | 1.00 |
| `recommended` | 0.80 |
| `best_for_use_case` | 0.75 |
| `strong_alternative` | 0.65 |
| `budget_pick` | 0.65 |
| `premium_pick` | 0.65 |
| `conditional` | 0.35 |
| `mentioned_only` / `not_mentioned` | 0.00 |
| `warning` | -0.50 |
| `discouraged` | -1.00 |

The summary exposes the type distribution, top-pick rate/share, conditional,
warning, and discouraged rates, mention-to-recommendation conversion, and
weighted strength. The denominator for conversion is eligible response-brand
mentions, not every planned attempt; an over-all-sample rate must use the
explicit eligible-attempt denominator instead.

## Competitor Intelligence

Competitor rows are discovered from responses rather than a bundled list. For
each target/competitor pair:

1. recommendation weight decides the response-level outcome;
2. observed rank breaks only an equal recommendation weight;
3. missing or equal rank remains a tie;
4. ties are reported but excluded from the win/loss denominator.

Replacement risk counts eligible attempts where a competitor receives a
positive recommendation and the target is absent. Rank-gap metrics use only
attempts where both ranks are observed. Co-occurrence, union, decisive,
replacement, top-pick, and rank-observation denominators are retained
separately.

## Citation Intelligence

HTTP(S) URLs are canonicalized for counting: host names are normalized through
IDNA, `www.` and default ports are removed, fragments and credentials are
dropped, tracking parameters such as `utm_*`, `gclid`, and `fbclid` are removed,
and remaining query parameters are sorted. Canonicalization is for aggregation;
the engine does not fetch the URL.

Attribution uses an explicit anchor/brand association first. Without an anchor,
answer-level brand co-occurrence is retained as an inferred
`answer_cooccurrence` attribution, never relabeled as direct evidence.
Unattributed sources remain explicit. Source types include official website,
ecommerce, content community, forum/Q&A, media/review, wiki/knowledge, video,
social, blog, and unknown.

Source gaps list competitor-cited URLs not observed for the target, together
with the competitor citation denominator and attribution method. They are
research leads, not proof that acquiring a citation will change model output.

## Situation And Macro-By-Query Aggregation

Seed, persona, intent, and scenario tables use macro-by-query as the primary
view: calculate the target rate within each planned query, then give each query
equal weight. Queries with no eligible denominator remain N/A rather than being
invented as success or failure. The tables also expose the micro rate
(`target mentions / eligible attempts`) for diagnostics.

Macro is the comparison default because additional repeats on one query must
not silently give that query more business weight. Persona gaps compare a
persona's macro rate with the same run's overall query macro rate; sample counts
and quality travel with every slice.

## Perception Intelligence

Perception records are grouped by canonical brand, claim type, and normalized
claim text. Supported claim types are claim, strength, weakness, pricing,
audience fit, and persona alignment. Each aggregate carries response/query
counts, eligible-attempt denominator, average confidence, evidence samples,
rejected-row count, and trace IDs. A repeated phrase is an observed response
pattern, not an independently verified product fact.

## Trend Intelligence

- Deltas compare adjacent chronologically ordered runs for the same entity and
  retain the prior value as the relative-change denominator.
- Drift compares top-k sets with Jaccard similarity/distance and retains both
  membership lists.
- Volatility reports population dispersion within runs and between run means.
  Between-run volatility is N/A with fewer than two runs; within-run volatility
  is N/A without a run containing at least two observations.

Compare only compatible cohorts: same frozen query manifest, repeats, effective
sampling request, adapter/API family/source grain, and analysis fingerprint.
Quality or source non-comparability downgrades conclusions even when arithmetic
can still be computed.

## Rule-Based Opportunities

Opportunity scores are transparent multiplicative rules, not another LLM
judgment:

- query gap = competitor visibility × competitor recommendation strength ×
  query quality;
- persona gap = persona visibility gap × competitor strength × persona quality;
- source gap = competitor source coverage × target source gap × source quality;
- messaging gap = competitor claim strength × audience relevance × message
  quality.

Optional explicit query/segment weight is then applied. A row is emitted only
when all required factors are observed and the resulting score is positive.
Every row contains `factor_breakdown`, observed/required factor counts, and
query/attempt/source trace fields. Rankings prioritize investigation; they do
not prescribe spend or guarantee uplift.

## DuckDB

`geo-monitor db build` verifies that analysis artifacts belong to the current
live generation before ingestion. Core facts have typed tables. Intelligence
CSVs are registered in `intelligence_artifacts`, preserved losslessly as JSON in
`intelligence_rows`, and exposed as a view with the same stem as each CSV.
Columns that can be inferred safely become Boolean, integer, double, percent,
or timestamp; `row_json` remains available for nested breakdowns and forward
compatibility.

Examples:

```sql
select *
from geo_overview_scores
where brand_name_canonical = 'ExampleBrand';

select segment_value as persona,
       visibility_rate_macro_by_query, visibility_rate_micro,
       eligible_attempts, sample_completeness
from visibility_by_persona
order by visibility_rate_macro_by_query desc nulls last;

select competitor_brand, target_win_rate, replacement_risk,
       win_loss_denominator, replacement_denominator
from competitor_edges
order by replacement_risk desc nulls last;
```

Use the restricted `geo-monitor db query` command for local read-only SQL.
Rebuild the database from retained bundles whenever schema or metric contracts
change.
