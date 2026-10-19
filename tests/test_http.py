"""Phase 5 tests: the deployed (HTTP) mode. A real uvicorn server runs in a thread
on a free port, against the fixture database and a temporary keys file.

Checks: health without a key; 401 without or with a wrong key; a valid key works
end to end through the MCP client; revoking a key takes effect without a restart;
the Host-header guard (HTTP 421); fail closed without a keys file.
"""

import json
import os
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from jtari_mcp import auth, server
from jtari_mcp import db as db_module


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def keys(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    monkeypatch.setenv("JTARI_MCP_KEYS_FILE", str(path))
    return path


def add_key(name: str, capsys) -> str:
    assert auth.main(["add", name]) == 0
    return capsys.readouterr().out.strip().splitlines()[-1]


@pytest.fixture
def live(built, keys, capsys, monkeypatch):
    """Start the HTTP server; yield (base_url, valid_key, keys_path)."""
    monkeypatch.setenv("JTARI_DB", str(built["out"]))
    db_module.reset_db()
    key = add_key("alice", capsys)
    port = free_port()
    app = server.build_http_app(public_host="jtari.example.org")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", key, keys
    srv.should_exit = True
    thread.join(timeout=5)
    db_module.reset_db()


LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def post(base, key=None, host=None):
    headers = dict(HEADERS)
    if key:
        headers["authorization"] = f"Bearer {key}"
    if host:
        headers["host"] = host
    return httpx2.post(f"{base}/mcp", headers=headers, content=json.dumps(LIST_TOOLS), timeout=10)


def test_health_needs_no_key(live):
    base, _, _ = live
    r = httpx2.get(f"{base}/health", timeout=10)
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_no_key_and_wrong_key_are_refused(live):
    base, _, _ = live
    assert post(base).status_code == 401
    assert post(base, key="jtari_wrong").status_code == 401


def test_host_header_guard(live):
    base, key, _ = live
    assert post(base, key=key, host="evil.example.com").status_code == 421
    assert post(base, key=key, host="jtari.example.org").status_code == 200


@pytest.mark.anyio
async def test_valid_key_works_through_the_mcp_client(live):
    base, key, _ = live
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
    async with Client(streamable_http_client(f"{base}/mcp", http_client=http)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"get_expression", "contrast"} <= names
        result = await client.call_tool("resolve_genes", {"query": ["ALK"]})
        assert json.loads(result.content[0].text)["results"][0]["gene_id"] == "ENSG00000171094"


def test_revoking_a_key_takes_effect_without_restart(live):
    base, key, path = live
    assert post(base, key=key).status_code == 200
    assert auth.main(["remove", "alice"]) == 0
    os.utime(path, (time.time() + 5, time.time() + 5))  # make sure the mtime changes
    assert post(base, key=key).status_code == 401


def test_fails_closed_without_keys_file(keys):
    assert not keys.exists()
    with pytest.raises(SystemExit, match="No API keys file"):
        server.build_http_app()


def test_keys_are_stored_hashed(keys, capsys):
    key = add_key("bob", capsys)
    stored = json.loads(keys.read_text())
    assert key not in keys.read_text() and len(stored["bob"]) == 64
    assert oct(keys.stat().st_mode)[-3:] == "600"


def test_refusals_keep_a_reused_connection_clean(live):
    """Refusals must leave a kept-alive connection usable (Caddy reuses connections).
    Live behind Caddy, a refused request without reading its body gave the client an
    "incomplete chunked read". This direct-connection test passes with or without the
    body-draining fix (uvicorn copes when talking directly); it guards the behaviour,
    it does not reproduce the Caddy-specific failure."""
    base, key, _ = live
    body = json.dumps({**LIST_TOOLS, "params": {"pad": "x" * 50_000}})
    with httpx2.Client(timeout=10) as client:  # one kept-alive connection
        for _ in range(3):
            r = client.post(f"{base}/mcp", headers=HEADERS, content=body)
            assert r.status_code == 401 and r.json()["error"] == "unauthorized"
        ok = client.post(
            f"{base}/mcp", headers={**HEADERS, "authorization": f"Bearer {key}"}, content=body
        )
        assert ok.status_code == 200
