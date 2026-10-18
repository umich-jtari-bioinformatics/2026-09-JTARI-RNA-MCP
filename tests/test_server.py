"""MCP-level tests: what a client sees. The server is pointed at the synthetic
fixture database through JTARI_DB.

Two layers:
- in-process: the test client talks to the server object directly (fast);
- stdio: the server runs as a subprocess, exactly as Claude Desktop launches it.
"""

import json
import os
import sys

import pytest
from mcp import StdioServerParameters
from mcp.client import Client

from jtari_mcp import db as db_module
from jtari_mcp import server

TOOLS = {
    "dataset_summary",
    "describe_metadata",
    "find_samples",
    "resolve_genes",
    "get_expression",
    "list_contrasts",
    "contrast",
}


@pytest.fixture
def fixture_server(built, monkeypatch):
    monkeypatch.setenv("JTARI_DB", str(built["out"]))
    db_module.reset_db()
    yield server.mcp
    db_module.reset_db()


@pytest.mark.anyio
async def test_tools_registered_read_only(fixture_server):
    async with Client(fixture_server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == TOOLS
        assert all(t.annotations.read_only_hint for t in tools.values())
        assert "NOT COMPARABLE ACROSS DATASETS" in tools["get_expression"].description
        schema = tools["get_expression"].input_schema["properties"]
        assert schema["genes"]["maxItems"] == 50
        assert schema["exclude_identity_status"]["default"] == ["suspect"]


@pytest.mark.anyio
async def test_instructions_carry_the_rules(fixture_server):
    async with Client(fixture_server) as client:
        for rule in ("comparable within a dataset only", "DIRECTION", "NOT parental"):
            assert rule in client.instructions


@pytest.mark.anyio
async def test_get_expression_returns_compact_json(fixture_server):
    async with Client(fixture_server) as client:
        result = await client.call_tool("get_expression", {"genes": ["ALK"]})
        assert not result.is_error
        text = result.content[0].text
        assert ", " not in text[:50]  # compact separators, not indented JSON
        assert json.loads(text)["complete"] is True
        assert result.structured_content is None  # no duplicate copy of the data


@pytest.mark.anyio
async def test_expected_errors_reach_claude_with_a_hint(fixture_server):
    async with Client(fixture_server) as client:
        result = await client.call_tool("find_samples", {"filters": {"compound": "lorlatnib"}})
        assert result.is_error
        assert "Closest values: lorlatinib" in result.content[0].text


@pytest.mark.anyio
async def test_schema_rejects_out_of_range_arguments(fixture_server):
    async with Client(fixture_server) as client:
        result = await client.call_tool("get_expression", {"genes": ["ALK"], "limit": 99999})
        assert result.is_error


@pytest.mark.anyio
async def test_missing_database_is_reported_not_crashed(tmp_path, monkeypatch):
    monkeypatch.setenv("JTARI_DB", str(tmp_path / "absent.duckdb"))
    db_module.reset_db()
    async with Client(server.mcp) as client:
        result = await client.call_tool("dataset_summary", {})
        assert result.is_error and "jtari-build-db" in result.content[0].text
    db_module.reset_db()


@pytest.mark.anyio
async def test_resources(fixture_server):
    async with Client(fixture_server) as client:
        uris = {str(r.uri) for r in (await client.list_resources()).resources}
        assert uris == {
            "jtari://docs/schema",
            "jtari://datasets/inventory",
            "jtari://docs/interpretation",
            "jtari://provenance",
        }
        schema = (await client.read_resource("jtari://docs/schema")).contents[0].text
        assert "Unified sample metadata" in schema and "effective_cell_line" in schema


@pytest.mark.anyio
async def test_over_stdio_subprocess(built):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jtari_mcp.server"],
        env={**os.environ, "JTARI_DB": str(built["out"])},
    )
    async with Client(params, read_timeout_seconds=30) as client:
        assert "JTARI" in client.instructions
        result = await client.call_tool("resolve_genes", {"query": ["ALK"]})
        assert json.loads(result.content[0].text)["results"][0]["gene_id"] == "ENSG00000171094"
