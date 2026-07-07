from __future__ import annotations

from typing import Any

from ..config import Settings
from ..schemas import QueryRecord
from .base import AdapterCapabilities, BaseAdapter, ProviderRequest


class QwenChatEnableSearchAdapter(BaseAdapter):
    name = "qwen_chat_enable_search"
    provider = "qwen"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        api_family="chat_completions",
        supported_model_patterns=("qwen-*", "qwen3*", "qwen-plus*", "qwen-max*", "qwen-turbo*"),
        supports_forced_search=True,
        supports_sources="partial",
        supports_search_trace=False,
        source_grain="unknown",
    )
    allowed_options = {"forced_search", "search_options"}

    def build_request(
        self,
        query_record: QueryRecord,
        sampling_profile: dict[str, Any],
        settings: Settings,
        adapter_options: dict[str, Any],
    ) -> ProviderRequest:
        self.validate_options(adapter_options)
        extra_body: dict[str, Any] = {"enable_search": True}
        search_options = dict(adapter_options.get("search_options") or {})
        forced_search = adapter_options.get("forced_search")
        if forced_search is None:
            forced_search = bool(sampling_profile.get("web_search_required", True))
        if forced_search:
            search_options["forced_search"] = True
        if search_options:
            extra_body["search_options"] = search_options
        payload = {
            "model": sampling_profile["model"],
            "messages": [{"role": "user", "content": query_record.query}],
            "extra_body": extra_body,
        }
        payload_basis = {
            "model": payload["model"],
            "messages": payload["messages"],
            "extra_body": extra_body,
        }
        return ProviderRequest(
            sampling_profile=sampling_profile,
            payload=payload,
            request_fingerprint_basis=self._fingerprint_basis(query_record, sampling_profile, payload_basis),
        )

    def send(self, client: Any, request: ProviderRequest) -> Any:
        return client.chat.completions.create(**request.payload)

