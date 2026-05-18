from __future__ import annotations

from typing import Any

import jwt as pyjwt


class AuthzError(Exception):
    pass


def verify_grant(token: str, *, key: str, requested_tool: str) -> dict[str, Any]:
    """Verify the authz_grant JWT. Returns the decoded claims on success.

    Raises AuthzError on signature failure, expiry, or tool not in allowed_tools.
    Note: `sub` is audit-only (capability delegation model); gating is purely
    via `allowed_tools` containment.
    """
    try:
        claims = pyjwt.decode(token, key, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise AuthzError("authz_grant expired") from None
    except pyjwt.InvalidSignatureError:
        raise AuthzError("authz_grant signature invalid") from None
    except pyjwt.PyJWTError as exc:
        raise AuthzError(f"authz_grant decode error: {exc}") from exc

    allowed = claims.get("allowed_tools") or []
    if requested_tool not in allowed:
        raise AuthzError(
            f"tool {requested_tool!r} not in allowed_tools {allowed!r}"
        )
    return claims
