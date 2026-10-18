"""Smoke test against a deployed server. Skipped unless both are set:
    JTARI_REMOTE_URL=https://<host>/mcp  JTARI_REMOTE_KEY=<your key>
Run after every deploy:  uv run pytest -q tests/test_deployed.py
"""

import json
import os

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

URL = os.environ.get("JTARI_REMOTE_URL")
KEY = os.environ.get("JTARI_REMOTE_KEY")
pytestmark = pytest.mark.skipif(
    not (URL and KEY), reason="set JTARI_REMOTE_URL and JTARI_REMOTE_KEY"
)


def test_health_and_refusal():
    base = URL.rsplit("/mcp", 1)[0]
    assert httpx2.get(f"{base}/health", timeout=20).status_code == 200
    r = httpx2.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=20)
    assert r.status_code == 401


@pytest.mark.anyio
async def test_tools_answer_with_a_key():
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {KEY}"})
    async with Client(streamable_http_client(URL, http_client=http)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"get_expression", "contrast", "list_contrasts"} <= names
        result = await client.call_tool("resolve_genes", {"query": ["ALK"]})
        assert json.loads(result.content[0].text)["results"][0]["gene_id"] == "ENSG00000171094"
        summary = json.loads((await client.call_tool("dataset_summary", {})).content[0].text)
        print("datasets served:", [d["dataset"] for d in summary["datasets"]])
