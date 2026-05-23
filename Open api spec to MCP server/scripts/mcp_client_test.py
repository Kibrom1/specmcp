#!/usr/bin/env python3
"""
specmcp local MCP client test script.

Spawns `specmcp serve` as a subprocess and talks to it over the real
MCP stdio protocol — the same wire format Claude Desktop uses.

Usage:
    python scripts/mcp_client_test.py [--spec SPEC] [--config CONFIG] [--verbose]

Options:
    --spec PATH     Path to OpenAPI spec (default: test-corpus/petstore.json)
    --config PATH   Path to mcp.config.yaml (optional)
    --verbose       Print full request/response JSON

Examples:
    # Test with the bundled petstore spec (no auth required in mock mode)
    python scripts/mcp_client_test.py

    # Test with your own spec and config
    python scripts/mcp_client_test.py --spec openapi.json --config mcp.config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Colour helpers (degrade gracefully if terminal doesn't support ANSI)
# ---------------------------------------------------------------------------

_COLOUR = sys.stdout.isatty()

def _green(s):  return f"\033[32m{s}\033[0m" if _COLOUR else s
def _red(s):    return f"\033[31m{s}\033[0m" if _COLOUR else s
def _yellow(s): return f"\033[33m{s}\033[0m" if _COLOUR else s
def _bold(s):   return f"\033[1m{s}\033[0m"  if _COLOUR else s
def _dim(s):    return f"\033[2m{s}\033[0m"  if _COLOUR else s


PASS = _green("✓ PASS")
FAIL = _red("✗ FAIL")
SKIP = _yellow("- SKIP")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            print(f"  {PASS}  {name}")
            self.passed += 1
        else:
            print(f"  {FAIL}  {name}")
            if detail:
                print(f"        {_red(detail)}")
            self.failed += 1

    def skip(self, name: str, reason: str = ""):
        print(f"  {SKIP}  {name}" + (f"  [{reason}]" if reason else ""))
        self.skipped += 1

    def summary(self):
        total = self.passed + self.failed + self.skipped
        print()
        if self.failed == 0:
            print(_bold(_green(f"All {self.passed}/{total} checks passed.")))
        else:
            print(_bold(_red(f"{self.failed} check(s) failed. {self.passed}/{total} passed.")))
        return self.failed == 0


# ---------------------------------------------------------------------------
# Main test suite
# ---------------------------------------------------------------------------


async def run_tests(spec: str, config: str | None, verbose: bool) -> bool:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # Build the command to spawn specmcp
    cmd = sys.executable  # use same Python interpreter
    args = ["-m", "specmcp", "serve", "--spec", spec]
    if config:
        args += ["--config", config]

    # Try the installed entry point first, fall back to -m
    specmcp_bin = Path(sys.executable).parent / "specmcp"
    if specmcp_bin.exists():
        cmd = str(specmcp_bin)
        args = ["serve", "--spec", spec]
        if config:
            args += ["--config", config]

    print(_bold(f"\nspawning: {cmd} {' '.join(args)}"))
    print(_dim("(stderr from server will appear below if --verbose is set)\n"))

    server_params = StdioServerParameters(
        command=cmd,
        args=args,
        cwd=str(Path(__file__).parent.parent),  # repo root
        env={**os.environ},
    )

    results = Results()

    stderr_dest = sys.stderr if verbose else open(os.devnull, "w")  # noqa: WPS515

    try:
        async with stdio_client(server_params, errlog=stderr_dest) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # --------------------------------------------------------
                # Section 1: tools/list
                # --------------------------------------------------------
                print(_bold("\n── tools/list ──────────────────────────────────"))

                list_resp = await session.list_tools()
                tools = list_resp.tools

                results.check(
                    "tools/list returns at least one tool",
                    len(tools) >= 1,
                    f"got {len(tools)} tools",
                )

                print(f"\n  {_dim('Tools exposed:')}")
                for t in tools:
                    print(f"    {_bold(t.name)}")
                    if t.description:
                        desc = t.description[:80] + ("…" if len(t.description) > 80 else "")
                        print(f"      {_dim(desc)}")
                    if verbose and t.inputSchema:
                        schema_str = json.dumps(t.inputSchema, indent=6)
                        for line in schema_str.splitlines():
                            print(f"      {_dim(line)}")

                tool_names = {t.name for t in tools}

                # --------------------------------------------------------
                # Section 2: tools/call — pick the first GET tool we can call
                # --------------------------------------------------------
                print(_bold("\n── tools/call ──────────────────────────────────"))

                # Find a tool with no required args (safest to call without config)
                no_req_tool = next(
                    (t for t in tools
                     if not t.inputSchema.get("required")),
                    None,
                )
                list_tool = next(
                    (t for t in tools if t.name.lower().startswith("list")),
                    no_req_tool,
                )

                if list_tool:
                    print(f"\n  Calling {_bold(list_tool.name)} with empty args…")
                    try:
                        call_resp = await session.call_tool(list_tool.name, {})
                        content = call_resp.content
                        results.check(
                            f"{list_tool.name} returns content",
                            len(content) > 0,
                        )
                        if content:
                            text = getattr(content[0], "text", "")
                            results.check(
                                f"{list_tool.name} content is non-empty text",
                                bool(text.strip()),
                            )
                            if verbose:
                                print(f"\n  {_dim('Response:')}")
                                for line in text[:500].splitlines():
                                    print(f"    {_dim(line)}")
                                if len(text) > 500:
                                    print(f"    {_dim('… (truncated)')}")
                            else:
                                preview = text[:120].replace("\n", " ")
                                if len(text) > 120:
                                    preview += "…"
                                print(f"  {_dim('→')} {preview}")
                    except Exception as exc:
                        results.check(f"{list_tool.name} call succeeded", False, str(exc))
                else:
                    results.skip("tools/call list operation", "no zero-arg tool found")

                # --------------------------------------------------------
                # Section 3: tools/call — petstore-specific tests
                # --------------------------------------------------------
                if "listPets" in tool_names:
                    print(_bold("\n── Petstore-specific checks ────────────────────"))

                    # listPets with limit
                    print(f"\n  Calling {_bold('listPets')} with limit=2…")
                    try:
                        resp = await session.call_tool("listPets", {"limit": 2})
                        text = getattr(resp.content[0], "text", "") if resp.content else ""
                        results.check(
                            "listPets(limit=2) returns content",
                            bool(text.strip()),
                        )
                        if verbose and text:
                            print(f"  {_dim('→')} {text[:200]}")
                    except Exception as exc:
                        results.check("listPets(limit=2) succeeded", False, str(exc))

                    # getPetById
                    if "getPetById" in tool_names:
                        print(f"\n  Calling {_bold('getPetById')} with petId=1…")
                        try:
                            resp = await session.call_tool("getPetById", {"petId": 1})
                            text = getattr(resp.content[0], "text", "") if resp.content else ""
                            results.check(
                                "getPetById(petId=1) returns content",
                                bool(text.strip()),
                            )
                            if verbose and text:
                                print(f"  {_dim('→')} {text[:200]}")
                        except Exception as exc:
                            results.check("getPetById(petId=1) succeeded", False, str(exc))

                # --------------------------------------------------------
                # Section 4: Error handling
                # --------------------------------------------------------
                print(_bold("\n── Error handling ──────────────────────────────"))

                # Call with wrong type (should get ArgumentValidationError)
                if "getPetById" in tool_names:
                    print(f"\n  Calling {_bold('getPetById')} with petId='not-an-int'…")
                    try:
                        resp = await session.call_tool("getPetById", {"petId": "not-an-int"})
                        # Should return an error text block, not crash
                        results.check(
                            "Invalid arg type returns error text (not crash)",
                            len(resp.content) > 0,
                        )
                    except Exception as exc:
                        results.check(
                            "Invalid arg type handled gracefully",
                            False,
                            f"server raised exception: {exc}",
                        )
                else:
                    results.skip("invalid arg type test", "getPetById not available")

    except Exception as exc:
        print(_red(f"\nFailed to connect to specmcp server: {exc}"))
        print(_dim("Make sure specmcp is installed: pip install -e ."))
        return False
    finally:
        if not verbose:
            stderr_dest.close()

    return results.summary()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run MCP client tests against a live specmcp serve process.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          # Petstore (bundled, no credentials needed for inspect/connect test)
          python scripts/mcp_client_test.py

          # Your own spec + config
          python scripts/mcp_client_test.py --spec openapi.json --config mcp.config.yaml

          # Verbose: show full schemas and responses
          python scripts/mcp_client_test.py --verbose
        """),
    )
    parser.add_argument(
        "--spec",
        default="test-corpus/petstore.json",
        help="Path or URL to OpenAPI spec (default: test-corpus/petstore.json)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to mcp.config.yaml (optional)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print full schemas and response bodies",
    )
    args = parser.parse_args()

    print(_bold("\nspecmcp local MCP client test"))
    print(_dim("=" * 50))
    print(f"  spec   : {args.spec}")
    print(f"  config : {args.config or '(none)'}")

    ok = asyncio.run(run_tests(args.spec, args.config, args.verbose))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
