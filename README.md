# 2026-09-JTARI-RNA-MCP

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants (Claude Desktop,
Claude Code and other MCP clients) answer questions about the **JTARI harmonized ALK
cell-line RNA-seq resource** (University of Michigan): expression levels across samples, and
effect comparisons such as "log2 fold change of DUSP6 under acute lorlatinib in each CUTO line".

The server exposes primitives and vocabulary; the assistant composes them. It is read-only.

## The resource

Five bulk RNA-seq cohorts of lung adenocarcinoma and other ALK+ cell lines, all quantified
with the same pipeline (nf-core/rnaseq 3.14.0, STAR + RSEM, Ensembl GRCh38 release 113), so
they share one gene namespace, plus one unified sample-metadata table.

| Dataset | Content | Samples |
|---|---|---|
| E-MTAB-11304 | NL20 isogenic: dox-inducible EML4-ALK V1/V3, KIF5B-ALK, TFG-ALK, empty vector, ± dox | 60 |
| E-MTAB-11342 | CUTO8/9/29, YU1077 × alectinib / brigatinib / lorlatinib × 6 h / 24 h | 90 |
| E-MTAB-11834 | CUTO8/9/29, H2228, H3122 × lorlatinib / trametinib × 6 h / 24 h | 120 |
| FL3C | Lung cell-line panel, baseline | 175 |
| LOCAL | Unpublished chronic-alectinib resistance series (not distributed) | 234 |

**No expression data or metadata is in this repository.** The build script reads them from
a local data directory; the built database is gitignored.

## Tools

| Tool | Answers |
|---|---|
| `dataset_summary` | what each dataset contains, and its caveats |
| `describe_metadata` | metadata columns and their values with counts |
| `find_samples` | samples matching metadata filters |
| `resolve_genes` | gene symbol / Ensembl ID / prefix → genes (ambiguity reported) |
| `get_expression` | per-sample TPM or counts, or per-group summaries |
| `list_contrasts` | the predefined valid comparisons |
| `contrast` | log2 fold change of mean log2(TPM+1) with a Welch 95% CI, n and caveat flags |

Every comparison is defined in advance, at build time, within one dataset and one cell line.
Resistance is compared at matched dose. Identity QC (somalier) is respected: suspect samples
are excluded by default, and every result carries caveat flags. The rules Claude follows are
written into the server instructions and tool descriptions (`src/jtari_mcp/server.py`).

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/). See [docs/SETUP.md](docs/SETUP.md).

```bash
uv sync
uv run jtari-fetch-reference                                # Ensembl r113 GTF, checksum-verified
uv run jtari-build-db --data-dir /path/to/raw/data          # → data/jtari.duckdb
uv run jtari-build-db --data-dir /path/to/raw/data --exclude-dataset LOCAL   # public datasets only
uv run python -m jtari_mcp.server                           # stdio, for a local MCP client
```

The data directory holds `sample_metadata.tsv` and one folder per dataset with
`rsem.merged.gene_tpm.tsv` and `rsem.merged.gene_counts.tsv`.

**Claude Code:**
`claude mcp add jtari -- uv run --directory /path/to/this/repo python -m jtari_mcp.server`

**Claude Desktop:** add the same command to `claude_desktop_config.json`, using absolute paths.

## Deployment

HTTP mode (`--http`) with per-person API keys (`uv run jtari-keys add <name>`), fail closed
without a keys file. Docker + Caddy (HTTPS) on AWS EC2: see [deploy/README.md](deploy/README.md).

## Tests

```bash
uv run pytest -q                       # synthetic fixtures; no real data needed
uv run python tests/evals/run_evals.py --list   # answer evals through headless Claude (needs data)
```

## Learning MCP

[docs/LEARNING.md](docs/LEARNING.md) explains MCP (tools, resources, transports, why a tool
description is a prompt), using this server as the running example.

## License and citation

MIT, see [LICENSE](LICENSE). Citation metadata in [CITATION.cff](CITATION.cff).
