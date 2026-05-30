"""
specmcp report-issue — bundle a sanitized debug report.

Collects:
  - specmcp version + Python version + platform
  - Spec source (path/URL, no content)
  - Config summary (all auth values redacted, env var names preserved)
  - Pipeline results: tool count, warning count, fallback count
  - Per-tool names and warning list
  - Any pipeline errors encountered

No credential values appear in the output. Auth config shows only the
env var name (e.g. "env(PETSTORE_API_KEY)"), never the resolved value.
SensitiveStr fields are never resolved here.
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from specmcp import __version__
from specmcp.cli.app import app
from specmcp.errors import SpecmcpError, exit_code_for


@app.command("report-issue")
def report_issue_cmd(
    spec: Optional[str] = typer.Option(  # noqa: UP007
        None,
        "--spec",
        "-s",
        help="Path or URL to the OpenAPI spec.",
    ),
    config: Optional[Path] = typer.Option(  # noqa: UP007
        Path("mcp.config.yaml"),
        "--config",
        "-c",
        help="Path to the config file.",
        show_default=True,
    ),
    output: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--output",
        "-o",
        help="Write report to this file instead of stdout.",
    ),
) -> None:
    """Bundle a sanitized debug report for filing an issue."""
    from specmcp.config import Config, SimplifyConfig
    from specmcp.core.expose import ToolRegistry
    from specmcp.core.load import load_spec
    from specmcp.core.normalize import normalize
    from specmcp.core.simplify import simplify

    report: dict[str, Any] = {
        "specmcp_version": __version__,
        "python_version": sys.version,
        "platform": platform.platform(),
        "spec_source": None,
        "config_summary": None,
        "pipeline": None,
        "errors": [],
    }

    # --- Config ---
    cfg: Optional[Config] = None  # noqa: UP007
    if config and config.exists():
        try:
            cfg = Config.load(config)
            report["config_summary"] = _summarize_config(cfg)
        except SpecmcpError as exc:
            report["errors"].append({"stage": "config", "error": str(exc)})

    # --- Spec source ---
    spec_source: str | None = None
    if spec:
        spec_source = spec
    elif cfg:
        spec_source = cfg.spec.source
    report["spec_source"] = spec_source

    if spec_source is None:
        report["errors"].append({
            "stage": "startup",
            "error": "No spec source. Provide --spec or a config file.",
        })
        _emit(report, output)
        return

    # --- Load ---
    try:
        raw, resolved = load_spec(spec_source)
        report["openapi_version"] = resolved.openapi_version
    except SpecmcpError as exc:
        report["errors"].append({"stage": "load", "error": str(exc)})
        _emit(report, output)
        return

    # --- Normalize ---
    try:
        ops = normalize(
            resolved,
            base_url_override=cfg.server.base_url_override if cfg else None,
            include_deprecated=cfg.server.include_deprecated if cfg else True,
            include_tags=cfg.server.include_tags if cfg else None,
            exclude_tags=cfg.server.exclude_tags if cfg else None,
            include_operations=cfg.server.include_operations if cfg else None,
            exclude_operations=cfg.server.exclude_operations if cfg else None,
        )
    except SpecmcpError as exc:
        report["errors"].append({"stage": "normalize", "error": str(exc)})
        _emit(report, output)
        return

    # --- Simplify ---
    simplify_cfg = cfg.simplify if cfg else SimplifyConfig()
    simplified_ops = simplify(ops, config=simplify_cfg)

    # --- Expose ---
    registry = ToolRegistry.build(simplified_ops, config=cfg)

    # --- Pipeline summary ---
    all_warnings = [w for sop in simplified_ops for w in sop.warnings]
    fallback_count = sum(
        1 for sop in simplified_ops
        if any(w.kind == "fallback_to_freeform" for w in sop.warnings)
    )

    report["pipeline"] = {
        "operation_count": len(ops),
        "tool_count": len(registry.tools),
        "warning_count": len(all_warnings),
        "fallback_count": fallback_count,
        "tools": [
            {
                "name": t.name,
                "method": t.simplified_operation.operation.method,
                "path": t.simplified_operation.operation.path,
                "warnings": [
                    {"kind": w.kind, "message": w.message}
                    for w in t.simplified_operation.warnings
                ],
            }
            for t in registry.tools
        ],
        "warnings": [
            {
                "kind": w.kind,
                "operation_id": w.operation_id,
                "field_path": w.field_path,
                "message": w.message,
            }
            for w in all_warnings
        ],
    }

    _emit(report, output)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize_config(cfg: Any) -> dict[str, Any]:
    """Return a JSON-safe config summary with all auth values redacted."""
    auth_summary: dict[str, Any] = {}
    for name, scheme in cfg._auth_schemes.items():  # noqa: SLF001
        entry: dict[str, Any] = {"type": scheme.type if hasattr(scheme, "type") else "unknown"}
        # Show value_from (env var name) but never the resolved value
        if hasattr(scheme, "value_from"):
            entry["value_from"] = scheme.value_from  # e.g. "env(PETSTORE_API_KEY)"
        if hasattr(scheme, "in_"):
            entry["in"] = scheme.in_
        if hasattr(scheme, "name"):
            entry["name"] = scheme.name
        auth_summary[name] = entry

    return {
        "version": cfg.version,
        "spec_source": cfg.spec.source,
        "auth_schemes": auth_summary,
        "server": cfg.server.model_dump(),
        "dispatch": cfg.dispatch.model_dump(),
        "simplify": cfg.simplify.model_dump(),
        "transport": cfg.transport.model_dump(),
        "operation_overrides": list(cfg.operations.keys()),
    }


def _emit(report: dict[str, Any], output: Path | None) -> None:
    """Write the report to *output* file or stdout."""
    text = json.dumps(report, indent=2, default=str)
    if output:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"Report written to {output}", err=True)
        typer.echo(
            "Paste the contents of that file into a GitHub issue at:\n"
            "  https://github.com/specmcp/specmcp/issues/new",
            err=True,
        )
    else:
        typer.echo(text)
        typer.echo(
            "\nPaste the JSON above into a GitHub issue at:\n"
            "  https://github.com/specmcp/specmcp/issues/new\n"
            "Tip: use --output report.json to save it to a file instead.",
            err=True,
        )
