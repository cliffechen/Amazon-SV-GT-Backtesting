"""Cross-platform provider configuration without committed credentials.

The full MCP URL can contain a query-string credential.  It is therefore read
from an explicit argument, an environment variable, or a user-owned config
file, and must never be copied into project manifests or logs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ProviderConfigError(RuntimeError):
    pass


_ENVIRONMENTS = {
    "sellersprite": "ASVGT_SELLERSPRITE_MCP_URL",
    "sif": "ASVGT_SIF_MCP_URL",
}


def default_user_config(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("ASVGT_PROVIDER_CONFIG"):
        return Path(env["ASVGT_PROVIDER_CONFIG"]).expanduser()
    if os.name == "nt" or env.get("APPDATA"):
        base = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Amazon-SV-GT-Backtesting" / "providers.json"


def redacted_url(url: str) -> str:
    """Return a log-safe endpoint with credentials and query removed."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-provider-url>"
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _url_from_document(document: object, provider: str) -> str | None:
    if not isinstance(document, dict):
        return None
    # Preferred shareable shape: {"providers": {"sif": {"url": "..."}}}
    providers = document.get("providers")
    if isinstance(providers, dict):
        entry = providers.get(provider)
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            return entry["url"]
    # Explicitly supplied legacy ZCode config files remain importable, but no
    # user-specific legacy location is ever searched automatically.
    try:
        value = document["mcp"]["servers"][provider]["url"]
    except (KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


def load_provider_url(
    provider: str,
    explicit_url: str | None = None,
    config_path: str | Path | None = None,
    environ: dict[str, str] | None = None,
    *,
    allow_http: bool = False,
) -> str:
    provider = str(provider).casefold()
    if provider not in _ENVIRONMENTS:
        raise ProviderConfigError(f"Unsupported provider: {provider}")
    env = os.environ if environ is None else environ
    url = explicit_url or env.get(_ENVIRONMENTS[provider])
    source = "argument/environment"
    if not url:
        path = Path(config_path).expanduser() if config_path else default_user_config(env)
        source = str(path)
        if not path.exists():
            raise ProviderConfigError(
                f"Missing {provider} MCP URL. Set {_ENVIRONMENTS[provider]} or create {path}."
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderConfigError(f"Cannot read provider config {path}: {exc}") from exc
        url = _url_from_document(document, provider)
    if not isinstance(url, str) or not url.strip():
        raise ProviderConfigError(f"No URL configured for {provider} in {source}")
    url = url.strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ProviderConfigError(f"Invalid URL for {provider}") from exc
    if parsed.scheme not in ({"http", "https"} if allow_http else {"https"}) or not parsed.hostname:
        expected = "http(s)" if allow_http else "https"
        raise ProviderConfigError(f"{provider} MCP URL must use {expected} and include a host")
    return url
