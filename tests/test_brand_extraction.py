from geo_monitor.brand_extraction import LLMBrandExtractor, normalize_extraction_items, normalize_extraction_items_with_quarantine, parse_canonical_map, parse_json_payload


def test_parse_json_payload_accepts_code_fence():
    data = parse_json_payload(
        """```json
{"brands":[{"brand_name_raw":"TestStudio","confidence":0.9}]}
```"""
    )
    assert data["brands"][0]["brand_name_raw"] == "TestStudio"


def test_normalize_extraction_items_adds_record_context():
    rows = normalize_extraction_items(
        [{"brand_name_raw": "TestStudio", "brand_type": "公司", "evidence": "TestStudio", "role": "mentioned", "confidence": "0.8", "sov_eligible": True}],
        {"query_id": "q001", "repeat_index": 2, "input_query": "best studios"},
    )
    assert rows[0]["query_id"] == "q001"
    assert rows[0]["repeat_index"] == 2
    assert rows[0]["input_query"] == "best studios"
    assert rows[0]["brand_name_raw"] == "TestStudio"
    assert rows[0]["brand_type"] == "公司"
    assert rows[0]["evidence"] == "TestStudio"
    assert rows[0]["role"] == "mentioned"
    assert rows[0]["confidence"] == 0.8
    assert rows[0]["is_recommended"] is False
    assert rows[0]["rank_position"] == ""
    assert rows[0]["sentiment"] == "unknown"
    assert rows[0]["mention_context"] == "answer"
    assert rows[0]["sov_eligible"] is True
    assert rows[0]["canonical_hint"] == ""


def test_normalize_extraction_items_accepts_enhanced_schema_and_dedupes():
    rows = normalize_extraction_items(
        [
            {
                "brand_name_raw": "TestStudio",
                "brand_type": "公司",
                "evidence": "1. TestStudio 表现较好",
                "role": "recommended",
                "confidence": "0.9",
                "is_recommended": "true",
                "rank_position": "1",
                "sentiment": "positive",
                "mention_context": "comparison",
                "sov_eligible": "false",
                "canonical_hint": "TestStudio",
            },
            {"brand_name_raw": " TestStudio ", "confidence": 0.5},
        ],
        {"query_id": "q001", "repeat_index": 1, "input_query": "best studios"},
    )

    assert len(rows) == 1
    assert rows[0]["is_recommended"] is True
    assert rows[0]["rank_position"] == 1
    assert rows[0]["sentiment"] == "positive"
    assert rows[0]["mention_context"] == "comparison"
    assert rows[0]["sov_eligible"] is False
    assert rows[0]["canonical_hint"] == "TestStudio"


def test_normalize_extraction_items_keeps_missing_sov_eligible_unknown():
    rows = normalize_extraction_items(
        [
            {"brand_name_raw": "TestStudio", "brand_type": "公司", "evidence": "TestStudio", "confidence": 0.8},
            {"brand_name_raw": "NullStudio", "brand_type": "公司", "evidence": "NullStudio", "confidence": 0.8, "sov_eligible": None},
            {"brand_name_raw": "BlankStudio", "brand_type": "公司", "evidence": "BlankStudio", "confidence": 0.8, "sov_eligible": ""},
            {"brand_name_raw": "UnknownStudio", "brand_type": "公司", "evidence": "UnknownStudio", "confidence": 0.8, "sov_eligible": "unknown"},
        ],
        {"query_id": "q001", "repeat_index": 1, "input_query": "best studios"},
    )

    assert [row["sov_eligible"] for row in rows] == ["", "", "", ""]


def test_parse_canonical_map_keeps_unknown_names_as_themselves():
    mapping = parse_canonical_map(
        {"canonical_brands": [{"canonical_name": "TestDesignGroup / TDG", "raw_names": ["TDG", "TestDesignGroup"]}]},
        ["TDG", "TestDesignGroup", "TestPeerEntity"],
    )
    assert mapping["TDG"] == "TestDesignGroup / TDG"
    assert mapping["TestDesignGroup"] == "TestDesignGroup / TDG"
    assert mapping["TestPeerEntity"] == "TestPeerEntity"


def test_normalize_extraction_items_quarantines_only_untraceable_rows():
    rows, quarantined = normalize_extraction_items_with_quarantine(
        [
            {"brand_name_raw": "TraceableBrand", "brand_type": "公司", "evidence": "TraceableBrand", "confidence": 0.9},
            {"brand_name_raw": "HallucinatedBrand", "brand_type": "公司", "evidence": "not in answer", "confidence": 0.9},
        ],
        {
            "query_id": "q001",
            "repeat_index": 1,
            "input_query": "best studios",
            "response_text": "TraceableBrand is mentioned.",
        },
    )

    assert [row["brand_name_raw"] for row in rows] == ["TraceableBrand"]
    assert quarantined == [
        {
            "query_id": "q001",
            "repeat_index": 1,
            "input_query": "best studios",
            "brand_name_raw": "HallucinatedBrand",
            "evidence": "not in answer",
            "reason": "untraceable_extraction_item",
        }
    ]


def test_normalize_extraction_items_requires_raw_name_traceability_not_only_evidence():
    rows, quarantined = normalize_extraction_items_with_quarantine(
        [
            {
                "brand_name_raw": "HallucinatedBrand",
                "brand_type": "公司",
                "evidence": "TraceableBrand is mentioned",
                "confidence": 0.9,
            }
        ],
        {
            "query_id": "q001",
            "repeat_index": 1,
            "input_query": "best studios",
            "response_text": "TraceableBrand is mentioned.",
        },
    )

    assert rows == []
    assert quarantined[0]["brand_name_raw"] == "HallucinatedBrand"


def test_llm_extractor_returns_quarantine_error_without_losing_valid_rows():
    class FakeClient:
        def create_response(self, payload):
            return {
                "output_text": (
                    '{"brands":['
                    '{"brand_name_raw":"TraceableBrand","brand_type":"公司","evidence":"TraceableBrand"},'
                    '{"brand_name_raw":"HallucinatedBrand","brand_type":"公司","evidence":"not in answer"}'
                    "]}"
                )
            }

    extractor = LLMBrandExtractor.__new__(LLMBrandExtractor)
    extractor.model = "test-model"
    extractor.client = FakeClient()

    rows, error = extractor.extract_record(
        {
            "query_id": "q001",
            "repeat_index": 1,
            "input_query": "best studios",
            "response_text": "TraceableBrand is mentioned.",
        }
    )

    assert [row["brand_name_raw"] for row in rows] == ["TraceableBrand"]
    assert error["type"] == "TraceabilityQuarantine"
    assert error["quarantined_rows"][0]["brand_name_raw"] == "HallucinatedBrand"
