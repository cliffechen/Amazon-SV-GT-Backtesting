"""List SellerSprite MCP tools without persisting session credentials."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from predictor.mcp_client import MCPClient  # noqa: E402
from predictor.provider_config import load_provider_url, redacted_url  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-config")
    args = parser.parse_args(argv)
    url = load_provider_url("sellersprite", config_path=args.provider_config)
    with MCPClient(url, client_name="amazon-sv-gt-discovery") as client:
        tools = client.list_tools()
        result = {
            "endpoint": redacted_url(url),
            "server": client.server_info,
            "tool_count": len(tools),
            "tools": [
                {"name": tool.get("name"), "description": tool.get("description", "")}
                for tool in tools
            ],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
