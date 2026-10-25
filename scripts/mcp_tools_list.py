"""Print the tool names the MCP server advertises, comma-separated, or exit non-zero.

Used by status.ps1 and start-all.ps1 to check the tools server *through the protocol* rather than
by poking its port - a listening socket says nothing about whether the MCP handshake works.

A file rather than `python -c`: PowerShell strips the quotes when passing inline code on Windows,
so `r"D:\\AIRA\\backend"` arrived at Python as `rD:\\AIRA\\backend` and the helper failed with a
SyntaxError every time. It reported "advertises no tools" on a cold start while the backend, asked
the same question a moment later, listed all three.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import mcp_client  # noqa: E402


def main() -> int:
    try:
        names = asyncio.run(mcp_client.tool_names())
    except Exception as e:  # noqa: BLE001 - the caller only needs to know it did not answer
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not names:
        print("the server answered but advertises no tools", file=sys.stderr)
        return 1
    print(",".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
