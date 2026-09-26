"""Small, strict Streamable-HTTP MCP client used by collection scripts.

It validates transport status and JSON-RPC identity, handles JSON and SSE
responses, keeps session identifiers in memory, and deliberately avoids putting
endpoint URLs or response bodies into exception strings.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

from .provider_config import redacted_url


class MCPError(RuntimeError):
    """Base class for sanitized MCP failures."""


class MCPTransportError(MCPError):
    pass


class MCPProtocolError(MCPError):
    pass


class MCPProviderError(MCPError):
    pass


def parse_transport_messages(text: str) -> list[dict[str, Any]]:
    """Decode a plain JSON or SSE-framed JSON-RPC response.

    SSE permits several ``data:`` lines in one event; they are joined before
    parsing.  Multiple events are retained so callers can select the response
    whose JSON-RPC id matches their request.
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP response is not valid JSON") from exc
        values = decoded if isinstance(decoded, list) else [decoded]
        if not all(isinstance(value, dict) for value in values):
            raise MCPProtocolError("MCP JSON response must contain objects")
        return values

    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def finish_event() -> None:
        nonlocal data_lines
        if not data_lines:
            return
        payload = "\n".join(data_lines).strip()
        data_lines = []
        if not payload or payload == "[DONE]":
            return
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP SSE event contains invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise MCPProtocolError("MCP SSE event must contain a JSON object")
        messages.append(decoded)

    for raw_line in stripped.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            finish_event()
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    finish_event()
    if not messages:
        raise MCPProtocolError("MCP response is neither JSON nor a data-bearing SSE event")
    return messages


class MCPClient:
    def __init__(
        self,
        url: str,
        *,
        session: requests.Session | None = None,
        protocol_version: str = "2025-03-26",
        client_name: str = "amazon-sv-gt-backtesting",
        client_version: str = "1.0",
        timeout: float = 120,
        allow_http: bool = False,
    ) -> None:
        parsed = urlsplit(url)
        allowed = {"https", "http"} if allow_http else {"https"}
        if parsed.scheme not in allowed or not parsed.hostname:
            raise ValueError("MCP endpoint must be an absolute HTTPS URL")
        self._url = url
        self.endpoint = redacted_url(url)
        self._session = session or requests.Session()
        self._owns_session = session is None
        self.protocol_version = protocol_version
        self.client_name = client_name
        self.client_version = client_version
        self.timeout = timeout
        self.session_id: str | None = None
        self.server_info: dict[str, Any] = {}
        self._counter = 0
        self._initialized = False
        self._closed = False

    def _id(self) -> int:
        self._counter += 1
        return self._counter

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    def _post(self, message: dict[str, Any], *, allow_empty: bool = False) -> dict[str, Any] | None:
        expected_id = message.get("id")
        try:
            response = self._session.post(
                self._url, headers=self._headers(), json=message, timeout=self.timeout
            )
        except requests.RequestException as exc:
            # Do not include requests' message: it can contain the credentialed URL.
            raise MCPTransportError(f"MCP POST failed ({type(exc).__name__}) at {self.endpoint}") from exc
        status = int(getattr(response, "status_code", 0))
        if status < 200 or status >= 300:
            raise MCPTransportError(f"MCP HTTP status {status} at {self.endpoint}")
        self._capture_session_id(response)
        messages = parse_transport_messages(getattr(response, "text", ""))
        if not messages:
            if allow_empty:
                return None
            raise MCPProtocolError("MCP returned an empty response")
        if expected_id is None:
            selected = messages[-1]
        else:
            selected = next((entry for entry in messages if entry.get("id") == expected_id), None)
            if selected is None:
                raise MCPProtocolError("MCP JSON-RPC response id does not match the request")
        if selected.get("jsonrpc") != "2.0":
            raise MCPProtocolError("MCP response is not JSON-RPC 2.0")
        if "error" in selected:
            error = selected.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            raise MCPProviderError(f"MCP JSON-RPC error code={code!r}")
        if expected_id is not None and "result" not in selected:
            raise MCPProtocolError("MCP response has no result")
        return selected

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return self.server_info
        request_id = self._id()
        response = self._post({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
        })
        result = response["result"] if response else None
        if not isinstance(result, dict):
            raise MCPProtocolError("MCP initialize result must be an object")
        self.server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        self._initialized = True
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, allow_empty=True)
        return self.server_info

    def _capture_session_id(self, response: Any) -> None:
        headers = getattr(response, "headers", {}) or {}
        sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = str(sid)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        request_id = self._id()
        response = self._post({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        result = response["result"] if response else None
        if not isinstance(result, dict):
            raise MCPProtocolError("MCP tool result must be an object")
        if result.get("isError"):
            raise MCPProviderError("MCP tool returned isError=true")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        if not self._initialized:
            self.initialize()
        response = self._post({
            "jsonrpc": "2.0", "id": self._id(), "method": "tools/list", "params": {}
        })
        result = response["result"] if response else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            raise MCPProtocolError("MCP tools/list returned an invalid tools array")
        return tools

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self.session_id:
                try:
                    self._session.delete(
                        self._url, headers=self._headers(), timeout=min(self.timeout, 30)
                    )
                except requests.RequestException:
                    pass
        finally:
            self.session_id = None
            self._closed = True
            if self._owns_session:
                self._session.close()

    def __enter__(self) -> "MCPClient":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
