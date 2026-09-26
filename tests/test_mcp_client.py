import json

import pytest

from predictor.mcp_client import (
    MCPClient,
    MCPProtocolError,
    MCPProviderError,
    MCPTransportError,
    parse_transport_messages,
)
from predictor.provider_config import ProviderConfigError, load_provider_url, redacted_url


class FakeResponse:
    def __init__(self, status_code=200, body="", headers=None):
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.deletes = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.responses.pop(0)

    def delete(self, url, **kwargs):
        self.deletes.append((url, kwargs))
        return FakeResponse(200, "")


def rpc(request_id, result):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


def test_streamable_http_validates_id_uses_session_and_closes_without_persisting(tmp_path):
    session = FakeSession([
        FakeResponse(200, rpc(1, {"serverInfo": {"name": "mock"}}), {"mcp-session-id": "secret-session"}),
        FakeResponse(202, ""),
        FakeResponse(200, "event: message\ndata: " + rpc(999, {}) + "\n\n"
                     + "event: message\ndata: " + rpc(2, {"content": []}) + "\n\n"),
    ])
    client = MCPClient("https://example.test/mcp?token=top-secret", session=session)
    client.initialize()
    assert client.call_tool("demo", {"x": 1}) == {"content": []}
    assert session.posts[1][1]["headers"]["mcp-session-id"] == "secret-session"
    client.close()
    assert len(session.deletes) == 1
    assert session.deletes[0][1]["headers"]["mcp-session-id"] == "secret-session"
    assert client.session_id is None
    assert not list(tmp_path.iterdir())


def test_multiline_sse_and_done_marker_are_supported():
    messages = parse_transport_messages(
        'data: {"jsonrpc":"2.0",\n'
        'data: "id":7,"result":{"ok":true}}\n\n'
        'data: [DONE]\n\n'
    )
    assert messages == [{"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}]


def test_wrong_jsonrpc_id_is_rejected():
    session = FakeSession([
        FakeResponse(200, rpc(1, {})),
        FakeResponse(202, ""),
        FakeResponse(200, rpc(88, {"content": []})),
    ])
    client = MCPClient("https://example.test/mcp", session=session)
    client.initialize()
    with pytest.raises(MCPProtocolError, match="id"):
        client.call_tool("demo", {})


def test_http_and_jsonrpc_errors_are_sanitized():
    secret_url = "https://example.test/mcp?api_key=do-not-log"
    transport = MCPClient(secret_url, session=FakeSession([FakeResponse(503, "credential response")]))
    with pytest.raises(MCPTransportError) as error:
        transport.initialize()
    assert "do-not-log" not in str(error.value)
    assert "credential response" not in str(error.value)

    provider = MCPClient(secret_url, session=FakeSession([
        FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1,
                                      "error": {"code": -32000, "message": "secret detail"}}))
    ]))
    with pytest.raises(MCPProviderError) as error2:
        provider.initialize()
    assert "secret detail" not in str(error2.value)
    assert "do-not-log" not in str(error2.value)


def test_tool_iserror_is_rejected():
    session = FakeSession([
        FakeResponse(200, rpc(1, {})),
        FakeResponse(202, ""),
        FakeResponse(200, rpc(2, {"isError": True, "content": []})),
    ])
    client = MCPClient("https://example.test/mcp", session=session)
    client.initialize()
    with pytest.raises(MCPProviderError, match="isError"):
        client.call_tool("demo", {})


def test_provider_config_prefers_environment_and_redacts_query(tmp_path):
    environment = {"ASVGT_SIF_MCP_URL": "https://sif.example/mcp?token=secret"}
    url = load_provider_url("sif", environ=environment)
    assert url.endswith("token=secret")
    assert redacted_url(url) == "https://sif.example/mcp"

    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": {"sif": {"url": "https://other.example/mcp?q=x"}}}))
    assert load_provider_url("sif", config_path=config, environ={}).startswith("https://other.example")
    with pytest.raises(ProviderConfigError, match="Missing"):
        load_provider_url("sellersprite", config_path=tmp_path / "missing.json", environ={})
