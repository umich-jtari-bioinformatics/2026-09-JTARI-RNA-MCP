# Learning MCP by building the JTARI server

> **How to read this.** This document was written before any application code
> existed, from a verified survey of `mcp==2.2.0` (checked 2026-09-19) and of
> the harmonized JTARI data. It is the map, not the territory. It will be
> revised at the end of each build phase so that the running example always
> matches what is actually in `src/jtari_mcp/`. Where a fact was not verified
> by running it, the text says "verify against docs". Everything else was
> either executed against the SDK or read from a live docs page on that date.

The running example throughout is the server this repo builds: 5 RNA-seq
datasets (LOCAL 234, E11304 60, E11342 90, E11834 120, FL3C 175 samples,
679 total), one shared namespace of 78,932 Ensembl gene IDs, 25 metadata
columns, one DuckDB file. Package `jtari_mcp`, script entry `jtari-mcp`,
database path in the `JTARI_DB` environment variable.

---

## 1. What MCP is, in one screen

MCP (Model Context Protocol) is a wire protocol that lets a program hand an
LLM three kinds of things in a standard shape: functions it may call, documents
it may read, and canned prompts a user may invoke. Three roles:

- **Host**: the application the human is using. Claude Desktop, Claude Code,
  Cursor. It owns the model and the conversation.
- **Client**: the connector inside the host that speaks to one server. One
  client per server. You never write this; the host ships it.
- **Server**: your process. It advertises what it has and answers requests.
  This is the only part you write.

Three primitives, each with a different owner:

| Primitive | Who decides to use it | Analogy |
|---|---|---|
| **Tool** | the model | A function whose docstring is read by a model instead of a human. |
| **Resource** | the application (or the user picking it in a UI) | A file the client can hand to the model as context without a tool call. |
| **Prompt** | the user | A saved slash-command that expands into a message. |

In the JTARI server: `get_expression` is a tool the model calls when someone
asks about ALK expression; `METADATA_SCHEMA.md` is a resource the client can
attach to the conversation so the model knows what `resistance_protocol` means
before it calls anything; "interpret this contrast conservatively" is a prompt.

What MCP is **not**: it is not an agent framework (no loop, no planning, no
memory; the host does all of that), not RAG (nothing gets embedded or
retrieved unless your tool does it), and not an API gateway (no routing, rate
limiting or billing; one server, one client, one connection). It is closer to
a language-server protocol for tools: a small JSON-RPC vocabulary and some
conventions about what the model gets to see.

Spec: https://modelcontextprotocol.io/specification/2026-07-28

---

## 2. How a request actually flows

Every message is JSON-RPC 2.0: an object with `jsonrpc: "2.0"`, a `method`,
`params`, and an `id` that pairs a response to its request. Follow one
`get_expression` call end to end.

1. **User asks.** "Show me ALK expression in H3122 across the resistant lines."
2. **Model picks a tool.** The client has already fetched the server's tool
   list. Each entry has a `name`, a `description`, and an `inputSchema` (JSON
   Schema). The host inserts those into the model's system prompt. The model
   reads them like any other instructions and decides, from the text alone,
   that `get_expression` fits and that `cell_line` should be `"H3122"`.
3. **Client sends `tools/call`.** `{"method": "tools/call", "params": {"name":
   "get_expression", "arguments": {"gene": "ALK", "filters": {...}}}}`.
4. **Server validates.** The SDK built a Pydantic model from your function's
   type hints. Arguments that fail validation never reach your code; the
   failure is returned as a tool error the model can read (section 3).
5. **Your function runs.** It queries DuckDB and returns Python objects.
6. **Server returns two views of the result.** `content` is a list of blocks
   (text, image, resource) that the model reads. `structuredContent` is the
   JSON your return type serializes to, validated against the tool's
   `outputSchema`, for an application to consume. Both are in the same
   response.
7. **Model composes the answer** from `content`, possibly calling more tools.

What the model actually sees for a tool is exactly this, emitted by
`mcp==2.2.0` for a `get_expression` stub (the wire is camelCase; the Python
attributes are snake_case, so `tool.input_schema` in code, `inputSchema` on
the wire):

```json
{
 "name": "get_expression",
 "title": "Get expression",
 "description": "Return expression for one gene.\n\n    TPM is NOT comparable across datasets; every row carries dataset and batch.\n    ",
 "inputSchema": {
  "type": "object",
  "properties": {
   "gene":  {"description": "Gene symbol, e.g. ALK", "title": "Gene", "type": "string"},
   "unit":  {"default": "tpm", "enum": ["tpm", "counts"], "title": "Unit", "type": "string"},
   "limit": {"default": 100, "description": "Max rows", "maximum": 1000, "minimum": 1, "title": "Limit", "type": "integer"}
  },
  "required": ["gene"],
  "title": "get_expressionArguments"
 },
 "outputSchema": { "...$defs.GeneRow...", "properties": {"result": {"items": {"$ref": "#/$defs/GeneRow"}, "type": "array"}}, "required": ["result"], "type": "object" },
 "annotations": {"readOnlyHint": true, "openWorldHint": false}
}
```

Notice the description: it is the docstring **verbatim**, indentation
included. That is what the model reads. Dedent your docstrings or pass
`description=` to the decorator.

### The two eras

The spec changed shape on 2026-07-28. Older clients, which today includes
Claude Desktop and Claude Code, open a connection with an `initialize`
handshake and keep a session. The current spec dropped both: every request
carries the protocol version and client capabilities in a `_meta` field, and a
new `server/discover` method replaces the handshake. `mcp` 2.x answers both
dialects from one process with no configuration. You only need to recognise
the two shapes in logs.

| | handshake era (2024-11-05 through 2025-11-25) | 2026-07-28 |
|---|---|---|
| First message | `initialize` request, then `initialized` notification | none; any request, `_meta` carries version and capabilities |
| Discovery | `tools/list`, `resources/list`, `prompts/list` | `server/discover` (mandatory) plus the lists |
| Session | `Mcp-Session-Id` header on HTTP | none |
| Result shape | `content`, `structuredContent`, `isError` | same, plus `resultType: "complete" \| "input_required"` |
| Server asks client for input | elicitation / sampling requests | tool returns `input_required`, client retries |
| Who speaks it today | Claude Desktop, Claude Code | the SDK's own `Client` (pass `mode="legacy"` to make it speak the other) |

Sources: https://py.sdk.modelcontextprotocol.io/whats-new/ and
https://modelcontextprotocol.io/specification/2026-07-28/changelog

---

## 3. A tool's description is a prompt

This is the one idea to take away. Anthropic's tool-use docs say the API
"constructs a special system prompt from the tool definitions" and inserts the
JSON Schema into it. Their best-practice guidance: "Provide extremely detailed
descriptions. This is by far the most important factor in tool performance",
covering what the tool does, when to use it and when not to, what each
parameter means, and caveats. "Aim for at least 3-4 sentences."
(https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)

So the docstring is not documentation for your teammates. It is an
instruction to the model that decides whether and how to call your function.
Write it as one.

**Bad:**

```python
@mcp.tool()
def get_expression(genes: list[str], filters: dict | None = None, unit: str = "tpm", limit: int = 5000):
    """Get expression values."""
```

The model will call this for any question with a gene in it, will guess
filter keys, will rank raw TPM across datasets, and will not know that
`unit="counts"` is fractional RSEM output.

**Good:**

```python
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_expression(
    genes: Annotated[list[str], Field(description="Gene symbols or Ensembl gene IDs (ENSG..., unversioned). A symbol that maps to several ENSG IDs is an error that tells you to call resolve_genes.", min_length=1, max_length=50)],
    filters: Annotated[dict[str, str | int | list[str | int] | None], Field(description="Metadata column -> value (equals), list (any of), or null (unrecorded), e.g. {'cell_line': ['H3122', 'H2228'], 'dataset': 'LOCAL'}. Column names and levels come from describe_metadata.")] = {},
    unit: Literal["tpm", "counts"] = "tpm",
    include_suspect: bool = False,
    limit: Annotated[int, Field(ge=1, le=5000, description="Maximum rows returned; the call fails above this instead of truncating silently.")] = 2000,
) -> ExpressionResult:
    """Return one row per (sample, gene) with the expression value joined to sample metadata.

    If you need to filter, call describe_metadata first to get valid column
    names and levels. Results include dataset, batch, cell_line,
    perturbation_class, compound, dose_nM, timepoint_h, resistance_protocol,
    derivation_replicate and identity_status on every row so you can group
    and sort them yourself.

    TPM is not comparable across datasets; every row carries dataset and batch.
    Ranking or averaging raw TPM across datasets is not meaningful, because TPM
    is normalised within each sample and library preparation differs between
    cohorts. Compare within a dataset, or use the contrast tool. Samples with
    identity_status = suspect are excluded unless include_suspect=true.
    unit="counts" returns RSEM expected_count, which is fractional and is only
    useful as input to a DE method.
    """
```

Six sentences, each one either steers a decision (when to call, what to call
first) or prevents a wrong inference (cross-dataset ranking). The guardrail
lives here and not in hope.

The pieces the SDK gives you, all verified to reach the wire:

- `Annotated[T, Field(description=...)]` puts a `description` on that
  property in `inputSchema`. `Field(ge=1, le=20000)` becomes `minimum` and
  `maximum`, which the model reads *and* which Pydantic enforces.
- `Literal["tpm", "counts"]` becomes `"enum": ["tpm", "counts"]`. The model
  sees the closed set instead of guessing spellings.
- `ToolAnnotations(read_only_hint=True, open_world_hint=False)` from
  `mcp.types` serializes under `annotations`. These are hints for the host
  UI (a read-only tool may skip a confirmation dialog); the spec says clients
  must treat them as untrusted, so they are not a security boundary.
- A Pydantic `BaseModel` return type gives an `outputSchema` and a
  `structuredContent` object; `list[Model]` gives one text block per item and
  `{"result": [...]}` as structured output. Returning a class with no
  annotations gives the model `repr()` text and is silently useless.

### Errors are prompts too

Two channels, and the distinction matters:

| You raise | Model sees | Wire |
|---|---|---|
| `ToolError("Unknown gene 'NOPE'. Call resolve_genes first.")` from `mcp.server.mcpserver.exceptions` | `is_error=True`, text `Error executing tool get_expression: Unknown gene 'NOPE'. Call resolve_genes first.` | normal result |
| any other exception | `is_error=True`, text `Error executing tool get_expression` with the message withheld; traceback in your server log | normal result |
| `MCPError(code, message)` from `mcp` | nothing; the call fails at the protocol level | JSON-RPC error |
| a `return "error: ..."` string | `is_error=False`, looks like success | normal result |

Pydantic validation failures take the first route automatically. The model
receives, for example:

```
Error executing tool get_expression: 1 validation error for get_expressionArguments
limit
  Input should be less than or equal to 1000
```

and will retry with a smaller `limit`. That is the spec's intent: tool
execution errors "contain actionable feedback that language models can use to
self-correct". So a `ToolError` message is another prompt. "Unknown gene" is a
dead end; "Unknown gene 'ALk'. Gene IDs are case-sensitive Ensembl IDs; call
resolve_genes('ALK') to get them" is a next step. The docs' rule of thumb:
could a smarter model have avoided this error? Yes means `ToolError`. No
(the database is missing, the server is misconfigured) means `MCPError`.
Never return an error as a string; it arrives marked as success.

Sources: https://py.sdk.modelcontextprotocol.io/servers/tools/ ,
https://py.sdk.modelcontextprotocol.io/servers/handling-errors/ ,
https://modelcontextprotocol.io/specification/2026-07-28/server/tools

---

## 4. Transports and lifecycle

A transport is how the JSON gets between client and server. Two matter.

**stdio.** The client spawns your process and talks over its stdin/stdout.
This is what Claude Desktop and Claude Code do for local servers. Consequences:
stdout *is* the wire, so a stray `print()` corrupts the protocol stream and
the client logs a parse error and typically gives up on the connection. Log
to stderr with stdlib `logging`; the
client captures it (Claude Desktop writes it to
`~/Library/Logs/Claude/mcp-server-<NAME>.log`). `MCPServer(...,
log_level="INFO")` calls `logging.basicConfig()` on the root logger for you.
The protocol-level `ctx.info()` family still exists but is deprecated and
emits a warning; do not build on it. One process per client session, so two
open clients mean two server processes and two DuckDB connections. There is
no network, no auth, and no port.

**Streamable HTTP.** One endpoint, by default `/mcp`, on a host and port you
choose. The client POSTs JSON-RPC to it and may receive a streamed (SSE)
response. This is the remote transport and the only place authentication
exists (section 7). Two gotchas the docs verified:

- DNS-rebinding protection is on by default and allowlists only
  `127.0.0.1`, `localhost` and `[::1]`. Behind a real hostname every request
  gets `421 Misdirected Request` until you pass
  `transport_security=TransportSecuritySettings(allowed_hosts=[...],
  allowed_origins=[...])` from `mcp.server.transport_security`. Binding
  `host="0.0.0.0"` does **not** allowlist anything.
- Transport options are arguments to `run()`, not the constructor:
  `mcp.run(transport="streamable-http", host="127.0.0.1", port=8000,
  streamable_http_path="/mcp", transport_security=...)`.
  `MCPServer("x", port=9000)` is a `TypeError`.

**SSE.** The 2024 HTTP transport. Deprecated in the spec since 2025-03-26 and
formally listed as deprecated in the current one. `mcp.run(transport="sse")`
still works for old clients. Do not build anything new on it.

Source: https://py.sdk.modelcontextprotocol.io/run/

### Lifespan: open DuckDB once

Opening a DuckDB file per tool call would be slow and silly. The lifespan is
an async context manager that runs once per process: code before `yield` is
startup, code after is shutdown, and the yielded object is reachable from
every tool.

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os

import duckdb
from mcp.server import MCPServer
from mcp.server.mcpserver import Context


@dataclass
class AppContext:
    con: duckdb.DuckDBPyConnection


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    con = duckdb.connect(os.environ["JTARI_DB"], read_only=True)   # startup
    try:
        yield AppContext(con=con)
    finally:
        con.close()                                                # shutdown


mcp = MCPServer("jtari", lifespan=app_lifespan)


@mcp.tool()
def dataset_summary(ctx: Context[AppContext]) -> list[dict]:
    """..."""
    con = ctx.request_context.lifespan_context.con
    return con.sql("select dataset, count(*) as n from samples group by 1 order by 1").fetchall()
```

The `ctx` parameter is found by its `Context` annotation. It never appears in
`inputSchema` (verified), so the model does not know it exists and cannot set
it. It may go first, keyword-only after `*`, or last with a `= None` default;
all three registered and worked. Use `Context[AppContext]` in tools so the
lifespan object is typed; in resources and prompts use bare `Context`, because
the parametrised form fails there at call time. Under Streamable HTTP the
lifespan also runs once per process, not per session, which is a change from
SDK v1.

Source: https://py.sdk.modelcontextprotocol.io/handlers/lifespan/ ,
https://py.sdk.modelcontextprotocol.io/handlers/context/

---

## 5. The JTARI server as the running example

### Phase 1: hello world, one tool, stdio

This is the whole of the first shippable server. No DuckDB, no data. The
point is to see a tool appear in Claude Desktop and get called.

```python
# src/jtari_mcp/server.py  (Phase 1 shape)
from mcp.server import MCPServer

mcp = MCPServer(
    "jtari",
    instructions="Harmonized ALK lung-cancer cell-line RNA-seq (679 samples, 5 datasets, 78,932 genes). Phase 1: inventory only.",
)


@mcp.tool()
def dataset_summary() -> list[dict]:
    """Return the dataset inventory: one row per dataset with its sample count.

    Use this first to learn which cohorts exist before asking about expression.
    Datasets are LOCAL (unpublished UMich resistant series), E11304, E11342,
    E11834 (ArrayExpress re-quantifications) and FL3C (baseline LUAD panel).
    Sample counts are not comparable across datasets in any biological sense.
    """
    return [
        {"dataset": "LOCAL", "n_samples": 234},
        {"dataset": "E11304", "n_samples": 60},
        {"dataset": "E11342", "n_samples": 90},
        {"dataset": "E11834", "n_samples": 120},
        {"dataset": "FL3C", "n_samples": 175},
    ]


def main() -> None:
    mcp.run()          # stdio


if __name__ == "__main__":
    main()
```

`main` is the `[project.scripts]` entry `jtari-mcp = "jtari_mcp.server:main"`,
so `uv run jtari-mcp` starts the server. `instructions=` is delivered to the
client too and is a good place for the one-paragraph "what this server is".

Register it in Claude Desktop by editing
`~/Library/Application Support/Claude/claude_desktop_config.json`
(Settings > Developer > Edit Config), then fully quit and reopen the app:

```json
{
  "mcpServers": {
    "jtari": {
      "command": "/Users/pulintz/.local/bin/uv",
      "args": ["run", "--frozen", "--directory", "/Users/pulintz/Code/2026-09-JTARI-RNA-MCP", "jtari-mcp"],
      "env": {"JTARI_DB": "/Users/pulintz/data/jtari.duckdb"}
    }
  }
}
```

The gotcha: **absolute path to `uv` itself.** Claude Desktop spawns servers
with a minimal `PATH`, so `"command": "uv"` fails with a not-found error that
only shows up in `~/Library/Logs/Claude/mcp.log`. `which uv` gives the path.
Use the same absolute path in the Claude Code registration so the two configs
stay identical:

```bash
claude mcp add jtari -e JTARI_DB=/Users/pulintz/data/jtari.duckdb -- \
  /Users/pulintz/.local/bin/uv run --frozen --directory /Users/pulintz/Code/2026-09-JTARI-RNA-MCP jtari-mcp
```

Everything after `--` is the launch command. `claude mcp list` and `/mcp`
inside a session show the connection state. For a committed, per-project
registration, `.mcp.json` at the repo root does the same with
`${CLAUDE_PROJECT_DIR}` expansion (shape in the SDK report, section 13).

Sources: https://modelcontextprotocol.io/docs/develop/connect-local-servers ,
https://code.claude.com/docs/en/mcp

### The full server

Six tools, three resources, one lifespan. The exact signatures live in
`PLAN.md`; this is the shape and the reasoning.

```
tools
  describe_metadata()                        column vocabulary with per-level counts, from the DB
  dataset_summary()                          inventory table, from the DB
  resolve_genes(query)                       symbol / ENSG / prefix -> gene rows (many-to-one!)
  find_samples(filters, columns, limit)      sample_id, label, requested columns
  get_expression(genes, filters, unit, limit) long table joined to metadata, hard row cap
  contrast(gene, design, ...)                per-group log2FC with n and dispersion
resources
  jtari://schema           METADATA_SCHEMA.md plus derived-column addendum   text/markdown
  jtari://datasets         dataset inventory table, rendered from the DB     text/markdown
  jtari://interpretation   what TPM, log2FC, n, identity_status mean here    text/markdown
  jtari://provenance       the provenance table (hashes, versions, build)    application/json
```

**`describe_metadata` is the vocabulary bridge.** A user says "ordered by
treatment". The model has no idea that treatment is spread over three columns.
`describe_metadata` returns, straight from DuckDB, something like:

```
compound            alectinib 142, lorlatinib 64, trametinib 40, doxycycline 30, brigatinib 24, none 379
dose_nM             LOCAL only: 0, 5, 100, 1000, 1300, 1500, 300000; NULL elsewhere (E11342 dose not in SDRF)
timepoint_h         6, 24, NULL (baseline rows and all E11834 controls)
perturbation_class  baseline 281, acute_drug 202, chronic_resistance 136, transgene_induction 60
resistance_protocol StartLow 40, StartIC50 48, Cmax 48 (independent protocols, not a ladder)
derivation_replicate BR1 48, BR2 48, BR3 40 (unit of evolutionary replication)
identity_status     unverified 599, confirmed 60, reassigned 12, suspect 8
```

Now the model can map "treatment" onto `compound` then `dose_nM` then
`timepoint_h`, knows that `dose_nM` will be NULL for the public cohorts, and
knows that `unverified` is the normal state (599 of 679) rather than a
failure. The same tool is why `find_samples` can accept arbitrary column
filters without the model guessing spellings. Every level count is a query,
never a constant, so it stays correct when the DB is rebuilt.

**`resolve_genes` when a symbol is ambiguous.** Symbols are not unique: in the
only on-disk annotation, 480 symbols map to more than one Ensembl ID and
`Y_RNA` maps to 758. That file covers just 54% of genes, so the build script
downloads the Ensembl 113 GTF instead. `get_expression` and `contrast` accept
symbols or ENSG IDs and resolve internally; an ambiguous symbol raises a
`ToolError` that names the candidates and tells the model to call
`resolve_genes`. Two tools, one clear dependency, spelled out in the
descriptions and in the error text.

**`contrast` pairing rules.** A contrast is log2FC of mean TPM between a
treated group and its own control, with `n` and a dispersion measure for
both groups. The design word picks the pairing. All pairing is within one
`dataset`, and `suspect` identities are excluded by default.

| design | numerator group | denominator (control) | notes |
|---|---|---|---|
| `chronic` (LOCAL only) | `cell_line=L, resistance_protocol=P, derivation_replicate=BR, dose_nM=0` | `cell_line=L, perturbation_class='baseline', dose_nM=0` (the parental) | One row per (P, BR). Both groups at matched dose 0, otherwise the acute drug effect contaminates the resistance signal. `parent_line` is empty for all 136 chronic rows, so pairing is by `cell_line`, not by that column. |
| `acute` | `cell_line=L, compound=C, dose_nM=D, timepoint_h=T` | `cell_line=L, compound='none'`, plus `timepoint_h=T` when that dataset's controls carry a timepoint (E11342), pooled otherwise (E11834, whose 8 controls per line have `timepoint_h` NULL); in LOCAL, `dose_nM=0, perturbation_class='baseline'` | `dose_nM` is NULL in every public-cohort row; group on it anyway so LOCAL doses separate. |
| `transgene` (E11304 only) | `construct=K, compound='doxycycline'` | `construct=K, compound='none'` | `construct` is derived at build from `matrix_sample_id`; the upstream TSV has no such column. Always return the empty-vector row too, as the dox-only null. |

**Why `contrast` never pairs across datasets.** TPM is normalised within each
sample: a gene's value depends on what else was in that library. Two cohorts
differ in library prep, sequencing depth, batch, and in E11342's case an
unrecorded dose. Taking a log2 difference *within* a dataset cancels whatever
offset is shared by the treated and control samples in that dataset. It only
cancels additive (in log space) offsets, and it only cancels them when both
sides carry the same one. Pair CUTO29 lorlatinib from E11342 against CUTO29
control from E11834 and the dataset offset does not cancel; it *is* the
result. Even two clean within-dataset log2FCs are not comparable in
magnitude across datasets, because non-additive effects (dynamic-range
compression, dose) remain. Directions are comparable. The legitimate
cross-dataset move is therefore: compute lorlatinib-vs-none inside E11342,
compute it inside E11834, and compare the two deltas. Lorlatinib at 6 h and
24 h in CUTO8, CUTO9 and CUTO29 exists in both, which is exactly the bridge
the kickoff's "across both acute cohorts" question needs. The tool refuses
the shortcut and the description explains the long way.

---

## 6. Testing an MCP server

`Client(mcp)` from `mcp` connects to your server object in-process with no
transport. It is the FastAPI `TestClient` of MCP. The SDK uses `anyio`, so
tests are `pytest.mark.anyio` with an `anyio_backend` fixture; you do not
need pytest-asyncio.

```python
# tests/test_server.py
import pytest
from mcp import Client
from jtari_mcp.server import mcp


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with Client(mcp, raise_exceptions=True) as c:
        yield c


@pytest.mark.anyio
async def test_unknown_gene_is_tool_error(client: Client):
    r = await client.call_tool("get_expression", {"genes": ["NOPE"]})
    assert r.is_error is True
    assert "resolve_genes" in r.content[0].text


@pytest.mark.anyio
async def test_schema_resource(client: Client):
    r = await client.read_resource("jtari://schema")
    assert r.contents[0].mime_type == "text/markdown"
```

Two things people get wrong:

- **`call_tool` never raises for a tool failure.** A `ToolError`, a crash
  inside your function, and a validation failure all come back as a normal
  result with `is_error=True`. Assert on `r.is_error` and on
  `r.content[0].text`. `raise_exceptions=True` only surfaces crashes
  *outside* tool bodies.
- **Results are snake_case in Python.** `r.structured_content`,
  `tool.input_schema`. The wire is camelCase.

**Fixture database.** Build a tiny DuckDB in a pytest fixture: 20 or so genes
(ALK, EML4, KIF5B, TFG, a few zeros, one duplicated symbol) and 30 or so
samples that cover every `perturbation_class`, one E11834 line with
timepoint-less controls, one E11342 line with time-split controls, one H3122
chronic derivation with all four dose-0 samples, and a `suspect` sample.
Point `JTARI_DB` at it (or pass the path to the lifespan) so every pairing
rule in section 5 has a test that would fail if the rule slipped.

**The real launch path.** The in-process client skips the transport. One
test should spawn the server the way a host does:

```python
import sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters

params = StdioServerParameters(
    command=sys.executable, args=["-m", "jtari_mcp.server"], env={"JTARI_DB": str(fixture_db)},
)
async with Client(params) as c:
    tools = await c.list_tools()
```

The child gets a minimal environment allowlist (`HOME, LOGNAME, PATH, SHELL,
TERM, USER`), so anything else, including `JTARI_DB`, must go in `env=`.
This is also the test that catches a stray `print()`.

**Poking at it interactively.** `uv run mcp dev src/jtari_mcp/server.py`
runs the server under the MCP Inspector, a web UI that lists tools, lets you
fill in arguments, and shows the raw request and response. It needs `npx`.
It looks for a module-level `mcp`, `server` or `app`. This is the fastest
way to read your own tool definitions the way the model will.

Sources: https://py.sdk.modelcontextprotocol.io/get-started/testing/ ,
https://py.sdk.modelcontextprotocol.io/run/#the-mcp-command

---

## 7. Deployment in one paragraph each

**Local stdio (Phase 1 acceptance target).** Each client launches its own
server process from the config in section 5, the lifespan opens the DuckDB
file read-only, and there is nothing to run, monitor or secure. Two clients
open two read-only connections to the same file, which DuckDB permits as long
as no process opens it for writing (verify against the DuckDB concurrency
docs before adding a writer). Rebuilding the database is a matter of
replacing the file and restarting the client.

**Lab Mac Studio, Streamable HTTP with a bearer token.** One long-running
process under `launchd`, `mcp.run(transport="streamable-http", ...)` with a
`transport_security` allowlist for the machine's hostname, TLS terminated by
a reverse proxy (then serve `mcp.streamable_http_app()` with `uvicorn
--proxy-headers --forwarded-allow-ips=<proxy>`). Authentication for a
pre-shared bearer token has two SDK-compatible shapes. The plan's v2 choice
is a small Starlette middleware on the ASGI app that checks
`Authorization: Bearer` and returns a bare 401. The MCP-native shape is a
`TokenVerifier` subclass whose `verify_token` compares the token and returns
an `AccessToken` or `None`, passed together with `AuthSettings(...)` to the
`MCPServer` constructor; the server then does the spec's 401 plus
`WWW-Authenticate` exchange. That second shape is the resource-server half
of OAuth, and it requires an `issuer_url`, so without a real authorization
server it advertises a placeholder that OAuth-discovering clients follow to
nowhere. It becomes the right slot when a real issuer (UM Okta) exists.
Either way, any client that sends a preconfigured header works. Claude Code
does: `claude mcp add --transport http jtari-lab https://<host>/mcp --header
"Authorization: Bearer ${JTARI_TOKEN}"`. **Claude Desktop custom connectors
do not**: they are contacted from Anthropic's egress range `160.79.104.0/21`
over the public internet, so a UM-network-only box fails to connect at all,
and their default auth is OAuth discovery (a static-header option exists in
beta for some organizations). Lab members on Claude Desktop instead use the
`mcp-remote` stdio bridge in `claude_desktop_config.json`, which runs on
their own Mac and forwards to the campus URL with the header, or run the
server locally against a copy of the DuckDB file.
Sources: https://py.sdk.modelcontextprotocol.io/run/authorization/ ,
https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp

**Docker for EC2 (and the Mac Studio, same image).** A layered `uv` build on
`python:3.12-slim-trixie`: copy the `uv` binary from
`ghcr.io/astral-sh/uv:0.10.9`, run `uv sync --frozen --no-install-project`
against the lockfile for a cached dependency layer, then copy the source and
`uv sync --frozen` again. The DuckDB file is a read-only
volume, never baked into the image; `JTARI_DB` and `JTARI_TOKEN` are
environment variables; `compose.yaml` adds the TLS proxy. The governance
question is not technical: LOCAL is unpublished, and EC2 means it leaves
campus. `PLAN.md` carries the comparison.
Source: https://docs.astral.sh/uv/guides/integration/docker/

---

## 8. Glossary

- **Host**: the application the user runs (Claude Desktop, Claude Code). Owns the model and the conversation.
- **Client**: the host's connector to one server. Fetches tool lists, sends `tools/call`, hands results to the model.
- **Server**: your process. Advertises tools, resources and prompts; answers requests. `MCPServer` in `mcp` 2.x.
- **Tool**: a function the *model* may call. Name, description and `inputSchema` are inserted into the model's system prompt.
- **Resource**: content the *application* may attach to the conversation, addressed by URI (`jtari://schema`). Static URI or template.
- **Resource template**: a resource URI with `{placeholders}` (`jtari://dataset/{name}`), listed under `resources/templates/list`. Placeholder names must equal the function's parameter names.
- **Prompt**: a *user*-invoked template that expands to one or more messages. The saved slash-command.
- **Transport**: how JSON-RPC bytes move. stdio or Streamable HTTP; SSE is deprecated.
- **stdio**: the client spawns the server and uses its stdin/stdout as the wire. stdout is sacred; log to stderr.
- **Streamable HTTP**: one HTTP endpoint (`/mcp`) accepting JSON-RPC POSTs with optional SSE streaming back. The remote transport; where auth lives.
- **JSON-RPC 2.0**: the message format. `method`, `params`, `id`; responses pair by `id`.
- **inputSchema**: JSON Schema for a tool's arguments, derived by Pydantic from your type hints. What the model reads to build a call.
- **outputSchema**: JSON Schema for `structuredContent`, derived from your return type. The return value is validated against it before leaving.
- **structuredContent**: the JSON form of your return value, for applications. Sits beside `content`, which is the text the model reads.
- **isError**: flag on a tool result. `true` for `ToolError`, crashes and validation failures. The model reads the accompanying text and can retry.
- **ToolError vs MCPError**: `ToolError` becomes an `isError` result the model can act on. `MCPError` becomes a JSON-RPC protocol error the model never sees. "Could a smarter model have avoided it?" picks between them.
- **Lifespan**: async context manager passed as `lifespan=`; runs once per process. Startup before `yield`, shutdown after. Where the DuckDB connection is opened.
- **Context**: the `ctx: Context[AppContext]` parameter. Gives access to `request_context.lifespan_context`, `request_id`, progress reporting and, on HTTP, headers. Invisible in `inputSchema`.
- **Annotations / readOnlyHint**: `ToolAnnotations(read_only_hint=True, ...)`. UI hints about side effects; untrusted by spec, so never a security control.
- **Protocol version**: the spec date a client and server agree on. `2026-07-28` is current and handshake-free; Claude Desktop and Claude Code still speak the 2025-era `initialize` dialect. `mcp` 2.x serves both.
- **TokenVerifier**: abstract class with one method, `verify_token(token) -> AccessToken | None`. Subclass it for bearer-token auth on Streamable HTTP; must be passed with `AuthSettings`.
- **MCP Inspector**: the web UI started by `uv run mcp dev server.py` (or `npx @modelcontextprotocol/inspector`) for listing and calling tools by hand.
- **`.mcp.json`**: Claude Code's per-project server registration file at the repo root; committed; supports `${VAR}` expansion.
- **`claude_desktop_config.json`**: Claude Desktop's server registration at `~/Library/Application Support/Claude/`. Absolute paths only. Restart the app after editing.
- **`instructions`**: the `MCPServer(..., instructions=...)` string, delivered to the client on connect. Server-level description, one paragraph.

---

## 9. Checkpoint questions

Prediction and design questions for the phase boundaries. Phase numbers
follow `PLAN.md` section 5.

### Phase 1: hello world on stdio

**Q1.** You add a `print("connected to", db_path)` at the top of
`dataset_summary` to help debug, register the server in Claude Desktop, and
ask Claude for the inventory. Predict what happens and where you would look
to diagnose it.

<details><summary>Intended answer</summary>

Under stdio, stdout is the JSON-RPC wire. The `print` emits a line that is
not valid JSON-RPC; the client logs a parse error and the connection
typically fails, so the call appears to hang or error out. Look in
`~/Library/Logs/Claude/mcp.log` (client side) and
`~/Library/Logs/Claude/mcp-server-jtari.log` (your stderr). The fix is
`logging.getLogger(__name__).info(...)`, which goes to stderr. The subprocess
test in section 6 exists to catch this before a human does.
</details>

**Q2.** Claude Desktop shows the `jtari` server as failed; Claude Code,
registered with the same `uv run --frozen --directory ... jtari-mcp` command,
works. What is the most likely difference and how would you confirm it?

<details><summary>Intended answer</summary>

Claude Desktop spawns servers with a minimal `PATH`, so a bare `"command":
"uv"` is not found. Claude Code inherited the launch command from a shell
where `uv` resolves. Confirm by reading `mcp.log` for a not-found or spawn
error and fix by using `/Users/pulintz/.local/bin/uv` (from `which uv`) in
the config. The same applies to every path in `args` and `env`.
</details>

### Phase 2: building the DuckDB file

**Q2b.** The benchmark showed the wide layout (one column per sample) is
about 20% smaller on disk and a little faster on most queries. Why does the
plan still choose the long layout (`sample_id, gene_id, tpm`)? Predict the
row count of the `expression` table.

<details><summary>Intended answer</summary>

Every tool shape is a join of `expression` to `samples` followed by a filter
or a `GROUP BY`; in long form that is one static SQL statement. In wide form
any sample filter needs a metadata query first, then dynamically generated SQL
naming hundreds of quoted columns (`E11342::CUTO29-lorla-24hr-rep1`), then an
UNPIVOT to get back to rows; adding a cohort changes the schema. Counts fit as
one extra column in long form and double the column count in wide. The speed
difference is milliseconds on queries that already finish in under 200 ms,
and the MCP round trip dominates. Row count: 78,932 genes x 679 samples =
53,594,828.
</details>

### Phase 3: the read tools

**Q3.** If `get_expression` returned the string `"Error: unknown gene NOPE"`
instead of raising `ToolError("Unknown gene 'NOPE'. Call resolve_genes
first.")`, what would the model see, and why does that matter?

<details><summary>Intended answer</summary>

A returned string is a successful result: `isError` is `false` and the text
arrives as ordinary tool output. The model has no signal that anything went
wrong and may present "Error: unknown gene NOPE" as data. With `ToolError`,
the result carries `isError: true` and the text `Error executing tool
get_expression: Unknown gene 'NOPE'. Call resolve_genes first.`; the spec says
clients should pass this to the model as actionable feedback, and the model
will typically call `resolve_genes` and retry. The error message is a prompt,
so it should name the next step. Validation failures (a `limit` above the
`Field(le=...)` bound) take the same route automatically, with Pydantic's
message.
</details>

**Q4.** A user asks "which cell line has the highest ALK expression?" and the
model calls `get_expression` with no dataset filter and sorts by TPM. The top
row is NL20 from E11304 with a mean TPM around 573. Is the model's answer
right, and what in the server should have steered it?

<details><summary>Intended answer</summary>

The number is real (NL20 with dox-induced ALK fusions has the highest mean ALK
TPM in the resource), but ranking raw TPM across datasets is not meaningful:
TPM is normalised within each sample, and library prep, depth and batch differ
by cohort, so the ordering between E11304 and SR-786 in LOCAL is partly a
dataset effect. That is why the `get_expression` description says "TPM is not
comparable across datasets; every row carries dataset and batch" and why every
row returns `dataset`. The right answer reports the top line *within each
dataset*, or says the cross-dataset ranking is unreliable. If the model still
ranked across datasets, the description needs to be sharper, because the
description is the only lever you have.
</details>

### Phase 4: contrast

**Q5.** Why does `contrast(design="acute")` refuse to pair CUTO29 lorlatinib
24 h from E11342 against CUTO29 lorlatinib 24 h from E11834, even though both
are the same line, drug and timepoint? What is the legitimate way to use both
cohorts?

<details><summary>Intended answer</summary>

They are different datasets. TPM is within-sample normalised, the two
experiments were run separately with their own libraries and depth, and
E11342's dose is not even recorded in the SDRF. A log2 difference cancels only
additive log-space offsets, and only those shared by both sides; pairing
across datasets leaves the dataset offset inside the result, so you would be
measuring the cohort, not the drug. The legitimate move is to compute
lorlatinib-vs-`none` inside E11342 (time-split controls, so the 24 h ones) and
inside E11834 (controls have no timepoint, so the pooled 8 per line), then
compare the two log2FCs. Directions are comparable across datasets;
magnitudes are not. CUTO8, CUTO9 and CUTO29 with lorlatinib at 6 h and 24 h
exist in both, which is the one deliberate cross-dataset bridge in the
resource.
</details>

**Q6.** For H2228 chronic contrasts with the default `exclude_suspect=True`,
predict what happens to the `Cmax_BR3` and `StartIC50_BR2` groups and what
the tool should say about it.

<details><summary>Intended answer</summary>

Each chronic derivation is 8 samples: 4 at dose 0 and 4 on-drug, split 2/2
across batches 1 and 2. In those two H2228 groups the batch-1 halves are
`identity_call = DFCI032, identity_status = reassigned` and the batch-2 halves
are `unresolved, suspect`. Excluding `suspect` removes the batch-2 samples,
leaving a dose-0 numerator of n = 2, both called DFCI032 by somalier, paired
against an H2228 parental. That log2FC is a line-identity difference, not a
resistance effect. The tool must report `n` and the `identity_status`
composition of each group and flag a numerator that is entirely `reassigned`.
This is also why the default excludes `suspect` only rather than restricting
to `confirmed`, which would leave 60 of 679 samples.
</details>

### Phase 5: Streamable HTTP and deployment

**Q7.** A lab member, on Claude Desktop at home, wants to use the Mac Studio
server you set up with a bearer `TokenVerifier` on the UM network. Predict
what happens and say why. What are their real options?

<details><summary>Intended answer</summary>

It does not connect, for two independent reasons. Claude Desktop custom
connectors are contacted from Anthropic's infrastructure (egress range
`160.79.104.0/21`), not from the user's laptop, so a UM-network-only server is
unreachable regardless of VPN. And even with a public HTTPS endpoint,
Desktop's default auth is OAuth discovery: it follows `authorization_servers`
in the resource metadata to the placeholder issuer URL and fails, because the
static-token pattern has no authorization server. Claude Code connects from
the user's own machine and sends a preconfigured header, so `claude mcp add
--transport http ... --header "Authorization: Bearer ..."` works from any
machine that can reach the Mac Studio. Their options: add the `mcp-remote`
stdio bridge to `claude_desktop_config.json` (it runs on their Mac, on the
U-M VPN if off campus, and forwards to the campus URL with the bearer
header); run the server locally over stdio against a copy of the DuckDB
file; use Claude Code; or wait for a public HTTPS deployment with OAuth or
the static-header beta enabled for the UM organization.
</details>
