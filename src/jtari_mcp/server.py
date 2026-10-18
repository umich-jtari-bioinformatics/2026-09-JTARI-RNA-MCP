"""The MCP server: tool registration, server instructions, resources, and
transport startup. The query logic lives in tools.py; this file only adapts it
to MCP (argument schemas, descriptions, errors, JSON replies).

Run it the way a client does:
    uv run python -m jtari_mcp.server            # stdio (Claude Desktop / Claude Code, local)
    uv run python -m jtari_mcp.server --http     # HTTP at http://127.0.0.1:8000/mcp, needs keys

HTTP settings (environment): JTARI_MCP_HOST (bind address, default 127.0.0.1),
JTARI_MCP_PORT (8000), JTARI_MCP_PUBLIC_HOST (the public hostname, e.g. the EC2
sslip.io name), JTARI_MCP_KEYS_FILE (see auth.py), JTARI_MCP_NO_AUTH=1 (local tests only).
"""

import json
import logging
import os
import sys
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from jtari_mcp import __version__, resources, tools
from jtari_mcp.db import DatabaseError, get_db

# In stdio mode, stdout IS the protocol channel: every byte written there must be
# a JSON-RPC message, so a stray print() corrupts the stream and the client drops
# the connection. Logs go to stderr, which Claude Desktop saves to its log files.
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("JTARI_MCP_LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("jtari_mcp")

# Sent to the client when it connects; Claude reads it before choosing any tool.
# Rules are written as definitions: where a rule is left undefined, Claude invents one.
INSTRUCTIONS = """\
JTARI harmonized ALK cell-line RNA-seq (University of Michigan). 679 bulk RNA-seq
samples of cell lines in 5 datasets: LOCAL (unpublished chronic-alectinib
resistance series in H2228/H3122 plus acute alectinib arms and baseline lines),
E11304 (NL20 ALK-fusion transgenes +/- doxycycline), E11342 and E11834 (acute
ALK/MEK inhibitor time courses in CUTO and other lines), FL3C (lung cell-line
baseline panel). All were quantified with nf-core/rnaseq 3.14.0 (STAR+RSEM) on
Ensembl GRCh38 r113, so gene IDs are shared. Cell lines only; no patient data.
A deployment may serve only some of these datasets: dataset_summary lists the ones
available. If a dataset named here is missing, say it is not available on this server.

How to work:
- Call describe_metadata before filtering on a column you have not seen, and use
  the exact values it lists. dataset_summary gives each dataset's design and caveats.
- Resolve gene symbols with resolve_genes and pass gene_id onward when a symbol
  is ambiguous.
- get_expression returns one row per sample; use group_by for per-group means
  instead of paging through many samples.
- For "what does X do to gene Y" questions, call list_contrasts to find the
  predefined comparisons, then contrast for log2 fold changes with n and 95% CI.
  Use level='protocol' to summarise resistance across independent derivations (BR).

Rules (definitions, not suggestions):
- TPM is comparable within a dataset only. Never pool or average raw TPM across
  datasets, and always name the dataset next to any number. Ranking raw TPM across
  datasets is allowed only when each dataset's HIGHEST group mean differs by 10-fold
  or more from the next dataset's highest group mean AND you state the design
  reason (e.g. transgene overexpression vs endogenous) AND the gene is not one whose
  measurement depends on library preparation (histone, mitochondrial or other
  non-polyadenylated transcripts); give the caveat first.
  Otherwise show per-dataset values side by side without ranking them.
- Across datasets, compare only the DIRECTION of effects, never their size.
- A comparison of conditions is valid only within one dataset and one cell line;
  the contrast tool enforces this. Never compute fold changes yourself from
  get_expression values across datasets or cell lines.
- When a summary groups samples of different conditions (n_conditions > 1, e.g.
  parental and resistant, or drug and control), either split by condition or say
  plainly that the value is pooled over conditions.
- contrast results are descriptive effect sizes (no p-values). Report log2fc with
  its CI and n, and mention the flags that apply; the result's notes explain each
  flag (e.g. untimed_control, vehicle_unknown, identity_reassigned, derivation_split:X
  naming the other cell line X,
  dose_suspect, BR_unusable).
- In the chronic series, 0 nM derivative samples are resistant cells off drug,
  NOT parental. Parental = chronic_state 'parental'.
- resistance_protocol values (StartLow, StartIC50, Cmax) are independent
  protocols, not a dose ladder. derivation_replicate (BR1-3) is the independent
  unit; 'replicate' is a library replicate and unreliable for pairing in LOCAL.
- Suspect-identity samples are excluded by default. 'unverified' means never
  tested, not doubtful. When identity_mismatch is true, report identity_call
  alongside cell_line.
- An empty dose_nM means unrecorded, not zero. Public-dataset doses are unrecorded.
- In E11304, doxycycline itself changes expression; EV +/- dox is the null.
- 'none' controls in E11342/E11834 may be untreated or vehicle.
- Report n with every summary. Never infer values a tool did not return, and
  never quote totals from a result whose 'complete' is false.
"""

mcp = MCPServer(
    name="jtari-alk-rnaseq",
    title="JTARI ALK cell-line RNA-seq",
    instructions=INSTRUCTIONS,
    version=__version__,
)

# Annotations are hints the client may show or act on (e.g. skip a confirmation
# prompt for a read-only tool). Every tool in this server only reads.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
# Claude Code saves results above 50,000 characters to a file unless the tool
# raises this limit; our replies are capped at about 60,000.
LARGE_RESULT = {"anthropic/maxResultSizeChars": 200_000}


def reply(result: dict) -> TextContent:
    """Compact JSON. Returning a dict or pydantic model instead would make the SDK
    send the data twice (indented text plus structured content)."""
    return TextContent(
        type="text", text=json.dumps(result, separators=(",", ":"), allow_nan=False, default=str)
    )


def run(fn, *args, **kwargs) -> TextContent:
    """Call a tools.py function and translate expected failures into ToolError,
    whose message Claude sees (any other exception shows only a generic error)."""
    try:
        return reply(fn(get_db(), *args, **kwargs))
    except (tools.QueryError, DatabaseError) as e:
        raise ToolError(str(e)) from e


# Shared parameter types. Field descriptions and limits become part of the
# input schema Claude sees, and the SDK rejects out-of-range values for us.
Filters = Annotated[
    dict[str, str | int | bool | None | list[str | int | bool | None]] | None,
    Field(
        description=(
            "Metadata column -> value or list of values (AND across columns, OR within a "
            'list), e.g. {"dataset": "E11834", "cell_line": ["CUTO8", "H3122"], '
            '"compound": "lorlatinib"}. null matches empty. "notes" matches a substring. '
            "Valid columns and values: describe_metadata."
        )
    ),
]
ExcludeIdentity = Annotated[
    list[Literal["suspect", "reassigned", "unverified", "confirmed"]],
    Field(description="identity_status values to leave out. Default excludes suspect only."),
]
OrderBy = Annotated[
    list[str] | None,
    Field(
        description=(
            "Columns to sort by, each optionally followed by ' desc', e.g. "
            '["compound", "dose_nM desc"]. Empty values and "none" sort first. Default: '
            "dataset, cell_line, chronic_state, resistance_protocol, derivation_replicate, "
            "construct, compound, dose_nM, timepoint_h, sample_id."
        )
    ),
]
Offset = Annotated[int, Field(ge=0, description="Row to start from (use next_offset to page).")]


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def dataset_summary() -> TextContent:
    """Inventory of the five harmonized datasets: accession, sample and cell-line
    counts, design, known caveats (e.g. untimed controls in E11834, unrecorded doses
    in public sets), and per-dataset counts of perturbation class, compound, dose,
    timepoint, batch and identity status. Call it first when a question spans
    datasets or you need to know which dataset holds a design. No arguments."""
    return run(tools.dataset_summary)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def describe_metadata(
    columns: Annotated[
        list[str] | None, Field(description="Columns to describe; default all.")
    ] = None,
    dataset: Annotated[
        str | None, Field(description="Count levels within one dataset only.")
    ] = None,
) -> TextContent:
    """The sample-metadata vocabulary: every column, what it means, and its values
    with counts (empty shown as null), read live from the database. Use it to map a
    user's words onto columns and exact values before filtering, e.g. "ordered by
    treatment" -> compound, dose_nM, timepoint_h; "resistant lines" ->
    perturbation_class 'chronic_resistance'; "parental" -> chronic_state 'parental'.
    dose_nM is an integer in nM (0 = no drug; empty = unrecorded); timepoint_h is hours."""
    return run(tools.describe_metadata, columns, dataset)


@mcp.tool(annotations=READ_ONLY, structured_output=False, meta=LARGE_RESULT)
def find_samples(
    filters: Filters = None,
    columns: Annotated[
        list[str] | None,
        Field(description="Metadata columns to return besides sample_id, label and identity."),
    ] = None,
    exclude_identity_status: ExcludeIdentity = ["suspect"],  # noqa: B006 (schema default)
    order_by: OrderBy = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    offset: Offset = 0,
) -> TextContent:
    """Find samples by metadata. Returns a CSV table (table_csv) with sample_id (the
    global key, "{dataset}::{matrix_sample_id}"), label (display only, never a key),
    the identity columns, and any requested columns, plus n_rows_total, complete and
    next_offset for paging. Samples with identity_status 'suspect' are excluded unless
    you pass exclude_identity_status=[]. Use describe_metadata for valid columns and
    values; an unknown column or value returns an error naming the closest valid ones."""
    return run(
        tools.find_samples, filters, columns, exclude_identity_status, order_by, limit, offset
    )


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def resolve_genes(
    query: Annotated[
        list[str],
        Field(
            description="Gene symbols, Ensembl IDs (ENSG..., version optional) or symbol "
            "prefixes, e.g. ['ALK', 'ENSG00000141510', 'DUSP'].",
            min_length=1,
            max_length=50,
        ),
    ],
    limit_per_query: Annotated[int, Field(ge=1, le=50)] = 10,
) -> TextContent:
    """Map gene symbols, Ensembl gene IDs or symbol prefixes to genes: gene_id,
    gene_symbol, gene_biotype, description, and match type (gene_id, symbol, prefix,
    none). Matching is case-insensitive: exact ID, then exact symbol, then prefix.
    Some symbols name several Ensembl genes (is_ambiguous=true); show the user the
    candidates rather than choosing silently, and pass the chosen gene_id onward.
    About 36,000 genes have no symbol and are reachable only by gene_id."""
    return run(tools.resolve_genes, query, limit_per_query)


@mcp.tool(annotations=READ_ONLY, structured_output=False, meta=LARGE_RESULT)
def get_expression(
    genes: Annotated[
        list[str],
        Field(
            description="Up to 50 gene_ids or unambiguous symbols, e.g. ['ALK', 'DUSP6'].",
            min_length=1,
            max_length=50,
        ),
    ],
    filters: Filters = None,
    unit: Annotated[
        Literal["tpm", "counts"],
        Field(description="'tpm' (default) or 'counts' (RSEM expected counts)."),
    ] = "tpm",
    exclude_identity_status: ExcludeIdentity = ["suspect"],  # noqa: B006 (schema default)
    order_by: OrderBy = None,
    group_by: Annotated[
        list[str] | None,
        Field(
            description="Summarise per group instead of per sample (n, mean, sd, median, "
            "min, max per gene; at most 10 genes), e.g. ['cell_line', 'compound', "
            "'timepoint_h']. dataset is always added as the first key, and cell_line groups "
            "use effective_cell_line (molecular identity)."
        ),
    ] = None,
    columns: Annotated[
        list[str] | None,
        Field(description="Extra metadata columns to include, e.g. ['label', 'notes']."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=2000)] = 1000,
    offset: Offset = 0,
) -> TextContent:
    """Expression of up to 50 genes per sample, joined to sample metadata: one row
    per sample, one column per gene, as CSV (table_csv). unit='tpm' (default;
    transcripts per million, a within-sample proportion) or 'counts' (RSEM expected
    counts: raw and depth-dependent, only meaningful inside a normalising model).
    TPM IS NOT COMPARABLE ACROSS DATASETS: every row carries dataset and batch.
    Never average raw TPM across datasets; rank across datasets only under the
    10-fold rule in the server instructions. For effects, use contrast. Use group_by for
    per-group summaries (groups never span datasets). Suspect-identity samples are
    excluded by default. Long results are paged: check complete and next_offset."""
    return run(
        tools.get_expression,
        genes,
        filters,
        unit,
        exclude_identity_status,
        order_by,
        group_by,
        columns,
        limit,
        offset,
    )


ContrastFilters = Annotated[
    dict[str, str | int | None | list[str | int | None]] | None,
    Field(
        description=(
            "Contrast column -> value or list, e.g. "
            '{"kind": "acute", "compound": "lorlatinib", "cell_line": ["CUTO8", "CUTO9"]}. '
            "Columns: contrast_id, kind, dataset, cell_line, compound, dose_nM, timepoint_h, "
            "resistance_protocol, derivation_replicate, derivative_id, construct, flags."
        )
    ),
]


@mcp.tool(annotations=READ_ONLY, structured_output=False, meta=LARGE_RESULT)
def list_contrasts(
    filters: ContrastFilters = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 200,
    offset: Offset = 0,
) -> TextContent:
    """List the predefined comparisons (CSV): contrast_id, kind, dataset, cell_line
    (molecular identity), compound, dose_nM, timepoint_h, resistance_protocol,
    derivation_replicate, construct, what the test and reference groups are, n per
    side, and caveat flags. Kinds:
    - acute: drug x dose x time vs untreated controls of the same line and dataset
      (time-matched where they exist; E11834 controls are untimed: flag untimed_control;
      LOCAL arms and controls have no timepoint: flag timepoint_unrecorded).
    - chronic_resistance: resistant derivative at 0 nM vs parental at 0 nM (the
      resistance state at matched dose).
    - chronic_drug_dependence: derivative on drug vs the same derivative at 0 nM.
    - transgene: +dox vs -dox within an E11304 construct (EV row = doxycycline null).
    - transgene_dox_corrected: a fusion's dox effect minus the EV dox effect.
    Contrasts never span datasets or cell lines. "Resistant on drug vs parental" is
    not offered: it mixes resistance with the acute drug effect."""
    return run(tools.list_contrasts, filters, limit, offset)


@mcp.tool(annotations=READ_ONLY, structured_output=False, meta=LARGE_RESULT)
def contrast(
    genes: Annotated[
        list[str],
        Field(
            description="Up to 20 gene_ids or unambiguous symbols, e.g. ['DUSP6', 'ALK'].",
            min_length=1,
            max_length=20,
        ),
    ],
    contrast_ids: Annotated[
        list[str] | None, Field(description="Contrast ids from list_contrasts.")
    ] = None,
    filters: ContrastFilters = None,
    level: Annotated[
        Literal["derivative", "protocol"],
        Field(
            description="'derivative' (default): one row per contrast. 'protocol': chronic "
            "contrasts summarised across derivation replicates BR1-3 (n_br = independent "
            "derivations); other kinds unchanged."
        ),
    ] = "derivative",
    exclude_identity_status: ExcludeIdentity = ["suspect"],  # noqa: B006 (schema default)
    order_by: OrderBy = None,
    limit: Annotated[int, Field(ge=1, le=2000)] = 1000,
    offset: Offset = 0,
) -> TextContent:
    """Effect of perturbations on genes: for each gene x predefined contrast, log2
    fold change of mean log2(TPM+1) between test and reference groups, with a Welch
    95% CI, SE, n per side, group means and SDs, mean TPM per side, batch composition
    and caveat flags. Select contrasts by contrast_ids or filters, e.g.
    {"kind": "acute", "compound": "lorlatinib", "dataset": ["E11342", "E11834"]} or
    {"kind": "chronic_resistance", "cell_line": ["H3122", "H2228"]}. This is a
    descriptive effect size, not a differential-expression test: no p-values and no
    multiple-testing correction; n is often 2-8, so always report n and the CI.
    Every contrast is within one dataset and one cell line; rows from different
    datasets may be shown side by side, but compare their direction, not their size.
    Suspect-identity samples are excluded by default."""
    return run(
        tools.contrast,
        genes,
        contrast_ids,
        filters,
        level,
        exclude_identity_status,
        order_by,
        limit,
        offset,
    )


def _resource_text(fn) -> str:
    try:
        return fn(get_db())
    except DatabaseError as e:
        return f"Database unavailable: {e}"


@mcp.resource(
    "jtari://docs/schema",
    mime_type="text/markdown",
    description="Sample-metadata schema (METADATA_SCHEMA.md) plus the server's derived columns.",
)
def schema_resource() -> str:
    return _resource_text(resources.schema_doc)


@mcp.resource(
    "jtari://datasets/inventory",
    mime_type="text/markdown",
    description="The five datasets: accession, size, design and caveats.",
)
def inventory_resource() -> str:
    return _resource_text(resources.inventory)


@mcp.resource(
    "jtari://docs/interpretation",
    mime_type="text/markdown",
    description="How to read TPM, counts, n, identity flags, controls and batch.",
)
def interpretation_resource() -> str:
    return resources.INTERPRETATION


@mcp.resource(
    "jtari://provenance",
    mime_type="text/markdown",
    description="How the database was built: input file checksums, versions, genome release.",
)
def provenance_resource() -> str:
    return _resource_text(resources.provenance)


def build_http_app(keys_path=None, public_host: str | None = None, no_auth: bool = False):
    """The Streamable HTTP app, wrapped in per-person key checks.

    Fails closed: without a keys file the server refuses to start, unless auth is
    explicitly disabled for local testing."""
    from jtari_mcp.auth import BearerKeyMiddleware, KeyStore, keys_file

    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    if public_host:
        # Without the public name here, requests forwarded by Caddy get HTTP 421.
        hosts += [public_host, f"{public_host}:*"]
    app = mcp.streamable_http_app(
        stateless_http=True,  # spec 2026-07-28 has no sessions; each request stands alone
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts
        ),
    )
    if no_auth:
        log.warning("HTTP auth DISABLED (JTARI_MCP_NO_AUTH=1): local testing only")
        return app
    path = keys_path or keys_file()
    if not path.exists():
        raise SystemExit(
            f"No API keys file at {path}. Create one with `uv run jtari-keys add <name>`, "
            "or set JTARI_MCP_NO_AUTH=1 for local testing only."
        )
    return BearerKeyMiddleware(app, KeyStore(path))


def main() -> None:
    if "--http" not in sys.argv:
        mcp.run(transport="stdio")
        return
    import uvicorn

    logging.getLogger().setLevel(os.environ.get("JTARI_MCP_LOG_LEVEL", "INFO"))
    get_db()  # fail fast: open and check the database before accepting requests
    app = build_http_app(
        public_host=os.environ.get("JTARI_MCP_PUBLIC_HOST", "").strip() or None,
        no_auth=os.environ.get("JTARI_MCP_NO_AUTH") == "1",
    )
    uvicorn.run(
        app,
        host=os.environ.get("JTARI_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("JTARI_MCP_PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("JTARI_MCP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
