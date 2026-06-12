from __future__ import annotations

from orchestrator.repl_gateway_commands import GatewayCommands


def test_allowed_users_in_field_specs():
    for platform in ("feishu", "qq"):
        names = [spec[0] for spec in GatewayCommands._gw_field_specs(platform, {})]
        assert "allowed_users" in names
        # It must be optional (blank allowed) so existing setups don't break.
        spec = next(
            s for s in GatewayCommands._gw_field_specs(platform, {})
            if s[0] == "allowed_users"
        )
        assert spec[3] is True  # is_optional


def test_allowed_users_in_overview_fields():
    for platform in ("feishu", "qq"):
        assert "allowed_users" in GatewayCommands._gw_fields(platform)
