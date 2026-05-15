import pytest
import httpx
from agents.shared.a2a_server import A2AServer, A2AHandler


@pytest.mark.asyncio
async def test_a2a_server_accepts_tasks_send_and_dispatches():
    async def echo_handler(skill_id: str, input: dict, meta: dict) -> dict:
        return {"echoed": input, "skill": skill_id}

    handler = A2AHandler(handler=echo_handler)
    server = A2AServer(handler=handler)
    await server.start()
    try:
        url = server.base_url
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{url}/a2a", json={
                "jsonrpc": "2.0", "id": "1", "method": "tasks/send",
                "params": {
                    "task_id": "1", "skill_id": "tool.read_file",
                    "input": {"path": "x"},
                    "_meta": {"authz_grant": "fake"},
                },
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["echoed"]["path"] == "x"
    finally:
        await server.stop()
