from geo_monitor.response_parser import extract_output_text, parse_response, response_to_dict


def test_parse_mock_response_fixture():
    payload = {
        "output_text": "hello",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello", "annotations": [{"type": "url_citation", "title": "Example", "url": "https://example.com", "snippet": "snippet"}]}]}],
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    }
    text, sources, usage, raw = parse_response(payload)
    assert text == "hello"
    assert len(sources) == 1
    assert usage["total_tokens"] == 3
    assert raw["output_text"] == "hello"


def test_parse_response_normalizes_source_domains():
    payload = {
        "output_text": "hello",
        "output": [
            {"type": "url_citation", "title": "A", "url": "https://WWW.Example.com:443/a"},
            {"type": "url_citation", "title": "B", "url": "http://www.Example.com:80/b"},
        ],
    }

    _, sources, _, _ = parse_response(payload)

    assert {source.domain for source in sources} == {"example.com"}


def test_parse_response_reads_nested_sources_with_source_context():
    payload = {
        "output_text": "hello",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "hello",
                        "annotations": [
                            {"type": "url_citation", "title": "A", "url": "https://example.com/a"},
                        ],
                    }
                ],
            }
        ],
        "web_search": {"sources": [{"title": "B", "url": "https://docs.example.com/b"}]},
    }

    _, sources, _, _ = parse_response(payload)

    assert [source.url for source in sources] == ["https://example.com/a", "https://docs.example.com/b"]


def test_parse_response_ignores_url_fields_without_source_context():
    payload = {
        "output_text": "hello",
        "metadata": {"title": "Account", "url": "https://internal.example.com/customer"},
    }

    _, sources, _, _ = parse_response(payload)

    assert sources == []


def test_parse_response_does_not_leak_source_context_to_siblings():
    payload = {
        "output_text": "hello",
        "web_search": {"query": "example", "sources": [{"title": "Doc", "url": "https://docs.example.com/a"}]},
        "metadata": {"title": "Account", "url": "https://internal.example.com/customer"},
    }

    _, sources, _, _ = parse_response(payload)

    assert [source.url for source in sources] == ["https://docs.example.com/a"]


def test_extract_output_text_returns_none_when_empty():
    payload = {"status": "completed", "output_text": "   ", "output": []}
    assert extract_output_text(payload) is None


def test_extract_output_text_ignores_response_source_snippets():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Answer mentions TargetOnly"}],
            },
            {
                "type": "web_search_call",
                "action": {
                    "sources": [
                        {
                            "title": "Competitor page",
                            "url": "https://example.com/a",
                            "snippet": "CompetitorBrand appears only in source snippet",
                        }
                    ]
                },
            },
        ],
    }

    assert extract_output_text(payload) == "Answer mentions TargetOnly"


def test_extract_output_text_ignores_chat_tool_content():
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "Answer mentions TargetOnly"}}],
        "tool_results": [{"content": "CompetitorBrand appears only in tool content"}],
    }

    assert extract_output_text(payload) == "Answer mentions TargetOnly"


def test_parse_response_preserves_incomplete_details():
    payload = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output_text": ""}
    text, sources, usage, raw = parse_response(payload)
    assert text is None
    assert raw["incomplete_details"]["reason"] == "max_output_tokens"


def test_response_to_dict_accepts_plain_dict():
    payload = {"status": "completed", "output_text": "ok"}
    assert response_to_dict(payload) == payload
