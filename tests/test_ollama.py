from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import write_config

from sloth_valet.ollama import (
    OllamaClient,
    OllamaHTTPError,
    OllamaResponseError,
    OllamaTimeout,
)


def _client(tmp_path: Path, handler: httpx.MockTransport) -> OllamaClient:
    return OllamaClient(write_config(tmp_path).load(), transport=handler)


def test_health_check_uses_tags_endpoint(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"models": []}, request=request)

    with _client(tmp_path, httpx.MockTransport(handler)) as client:
        assert client.check_health() is True

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/tags"


def test_chat_posts_one_non_streaming_schema_request(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    schema = {"type": "object", "properties": {"category": {"type": "string"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": '{"ok": true}'}},
            request=request,
        )

    with _client(tmp_path, httpx.MockTransport(handler)) as client:
        content = client.chat([{"role": "user", "content": "hello"}], schema)

    assert content == '{"ok": true}'
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/api/chat"
    assert payload["model"] == "fixture-model"
    assert payload["stream"] is False
    assert payload["format"] == schema
    assert payload["options"] == {"temperature": 0}
    assert payload["messages"] == [{"role": "user", "content": "hello"}]


def test_health_failure_is_boolean(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    with _client(tmp_path, httpx.MockTransport(handler)) as client:
        assert client.check_health() is False


def test_chat_http_error_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    with _client(tmp_path, httpx.MockTransport(handler)) as client, pytest.raises(
        OllamaHTTPError, match="HTTP 500"
    ):
        client.chat([{"role": "user", "content": "hello"}], {})


def test_chat_timeout_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    with _client(tmp_path, httpx.MockTransport(handler)) as client, pytest.raises(
        OllamaTimeout
    ):
        client.chat([{"role": "user", "content": "hello"}], {})


def test_chat_without_message_content_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {}}, request=request)

    with _client(tmp_path, httpx.MockTransport(handler)) as client, pytest.raises(
        OllamaResponseError
    ):
        client.chat([{"role": "user", "content": "hello"}], {})
