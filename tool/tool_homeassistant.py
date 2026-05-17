"""Home Assistant REST API integration.

Ported from ``hermes-agent/tools/homeassistant_tool.py`` but rewritten with
``urllib.request`` so we don't pull in ``aiohttp``. Four operations are
exposed via a single ``home_assistant`` LangChain tool dispatched by
``action``:

* ``list_entities`` (filter by domain/area)
* ``get_state``     (single ``entity_id``)
* ``list_services`` (optional ``domain`` filter)
* ``call_service``  (call a HA service; domain/service whitelisted)

Authentication: ``HASS_URL`` (default ``http://homeassistant.local:8123``) +
``HASS_TOKEN`` (long-lived access token).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Block domains that would let HA execute arbitrary code on its host.
BLOCKED_DOMAINS = frozenset(
    {
        "shell_command",
        "command_line",
        "python_script",
        "pyscript",
        "hassio",
        "rest_command",
    }
)


def _config() -> tuple[str, str]:
    url = os.getenv("HASS_URL", "http://homeassistant.local:8123").rstrip("/")
    token = os.getenv("HASS_TOKEN", "")
    return url, token


def _request(method: str, path: str, body: Optional[dict] = None, timeout: float = 15.0) -> Any:
    """Issue an authenticated HTTP request to the user's Home Assistant.

    HASS_URL is user-configured (often a private-network address like
    ``http://homeassistant.local:8123``), so we deliberately do *not* run
    ``hostname_is_safe`` on the initial request — that would refuse the
    entire HA tool by design. Redirects ARE validated via ``OPENER``: if a
    compromised or misconfigured HA endpoint replies with a 30x to an
    *unrelated* private IP, the bearer token won't leak there.
    """
    from tool.tool_web import OPENER

    url, token = _config()
    if not token:
        raise RuntimeError("HASS_TOKEN is not set")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    with OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def list_entities(domain: Optional[str] = None, area: Optional[str] = None) -> dict[str, Any]:
    states = _request("GET", "/api/states") or []

    if domain:
        states = [s for s in states if s.get("entity_id", "").startswith(f"{domain}.")]

    if area:
        a = area.lower()
        states = [
            s
            for s in states
            if a in (s.get("attributes", {}).get("friendly_name", "") or "").lower()
            or a in (s.get("attributes", {}).get("area", "") or "").lower()
        ]

    entities = [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
        }
        for s in states
    ]
    return {"count": len(entities), "entities": entities}


def get_state(entity_id: str) -> dict[str, Any]:
    if not _ENTITY_ID_RE.match(entity_id):
        raise ValueError(f"Invalid entity_id format: {entity_id!r}")
    data = _request("GET", f"/api/states/{entity_id}")
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected HA state response: {data!r}")
    return {
        "entity_id": data["entity_id"],
        "state": data["state"],
        "attributes": data.get("attributes", {}),
        "last_changed": data.get("last_changed"),
        "last_updated": data.get("last_updated"),
    }


def list_services(domain: Optional[str] = None) -> dict[str, Any]:
    services = _request("GET", "/api/services") or []
    if domain:
        services = [s for s in services if s.get("domain") == domain]

    result = []
    for svc_domain in services:
        d = svc_domain.get("domain", "")
        domain_services: dict[str, Any] = {}
        for svc_name, svc_info in svc_domain.get("services", {}).items():
            entry: dict[str, Any] = {"description": svc_info.get("description", "")}
            fields = svc_info.get("fields", {})
            if fields:
                entry["fields"] = {k: v.get("description", "") for k, v in fields.items() if isinstance(v, dict)}
            domain_services[svc_name] = entry
        result.append({"domain": d, "services": domain_services})
    return {"count": len(result), "domains": result}


def call_service(
    domain: str,
    service: str,
    entity_id: Optional[str] = None,
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not _SERVICE_NAME_RE.match(domain):
        raise ValueError(f"Invalid domain format: {domain!r}")
    if not _SERVICE_NAME_RE.match(service):
        raise ValueError(f"Invalid service format: {service!r}")
    if domain in BLOCKED_DOMAINS:
        raise PermissionError(
            f"Service domain '{domain}' is blocked. Blocked domains: "
            f"{', '.join(sorted(BLOCKED_DOMAINS))}"
        )
    if entity_id and not _ENTITY_ID_RE.match(entity_id):
        raise ValueError(f"Invalid entity_id format: {entity_id!r}")

    payload: dict[str, Any] = {}
    if data:
        payload.update(data)
    if entity_id:
        payload["entity_id"] = entity_id

    result = _request("POST", f"/api/services/{domain}/{service}", body=payload)
    affected = []
    if isinstance(result, list):
        for s in result:
            affected.append({"entity_id": s.get("entity_id", ""), "state": s.get("state", "")})
    return {
        "success": True,
        "service": f"{domain}.{service}",
        "affected_entities": affected,
    }


def dispatch(action: str, **kwargs) -> dict[str, Any]:
    """Single entry point used by the LangChain tool wrapper."""
    if action == "list_entities":
        return list_entities(domain=kwargs.get("domain"), area=kwargs.get("area"))
    if action == "get_state":
        entity_id = kwargs.get("entity_id", "")
        if not entity_id:
            raise ValueError("get_state requires entity_id")
        return get_state(entity_id)
    if action == "list_services":
        return list_services(domain=kwargs.get("domain"))
    if action == "call_service":
        domain = kwargs.get("domain", "")
        service = kwargs.get("service", "")
        if not domain or not service:
            raise ValueError("call_service requires domain and service")
        data = kwargs.get("data")
        if isinstance(data, str):
            data = json.loads(data) if data.strip() else None
        return call_service(domain, service, entity_id=kwargs.get("entity_id"), data=data)
    raise ValueError(f"Unknown action: {action!r}")
