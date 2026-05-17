"""Tests for tool/tool_homeassistant.py.

Mocks the OPENER's ``open`` so no real HTTP traffic leaves the box. Covers:
- happy-path dispatch for each ``action`` (list_entities / get_state /
  list_services / call_service)
- input validation (entity_id format, service-name regex)
- blocked HA service domains (shell_command etc.)
- missing HASS_TOKEN → RuntimeError
- area / domain filtering for list_entities
"""
from __future__ import annotations

import io
import json
from contextlib import contextmanager

import pytest

from tool import tool_homeassistant
from tool.tool_homeassistant import (
    BLOCKED_DOMAINS,
    call_service,
    dispatch,
    get_state,
    list_entities,
    list_services,
)


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Mimics the urllib response context-manager interface (``.read()``)."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


@contextmanager
def _patch_opener(monkeypatch, response_factory):
    """Replace ``tool_web.OPENER.open`` with a callable that returns
    a ``_FakeResponse`` derived from the per-call ``response_factory(req)``.

    Calls are recorded on ``calls`` so tests can assert on the request
    method / URL / body."""
    from tool import tool_web

    calls: list[dict] = []

    class _FakeOpener:
        def open(self, req, timeout=None):
            body = req.data
            method = req.get_method()
            url = req.full_url
            payload = json.loads(body) if body else None
            calls.append({
                "method": method, "url": url,
                "headers": dict(req.headers),
                "payload": payload,
            })
            return _FakeResponse(response_factory(method, url, payload))

    monkeypatch.setattr(tool_web, "OPENER", _FakeOpener())
    yield calls


@pytest.fixture(autouse=True)
def _hass_env(monkeypatch):
    monkeypatch.setenv("HASS_URL", "http://homeassistant.local:8123")
    monkeypatch.setenv("HASS_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# list_entities
# ---------------------------------------------------------------------------


def test_list_entities_filters_by_domain(monkeypatch):
    states = [
        {"entity_id": "light.kitchen", "state": "on",
         "attributes": {"friendly_name": "Kitchen Light"}},
        {"entity_id": "sensor.temp", "state": "21.5",
         "attributes": {"friendly_name": "Temperature"}},
    ]
    with _patch_opener(monkeypatch, lambda *a, **kw: json.dumps(states).encode()):
        result = list_entities(domain="light")
    assert result["count"] == 1
    assert result["entities"][0]["entity_id"] == "light.kitchen"


def test_list_entities_filters_by_area_friendly_name(monkeypatch):
    states = [
        {"entity_id": "light.kitchen", "state": "on",
         "attributes": {"friendly_name": "Kitchen Light", "area": "kitchen"}},
        {"entity_id": "light.bedroom", "state": "off",
         "attributes": {"friendly_name": "Bedroom Light", "area": "bedroom"}},
    ]
    with _patch_opener(monkeypatch, lambda *a, **kw: json.dumps(states).encode()):
        result = list_entities(area="kitchen")
    assert result["count"] == 1
    assert "kitchen" in result["entities"][0]["entity_id"]


def test_list_entities_sends_authorization_header(monkeypatch):
    with _patch_opener(monkeypatch, lambda *a, **kw: b"[]") as calls:
        list_entities()
    # urllib normalises header keys via ``.capitalize()``.
    auth = calls[0]["headers"].get("Authorization")
    assert auth == "Bearer test-token"


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


def test_get_state_returns_state_and_attributes(monkeypatch):
    body = json.dumps({
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"brightness": 200},
        "last_changed": "2026-05-17T12:00:00Z",
        "last_updated": "2026-05-17T12:00:01Z",
    }).encode()
    with _patch_opener(monkeypatch, lambda *a, **kw: body):
        result = get_state("light.kitchen")
    assert result["entity_id"] == "light.kitchen"
    assert result["state"] == "on"
    assert result["attributes"]["brightness"] == 200


def test_get_state_rejects_bad_entity_id_format():
    with pytest.raises(ValueError, match="Invalid entity_id"):
        get_state("not.a.valid.id")
    with pytest.raises(ValueError, match="Invalid entity_id"):
        get_state("BadCase.entity")


# ---------------------------------------------------------------------------
# list_services
# ---------------------------------------------------------------------------


def test_list_services_filters_by_domain(monkeypatch):
    body = json.dumps([
        {"domain": "light", "services": {
            "turn_on": {"description": "Turn on", "fields": {
                "brightness": {"description": "0-255"},
            }},
        }},
        {"domain": "switch", "services": {"turn_on": {"description": ""}}},
    ]).encode()
    with _patch_opener(monkeypatch, lambda *a, **kw: body):
        result = list_services(domain="light")
    assert result["count"] == 1
    assert result["domains"][0]["domain"] == "light"
    assert "turn_on" in result["domains"][0]["services"]


# ---------------------------------------------------------------------------
# call_service
# ---------------------------------------------------------------------------


def test_call_service_blocks_dangerous_domain():
    """``shell_command`` / ``python_script`` / ``command_line`` etc. let HA
    execute arbitrary code on the host. Must be refused even with a valid
    token, even under danger-full-access."""
    for blocked in BLOCKED_DOMAINS:
        with pytest.raises(PermissionError, match="blocked"):
            call_service(blocked, "anything")


def test_call_service_rejects_bad_domain_or_service_name():
    with pytest.raises(ValueError, match="Invalid domain"):
        call_service("Bad-Domain", "turn_on")
    with pytest.raises(ValueError, match="Invalid service"):
        call_service("light", "Bad-Service!")


def test_call_service_happy_path_includes_entity_and_data(monkeypatch):
    body = json.dumps([
        {"entity_id": "light.kitchen", "state": "on"},
    ]).encode()
    with _patch_opener(monkeypatch, lambda *a, **kw: body) as calls:
        result = call_service(
            "light", "turn_on",
            entity_id="light.kitchen",
            data={"brightness": 255},
        )
    assert result["success"] is True
    assert result["service"] == "light.turn_on"
    # POST body should contain both ``data`` and the entity_id.
    sent = calls[0]["payload"]
    assert sent["brightness"] == 255
    assert sent["entity_id"] == "light.kitchen"


def test_call_service_rejects_bad_entity_id():
    with pytest.raises(ValueError, match="Invalid entity_id"):
        call_service("light", "turn_on", entity_id="bad..id")


# ---------------------------------------------------------------------------
# dispatch + missing token
# ---------------------------------------------------------------------------


def test_dispatch_routes_actions(monkeypatch):
    with _patch_opener(monkeypatch, lambda *a, **kw: b"[]"):
        out = dispatch("list_entities", domain="light")
    assert "entities" in out


def test_dispatch_unknown_action():
    with pytest.raises(ValueError, match="Unknown action"):
        dispatch("nope")


def test_dispatch_get_state_requires_entity_id():
    with pytest.raises(ValueError, match="requires entity_id"):
        dispatch("get_state")


def test_dispatch_call_service_requires_domain_and_service():
    with pytest.raises(ValueError, match="requires domain and service"):
        dispatch("call_service", domain="light")


def test_dispatch_call_service_accepts_json_string_data(monkeypatch):
    with _patch_opener(monkeypatch, lambda *a, **kw: b"[]") as calls:
        dispatch(
            "call_service",
            domain="light", service="turn_on",
            entity_id="light.kitchen",
            data='{"brightness": 128}',
        )
    assert calls[0]["payload"]["brightness"] == 128


def test_missing_hass_token_raises(monkeypatch):
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="HASS_TOKEN"):
        list_entities()
