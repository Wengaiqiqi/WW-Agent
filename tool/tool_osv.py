"""OSV malware / vulnerability check via the public OSV API.

Ported from ``hermes-agent/tools/osv_check.py``.

Two surfaces:

* :func:`check_package_for_malware` — pre-flight check matching the original
  hermes signature; given a launcher command (``npx`` / ``uvx`` / ``pipx``)
  and its args, returns a BLOCK message or ``None``.
* :func:`osv_lookup` — direct ``(package, ecosystem, version)`` lookup that
  returns the full advisory list (malware + regular CVEs). This is what the
  ``osv_check`` LangChain tool ultimately exposes.

The OSV endpoint is free and public; default fail-open on network errors.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_OSV_ENDPOINT = "https://api.osv.dev/v1/query"
_TIMEOUT = 10  # seconds


def _osv_endpoint() -> str:
    """Resolve at call time so a runtime ``setenv`` (e.g. pointing at a
    test stub) takes effect without re-importing the module."""
    return os.getenv("OSV_ENDPOINT", _DEFAULT_OSV_ENDPOINT)


def osv_lookup(
    package: str,
    ecosystem: str,
    version: Optional[str] = None,
    malware_only: bool = False,
) -> dict[str, Any]:
    """Query OSV for advisories on a package.

    Returns ``{"package", "ecosystem", "version", "vulns": [...]}``. On
    network/API errors returns ``{"error": "...", "vulns": []}`` instead of
    raising — callers can treat that as "unknown / allow".
    """
    payload: dict[str, Any] = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _osv_endpoint(),
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "agent-osv-check/1.0",
        },
        method="POST",
    )

    from tool.tool_web import OPENER

    try:
        # Route through OPENER so a (hypothetical) 30x off api.osv.dev to a
        # private host can't be followed silently. The initial public host
        # doesn't need ``hostname_is_safe`` — api.osv.dev is hard-coded.
        with OPENER.open(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        return {
            "package": package,
            "ecosystem": ecosystem,
            "version": version,
            "vulns": [],
            "error": f"OSV query failed: {exc}",
        }

    vulns = body.get("vulns", []) or []
    if malware_only:
        vulns = [v for v in vulns if v.get("id", "").startswith("MAL-")]

    summary = []
    for v in vulns[:25]:
        summary.append(
            {
                "id": v.get("id"),
                "summary": (v.get("summary") or "")[:200],
                "aliases": v.get("aliases", []),
                "modified": v.get("modified"),
                "severity": _flatten_severity(v.get("severity")),
            }
        )

    return {
        "package": package,
        "ecosystem": ecosystem,
        "version": version,
        "count": len(vulns),
        "vulns": summary,
    }


def check_package_for_malware(command: str, args: list) -> Optional[str]:
    """Pre-flight malware check for ``npx`` / ``uvx`` / ``pipx`` launchers.

    Mirrors the hermes signature so existing call sites can drop in unchanged.
    """
    ecosystem = _infer_ecosystem(command)
    if not ecosystem:
        return None

    package, version = _parse_package_from_args(args, ecosystem)
    if not package:
        return None

    result = osv_lookup(package, ecosystem, version, malware_only=True)
    if result.get("error"):
        return None  # fail-open
    malware = result.get("vulns", [])
    if not malware:
        return None

    ids = ", ".join(m["id"] for m in malware[:3] if m.get("id"))
    summaries = "; ".join((m.get("summary") or m.get("id") or "")[:100] for m in malware[:3])
    return (
        f"BLOCKED: Package '{package}' ({ecosystem}) has known malware "
        f"advisories: {ids}. Details: {summaries}"
    )


def _infer_ecosystem(command: str) -> Optional[str]:
    base = os.path.basename(command).lower()
    if base in {"npx", "npx.cmd"}:
        return "npm"
    if base in {"uvx", "uvx.cmd", "pipx"}:
        return "PyPI"
    return None


def _parse_package_from_args(args: list, ecosystem: str) -> tuple[Optional[str], Optional[str]]:
    if not args:
        return None, None

    package_token = None
    for arg in args:
        if not isinstance(arg, str) or arg.startswith("-"):
            continue
        package_token = arg
        break

    if not package_token:
        return None, None

    if ecosystem == "npm":
        return _parse_npm_package(package_token)
    if ecosystem == "PyPI":
        return _parse_pypi_package(package_token)
    return package_token, None


def _parse_npm_package(token: str) -> tuple[Optional[str], Optional[str]]:
    if token.startswith("@"):
        match = re.match(r"^(@[^/]+/[^@]+)(?:@(.+))?$", token)
        if match:
            return match.group(1), match.group(2)
        return token, None
    if "@" in token:
        parts = token.rsplit("@", 1)
        name = parts[0]
        version = parts[1] if len(parts) > 1 and parts[1] != "latest" else None
        return name, version
    return token, None


def _parse_pypi_package(token: str) -> tuple[Optional[str], Optional[str]]:
    match = re.match(r"^([a-zA-Z0-9._-]+)(?:\[[^\]]*\])?(?:==(.+))?$", token)
    if match:
        return match.group(1), match.group(2)
    return token, None


def _flatten_severity(severity: Any) -> Optional[str]:
    if not severity:
        return None
    if isinstance(severity, list):
        parts = []
        for item in severity:
            if isinstance(item, dict):
                t = item.get("type", "")
                s = item.get("score", "")
                if t or s:
                    parts.append(f"{t}:{s}".strip(":"))
        return ", ".join(parts) or None
    return str(severity)
