from __future__ import annotations

from typing import Any

from .schemas import QueryRecord


def build_mock_response(query_record: QueryRecord) -> dict[str, Any]:
    return {
        "id": f"mock-{query_record.query_id}",
        "object": "response",
        "output_text": f"这是针对 query {query_record.query_id} 的 mock 联网回答：{query_record.query}",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": f"这是针对 query {query_record.query_id} 的 mock 联网回答：{query_record.query}",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Mock Source",
                                "url": "https://example.com/mock-source",
                                "snippet": "这是用于验证 source 解析的模拟来源。",
                            }
                        ],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 50,
            "total_tokens": 70,
        },
    }
