"""The only module allowed to speak HTTP: a thin client for a configured Ollama."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx

from sloth_valet.config import ValetConfig


class OllamaError(RuntimeError):
    """Base class for failures from the local model service."""


class OllamaUnavailable(OllamaError):
    """The configured Ollama service could not be reached."""


class OllamaTimeout(OllamaError):
    """An Ollama request exceeded the configured timeout."""


class OllamaHTTPError(OllamaError):
    """Ollama returned a non-success HTTP status."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Ollama returned HTTP {status_code}")
        self.status_code = status_code


class OllamaResponseError(OllamaError):
    """Ollama returned a response without usable message content."""


class OllamaClient:
    """Small localhost-only client with no retries, streaming, or hidden endpoints."""

    def __init__(
        self,
        config: ValetConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=config.model.host,
            timeout=httpx.Timeout(config.model.timeout_s),
            trust_env=False,
            transport=transport,
        )

    def check_health(self) -> bool:
        """Return whether GET /api/tags succeeds on the configured host."""
        try:
            response = self._client.get("/api/tags")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def chat(self, messages: list[dict[str, str]], response_schema: dict[str, Any]) -> str:
        """Send one non-streaming chat request with a strict JSON-schema format."""
        payload: dict[str, Any] = {
            "model": self._config.model.name,
            "messages": messages,
            "stream": False,
            "format": response_schema,
            "options": {"temperature": self._config.model.temperature},
        }
        try:
            response = self._client.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise OllamaTimeout("Ollama chat request timed out") from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"Ollama chat request failed: {exc}") from exc

        if response.status_code >= 400:
            raise OllamaHTTPError(response.status_code)

        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaResponseError("Ollama response was not JSON") from exc
        if not isinstance(data, dict):
            raise OllamaResponseError("Ollama response was not an object")

        message = data.get("message")
        if not isinstance(message, dict):
            raise OllamaResponseError("Ollama response had no message object")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise OllamaResponseError("Ollama response had no message content")
        return content

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
