"""Standalone probe for the QQ C2C send endpoint.

Sends a single passive-reply POST to ``/v2/users/{openid}/messages`` using
credentials from ``gateways.json``. Prints every step with timing so the
user can see exactly where the call gets stuck.

Usage:
    python -m gateway._probe_qq
        - Uses saved gateways.json credentials
        - You must supply USER_OPENID and MSG_ID from a recent log line:

    python -m gateway._probe_qq USER_OPENID MSG_ID
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid

import httpx

from gateway import credentials as gw_creds
from gateway.qq import _coerce, _msg_seq

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
# Quiet noisy libs
logging.getLogger("httpcore").setLevel(logging.INFO)


async def main(user_openid: str, msg_id: str) -> int:
    cfg = _coerce(gw_creds.load("qq"))
    print(f"[probe] using api_base={cfg['api_base']}")
    print(f"[probe] target user_openid={user_openid}")
    print(f"[probe] target msg_id={msg_id[:60]}...")

    async with httpx.AsyncClient() as client:
        print("[probe] fetching access_token...")
        t0 = time.monotonic()
        resp = await client.post(
            "https://bots.qq.com/app/getAppAccessToken",
            json={
                "appId": cfg["app_id"],
                "clientSecret": cfg["client_secret"],
            },
            timeout=10.0,
        )
        token = resp.json().get("access_token")
        print(f"[probe] got token in {time.monotonic()-t0:.2f}s: {token[:16]}...")

        body = {
            "msg_type": 0,
            "content": "PROBE TEST " + uuid.uuid4().hex[:6],
            "msg_id": msg_id,
            "msg_seq": _msg_seq(),
        }
        url = f"{cfg['api_base']}/v2/users/{user_openid}/messages"
        print(f"[probe] POST {url}")
        print(f"[probe] body={json.dumps(body, ensure_ascii=False)}")

        t1 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                client.post(
                    url,
                    headers={
                        "Authorization": f"QQBot {token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            print(f"[probe] HARD TIMEOUT after {time.monotonic()-t1:.2f}s -- asyncio.wait_for fired")
            return 1
        except httpx.TimeoutException as exc:
            print(f"[probe] httpx timeout after {time.monotonic()-t1:.2f}s: {exc}")
            return 2
        except Exception as exc:
            print(f"[probe] other error after {time.monotonic()-t1:.2f}s: {exc!r}")
            return 3

        elapsed = time.monotonic() - t1
        print(f"[probe] response in {elapsed:.2f}s: status={resp.status_code}")
        try:
            print(f"[probe] body: {json.dumps(resp.json(), ensure_ascii=False, indent=2)}")
        except Exception:
            print(f"[probe] body (raw): {resp.text}")
        return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python -m gateway._probe_qq <USER_OPENID> <MSG_ID>")
        print()
        print("Get both from a recent 'qq: received C2C_MESSAGE_CREATE' log line.")
        print("Note: msg_id is only valid for ~5 minutes after the event.")
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
