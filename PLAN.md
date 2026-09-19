# PLAN.md: JTARI harmonized ALK cell-line expression MCP server

Status: **draft for approval**. No application code exists yet. Approve by merging the `planning` PR (or by saying so in chat); the open decisions in section 8 can be answered in the PR or in chat before Phase 0 starts.

Written 2026-09-19 by the orchestrator from four subagent reports (legacy analyst, data/DB engineer, MCP SDK researcher, deployment engineer) and the teacher's `docs/LEARNING.md`. Every number below was verified against the files in `~/Projects/patient-avatars` or against a live source on that date; the verification scripts sit in the session scratchpad and will be folded into `tests/` in Phase 2.

## 0. What changed relative to the kickoff prompt

Three things the kickoff assumed are no longer true and reshape the plan:

1. **The SDK moved.** `mcp` is at 2.2.0 (2026-09-07). `FastMCP` was renamed `MCPServer` (`from mcp.server import MCPServer`); `mcp.server.fastmcp` raises `ModuleNotFoundError`. The current spec is 2026-07-28 (no `initialize` handshake, no sessions); `mcp` 2.x serves it and the 2025-era clients (Claude Desktop and Claude Code today) from one process. We build on `mcp>=2.2,<3`. Details and sources: `docs/LEARNING.md` sections 2 and 4.
2. **`parent_line` cannot drive chronic pairing.** It is empty for all 136 `chronic_resistance` rows. Pairing is derived from `cell_line` within `dataset` (section 4.5). There is also no `DFCI032-like` cell line; those samples are recorded as `H2228` with `identity_call = DFCI032`.
3. **Claude Desktop cannot reach a campus-only server.** Custom connectors originate from Anthropic's cloud (`160.79.104.0/21`) and reject private/CGNAT addresses. A Mac Studio on the UM network works for Claude Code (connects from the user's machine) and for Claude Desktop only through the `mcp-remote` stdio bridge. This changes the deployment recommendation (section 6).

Smaller corrections: 679 metadata rows (not ~692), 78,932 genes (not 78,933), FL3C is 60 lines (not ~97), the two LOCAL TPM files are byte-identical, E11834 controls have no timepoint, public-cohort controls have empty dose (0 exists only in LOCAL). Full list in section 1.

Repo: `umich-jtari-bioinformatics/2026-09-JTARI-RNA-MCP` (already created, MIT LICENSE and README present; the kickoff's `2026-09-jtari-model-MCP` is superseded).

## 1. Verified dataset inventory

Sources: `~/Projects/patient-avatars/sample_metadata.tsv` (pandas, `dtype=str`, `keep_default_na=False`), matrix headers under `~/Projects/patient-avatars/results/`, `METADATA_SCHEMA.md`, `contrast_space_tutorial.md`, `SESSION_HANDOFF.md`, `build_metadata.py`, `run_rnaseq.sh`.

| `dataset` | samples | cell lines | design as built | canonical matrices |
|---|---:|---:|---|---|
| LOCAL | 234 | 19 | chronic_resistance 136 (H3122: Cmax / StartIC50 / StartLow x BR1-3 = 9 derivations; H2228: 8 derivations, **StartLow_BR3 absent**), acute_drug 56 (13 lines, alectinib), baseline 42. Every chronic derivation = 8 samples: 4 at `dose_nM=0` + 4 on drug (Cmax 1300, StartLow/StartIC50 1500), split 2/2 across `batch` 1/2. Includes non-lung ALK+ lines (SR-786, SU-DHL-1: ALCL; SH-SY5Y: neuroblastoma; A549 +/- EML4-ALK transgene). | `soellner_13807-nm/rsem.merged.gene_{tpm,counts}.tsv` |
| E11304 | 60 | 1 (NL20) | 5 constructs {EV, V1, V3, KIF5B, TFG} x {dox, nodox} x 6 reps. `compound` = doxycycline / none; `dose_nM` empty; `fusion=none` for **all** nodox rows. **No `construct` column**; derived at build from `matrix_sample_id` (`NL20-{con}-{dox|nodox}-rep{n}`). | `e-mtab-11304_results/` |
| E11342 | 90 | 4 | CUTO8/9/29, YU1077 x {alectinib, brigatinib, lorlatinib} x {6, 24 h} x 3 reps; **YU1077 has no alectinib arm**. Controls: `compound=none`, 3 per line per timepoint (time-split), `perturbation_class=baseline`. `dose_nM` empty everywhere (not in SDRF). | `e-mtab-11342_results/` |
| E11834 | 120 | 5 | CUTO8/9/29, H2228, H3122 x {lorlatinib, trametinib} x {6, 24 h} x 4 reps. Controls: `compound=none`, **8 per line, `timepoint_h` empty** (shared across 6 h / 24 h), `perturbation_class=baseline`. `dose_nM` empty. | `e-mtab-11834_results/` |
| FL3C | 175 | **60** (50 lung_adeno, 7 lung_nonadeno_nsclc, 2 lung_fibroblast, 1 lung_epithelial_normal) | baseline only, 3 reps (5 lines have 2). Annotation in `notes` (subtype, KRAS, EGFR, TP53, prolif72h); more in `fl3c/fl3c_sup_S1_cell_lines.csv` (60 real rows of 97; BOM, 37 blank rows; names `NCI-H2228`/`NCI-H3122` vs metadata `H2228`/`H3122`). | `fl3c/` |
| **total** | **679** | 79 distinct `cell_line` | 25 metadata columns; `sample_id = {dataset}::{matrix_sample_id}` already present and unique; 18 `matrix_sample_id` values recur between E11342 and E11834 | 78,932 gene rows, identical `gene_id` set **and order** in all 13 gene-level files |

### 1.1 Corrections to the kickoff prompt

| Kickoff claim | Reality | Evidence |
|---|---|---|
| ~692 metadata rows | **679** rows, 25 columns. 694 physical lines = header + 679 + 14 wrapped multi-line FL3C `notes`. | pandas row count vs `wc -l` |
| 78,933 gene rows | **78,932** (kickoff counted the header). | `awk 'END{print NR-1}'`; `count(DISTINCT gene_id)` |
| Two LOCAL TPM files may differ | `rsem.merged.gene_tpm.tsv` and `ALKcellLines_rsem.merged.gene_tpm.tsv` are byte-identical (md5 `6b4bf195...`); counts likewise (`e84e658b...`). `rsem.merged.gene_counts.annot.tsv` = same counts + 5 leading columns (`entrezgene_id`, `external_gene_name`, `description`); no biotype. | md5, pandas compare |
| "DFCI032-like line has StartIC50/Cmax without its own parental" | No such `cell_line`. The 16 samples are `cell_line=H2228`, groups Cmax_BR3 and StartIC50_BR2: 8 `identity_call=DFCI032` (`reassigned`, batch 1) + 8 `unresolved` (`suspect`, batch 2). 4 more in H2228 StartIC50_BR3 are `reassigned` DFCI032. DFCI032 **does** have a parental (4 samples, `confirmed`). | metadata; `METADATA_SCHEMA.md` "recorded cell_line values stay as supplied" |
| `parent_line` defines chronic pairing | Empty for all 136 chronic rows. Populated only for A549-EML4ALK -> A549 (2) and a CUTO29.1 -> CUTO29.1 self-reference (6). | value counts; `build_metadata.py` `PARENT` dict |
| "other 17 lines are parental-only baseline" | 17 lines lack chronic derivatives, but 11 of them have acute alectinib arms. LOCAL = chronic 136 / acute 56 / baseline 42. | crosstab |
| `0` = explicitly no drug | True only in LOCAL. All 445 public-cohort rows have empty `dose_nM`, including the 64 `compound=none` controls and 30 `nodox` rows. | value counts |
| H2228 carries the full ladder | StartLow_BR3 absent (STR-profiled as DFCI032, never in the RNA set). | `contrast_space_tutorial.md` SAMPLE IDENTITY |
| FL3C ~97 LUAD lines | 60 lines (50 LUAD); 97 is the supplement CSV row count incl. 37 blank rows. | csv parse |
| `identity_status=confirmed` (60) | Covers only H2228 (56) + DFCI032 parental (4). All other LOCAL lines are `unverified` because `somalier_call` was filled only for the H2228/DFCI032 question, though all 234 were fingerprinted (18 clean clusters). `unverified` means "no call recorded", not "failed". | `build_metadata.py`; v2 somalier report |
| LOCAL acute doses | Three values conflict with source sample names: CUTO46 and SNU2535 `300000` nM (name says `300_nM`), DFCI032 `1000` nM (name says `1_nM`). MGH953-7x 1300 nM tagged `Low`. | `resistant_cellline_metadata_rna.csv` |
| E11834 "x {6 h, 24 h}" | Treated arms only; controls have no timepoint. | SDRF `Factor Value[time]` blank |
| `fusion` usable as a filter | Mostly `unknown` (LOCAL 64, E11342 90, E11834 72, FL3C 166). Only 7 lines annotated. | value counts |
| `source_lab` in {UMich, Ghent, Colorado, Yale, FL3C} | Observed: UMich 234, Ghent 270, FL3C 175. | value counts |
| `contrast_space_tutorial.md` public counts | Its inventory (119/240/180) is SDRF FASTQ rows, 2x the sample counts. Kickoff's 60/120/90 are right. | `SESSION_HANDOFF.md` Trap 7 |

### 1.2 Facts the kickoff omitted that constrain the design

- **The Ensembl 113 GTF is not on this Mac.** It is on Armis2 (`/nfs/turbo/umms-jtari/jtsi-references/Ensembl/homo_sapiens/GRCH38/assembly113/Homo_sapiens.GRCh38.113.gtf`). Public copy: `https://ftp.ensembl.org/pub/release-113/gtf/homo_sapiens/Homo_sapiens.GRCh38.113.gtf.gz` (HTTP 200 on 2026-09-19). Local GTFs are releases 102/96 and GENCODE: wrong, do not use.
- The only on-disk symbol source (`annot.tsv`) covers 42,745/78,932 genes (54%); 480 symbols map to >1 ENSG (Y_RNA 758x); no biotype. `resolve_genes` must return many-to-one.
- Chronic delta must be at matched dose (resistant@0 vs parental@0); on-drug resistant samples measure residual drug dependence, a different contrast (`contrast_space_tutorial.md`, STRUCTURAL DISCOVERY).
- E11304 EV +/- dox is the empirical null for dox itself; the tool returns the EV delta alongside every fusion delta.
- Whether `none` in E11342/E11834 is untreated or vehicle is unknown (SDRF silent).
- Cross-dataset bridge: lorlatinib 6 h / 24 h in CUTO8/9/29 exists in both E11342 and E11834; the one legitimate cross-dataset *direction* comparison.
- Unequal replication (H3122 24/state, H2228 16, parental dose arms 4 or 2). Always report n.
- 8 `suspect` samples are unresolved H2228/DFCI032 mixtures; the definitive allele-fraction test has not been run. Phase-2 fingerprinting of public cohorts (CUTO29.1 vs CUTO29 etc.) has not been run; all 445 public rows are `unverified`. `CUTO29.1` (LOCAL) is deliberately distinct from `CUTO29`.
- `e-mtab-11304_results/rsem.merged.gene_tpm.2.tsv` is a 1,569-byte fragment; the build script globs exact filenames.
- RSEM `expected_count` is fractional (5-18% of values), max ~1.97M, 67% zeros.

## 2. DuckDB schema and benchmark

### 2.1 Benchmark (DuckDB 1.4.5, Apple M1 Max, all five TPM matrices, 53,594,828 long rows)

| layout | build | file | Q1: 1 gene x 679 + metadata | Q2: 10 genes x 24 samples | Q3: 50 genes x all (33,950 rows) | Q4: contrast aggregate | Q6: 5 samples x all genes |
|---|---:|---:|---:|---:|---:|---:|---:|
| long, VARCHAR keys, no index | 8.7 s | 240.7 MB | 28 ms | 11 ms | 112 ms | 28 ms | 187 ms |
| long + ART index on gene_id | +5.4 s | **1,284 MB** (+667 MB) | 25 ms | 10 ms | 112 ms | 25 ms | 142 ms |
| long, integer surrogate keys | +0.7 s | 136 MB (table) | 26 ms | - | 59 ms | - | - |
| wide (78,932 x 679 DOUBLE) | 7.5 s | 186.1 MB | 20 ms | 5 ms + a metadata round trip + dynamic SQL | 107 ms | 20 ms | 31 ms native / 129 ms unpivoted |

Median of 5 runs, `fetchall()` included. Value spot-checks: ALK for all 234 LOCAL and 175 FL3C samples match the source TSVs exactly; long and wide agree.

**Decision: long format, VARCHAR keys, no ART index, `ORDER BY gene_id, sample_id` at build.** Performance does not discriminate (5-190 ms everywhere; MCP round-trip and JSON will dominate). Long maps 1:1 onto `get_expression` (`expression JOIN samples`) and `contrast` (`GROUP BY` over the same join) with static SQL; wide needs UNPIVOT plus dynamic column lists quoting `::` and `-`, and its schema changes with every added sample. The ART index costs 667 MB for ~3 ms. Integer keys are the escape hatch if file size ever matters (45% smaller, Q3 2x faster, two extra joins).

### 2.2 Schema

```sql
CREATE TABLE samples (
  sample_id VARCHAR PRIMARY KEY,           -- '{dataset}::{matrix_sample_id}'
  matrix_sample_id VARCHAR NOT NULL,
  label VARCHAR,                           -- display only, never a key
  dataset VARCHAR NOT NULL,                -- LOCAL | E11304 | E11342 | E11834 | FL3C
  cell_line VARCHAR NOT NULL,              -- effective identity: molecular call where reassigned, else the lab label (decision 8.8)
  cell_line_recorded VARCHAR NOT NULL,     -- the lab label before any identity reassignment (H2228 for the 12 reassigned rows)
  cell_line_raw VARCHAR,                   -- as given by the source file
  parent_line VARCHAR,                     -- as in source (mostly empty; not used for pairing)
  lineage VARCHAR, fusion VARCHAR, fusion_source VARCHAR,
  perturbation_class VARCHAR NOT NULL,     -- baseline | acute_drug | chronic_resistance | transgene_induction
  compound VARCHAR NOT NULL,               -- 'none' for controls
  dose_nM INTEGER,                         -- NULL = not recorded / n.a.; 0 = explicitly no drug (LOCAL only)
  timepoint_h INTEGER,                     -- NULL for baseline rows and E11834 controls
  resistance_protocol VARCHAR,             -- StartLow | StartIC50 | Cmax | NULL
  derivation_replicate VARCHAR,            -- BR1 | BR2 | BR3 | NULL
  replicate INTEGER NOT NULL,
  batch INTEGER,                           -- LOCAL only
  source_lab VARCHAR,
  identity_status VARCHAR NOT NULL,        -- confirmed | reassigned | suspect | unverified
  identity_call VARCHAR, identity_confidence VARCHAR, identity_basis VARCHAR, identity_qc_status VARCHAR,
  notes VARCHAR,
  -- derived at build (documented in the schema resource; not in the upstream TSV):
  construct VARCHAR,                       -- E11304 only: EV | V1 | V3 | KIF5B | TFG
  -- annotation columns (decision 8.4): read from the updated upstream metadata sheet when present,
  -- otherwise parsed from FL3C `notes` at build; NULL where unknown. Names to be settled with you.
  histology VARCHAR,                       -- e.g. lung_adenocarcinoma, nonadeno_nsclc (FL3C "subtype")
  mut_kras VARCHAR, mut_egfr VARCHAR, mut_tp53 VARCHAR, mut_braf VARCHAR,  -- protein-level calls, e.g. 'p.G12C'; 'WT'; NULL = not assessed
  proliferation_72h DOUBLE                 -- FL3C 72 h proliferation measure
);

CREATE TABLE genes (
  gene_id VARCHAR PRIMARY KEY,             -- ENSG, unversioned; exactly the 78,932 matrix ids
  gene_symbol VARCHAR,                     -- Ensembl 113 gene_name (may be NULL; not unique)
  gene_biotype VARCHAR,                    -- Ensembl 113 gene_biotype
  entrez_id VARCHAR,                       -- from annot.tsv (27,610 present)
  description VARCHAR                      -- from annot.tsv
);

CREATE TABLE expression (
  sample_id VARCHAR NOT NULL,              -- FK samples
  gene_id VARCHAR NOT NULL,                -- FK genes
  tpm DOUBLE NOT NULL,
  expected_count DOUBLE                    -- RSEM expected_count; fractional
);  -- 53,594,828 rows; ORDER BY gene_id, sample_id

CREATE TABLE datasets (
  dataset VARCHAR PRIMARY KEY, access_tier VARCHAR,  -- public | restricted
  source VARCHAR, source_doi VARCHAR, caveats VARCHAR  -- static text maintained in the build script
);

CREATE TABLE provenance (key VARCHAR PRIMARY KEY, value VARCHAR);
-- build_date, duckdb_version, jtari_mcp_version, git_commit, pipeline='nf-core/rnaseq 3.14.0 --aligner star_rsem',
-- genome='GRCh38 Ensembl 113', gtf_url, gtf_sha256, metadata_sha256, matrix_sha256_<dataset>_<unit> (10 keys),
-- n_samples=679, n_genes=78932, datasets_included
```

`genes` build: read the Ensembl 113 GTF from the data directory (`--gtf PATH`, default `$JTARI_DATA_DIR/Homo_sapiens.GRCh38.113.gtf.gz`; you are placing a copy there, decision 8.11), record its sha256, parse `gene` features -> `gene_id, gene_name, gene_biotype`; assert the ID set equals the matrix set (78,932, zero missing); cross-check `gene_name` against `annot.tsv` `external_gene_name` and log disagreements into provenance. If the GTF is missing, fail loudly; no silent fallback to the 54%-coverage annot file. `--download-gtf` fetches the public Ensembl copy as a convenience.

Identity relabeling (decision 8.8, your answer 2026-09-19): the 12 `reassigned` rows are served with `cell_line = identity_call` (DFCI032) and `cell_line_recorded = H2228`, so a search or contrast on `H2228` never returns them and a search on `DFCI032` does. Preferred implementation: make the change in the upstream metadata sheet you are updating anyway (set `cell_line`, keep the lab label in `cell_line_recorded`, set `parent_line = DFCI032`), so the data is the truth and the server has no relabel logic. The build script asserts `identity_status = 'reassigned' implies cell_line = identity_call` and fails otherwise. The 8 `suspect` rows (unresolved H2228/DFCI032 mixtures) keep `cell_line = H2228` but are excluded by default from every tool and flagged whenever included. Consequence for `contrast`: DFCI032 gains chronic derivations (Cmax "BR3" 4 samples, StartIC50 "BR2" 4, StartIC50 "BR3" 4; the BR labels are inherited from the H2228 derivation series) paired against the confirmed DFCI032 parental (4 samples, 2 at 0 nM), and H2228 loses them.

`build_db.py --datasets` selects which cohorts to load, so one script produces `jtari-full.duckdb` (with LOCAL, restricted) and `jtari-public.duckdb` (the four public cohorts). Two files, not row-level access filtering: a missed filter in one tool would leak Moderate data; a separate file cannot.

## 3. TPM and counts decision

**Load both. Serve TPM by default. Expose `unit = "tpm" | "counts"` on `get_expression` only; `contrast` v1 is TPM-only.**

- Cost is +70 MB (+29%, 242 -> 312 MB) and ~7 s of build, not "double": the key columns are shared in the long table (measured on LOCAL, scaled).
- Benefit: a v2 `contrast` with a proper DE method (pydeseq2 on `expected_count`) needs them, and the LOCAL chronic series (n = 4 per arm, 8 per derivation) is where that is worth doing. Rebuilding the DB later just to add a column is more friction than 70 MB now.
- `expected_count` is fractional (RSEM); stored as DOUBLE; the tool description says to round only when handing to DESeq2/tximport.
- Tool docs state: TPM is within-sample normalized, good for "is gene X expressed", ranking genes within a sample, and effect directions within a dataset; counts are for DE methods only and are meaningless across datasets without joint normalization.

## 4. Tool and resource specifications

Stack: `mcp>=2.2,<3` (`from mcp.server import MCPServer`), Python 3.12 (pyenv 3.12.0 present; uv can also manage it), `duckdb>=1.4,<2` (benchmarked on 1.4.5; 1.5.5 is current on PyPI), `pydantic>=2.12` (SDK dep), `uv` 0.10.9. Package `jtari_mcp`, console script `jtari-mcp`, env var `JTARI_DB` (path to the DuckDB file). One `MCPServer("jtari", instructions=..., lifespan=open_duckdb)`; every tool carries `ToolAnnotations(read_only_hint=True, open_world_hint=False)`.

Conventions shared by all tools:
- **Identity guardrail**: `include_suspect: bool = False` on every sample-returning tool. `identity_status`, `identity_call` and `cell_line_recorded` come back with every sample row. `cell_line` is the **effective identity** (molecular call where `reassigned`, section 2.2), so `H2228` queries never return DFCI032 material. Rows whose `cell_line` differs from `cell_line_recorded` add a top-level `identity_flags` entry (e.g. `"12 rows are served as DFCI032 (somalier); the lab label was H2228 Cmax_BR3 / StartIC50_BR2 / StartIC50_BR3"`). Suspect rows, when included, are always flagged.
- **Filters** (`find_samples`, `get_expression`): `filters: dict[str, str | int | list[str | int] | None]`. Scalar = equals, list = any-of, `None` = `IS NULL`. Keys are validated against the `samples` columns; an unknown key raises `ToolError` listing the valid columns.
- **Genes**: `get_expression` and `contrast` accept symbols or ENSG IDs and resolve internally. A symbol with several ENSG matches raises `ToolError` naming the candidates and pointing to `resolve_genes`.
- **Errors**: anything the model could fix (unknown gene, unknown column, too many rows) raises `ToolError` with an actionable message. Never return an error string.
- **Return shapes**: pydantic models, so the client gets `structuredContent` + `outputSchema` and the model gets JSON text. Rows are returned in a fixed order.

### 4.1 `describe_metadata(columns: list[str] | None = None, by_dataset: bool = False)`

Returns, per column: `description` (from `METADATA_SCHEMA.md`), `sql_type`, `n_distinct`, `n_null`, `levels: [{value, n}]` (all levels for columns with <= 60 distinct values, top 20 otherwise), and with `by_dataset=True` the level counts split by `dataset`. Excludes `notes`, `label`, `matrix_sample_id` from level listing.

Description:
> Return the metadata vocabulary of the JTARI ALK cell-line RNA-seq resource: every sample column with its meaning, type, and the distinct values with counts, straight from the database. Call this first when you need to translate a question into filters, e.g. "ordered by treatment" means compound, then dose_nM, then timepoint_h; "resistant vs parental" means perturbation_class chronic_resistance vs baseline within the same cell_line and dataset. Key facts: dose_nM is an integer in nanomolar; 0 means explicitly no drug and is used only in dataset LOCAL, NULL means not recorded (all public cohorts, including E11342 where the paper's doses are not in the metadata). resistance_protocol values StartLow, StartIC50, Cmax are three independent derivation protocols, not an ordered ladder; derivation_replicate BR1-3 is the unit of evolutionary replication, replicate is the library replicate. identity_status: confirmed, reassigned (molecular identity differs from the recorded cell_line; see identity_call), suspect (excluded by default everywhere), unverified (no identity call recorded; not a failure). Pass by_dataset=true to see which levels exist in which dataset before composing a contrast.

### 4.2 `find_samples(filters: dict = {}, columns: list[str] | None = None, include_suspect: bool = False, limit: int = 200)`

Returns `{n_total, n_returned, identity_flags, rows: [{sample_id, label, dataset, cell_line, perturbation_class, identity_status, identity_call, ...requested columns}]}`. `limit <= 1000`; if `n_total > limit` the response says so and returns the first `limit` ordered by `dataset, cell_line, perturbation_class, compound, dose_nM, timepoint_h, resistance_protocol, derivation_replicate, replicate`.

Description:
> Find samples by any metadata column and return their identifiers plus the columns you ask for. filters is a dict of column to value: a scalar means equals, a list means any-of, null means the column is unrecorded (e.g. {"dataset": "E11834", "compound": ["lorlatinib", "none"], "timepoint_h": null}). Column names and values come from describe_metadata; an unknown column returns an error listing the valid ones. Suspect-identity samples are excluded unless include_suspect=true. Use this to check what exists (which lines have which drugs, doses, timepoints) before calling get_expression or contrast; it returns no expression values. sample_id is the global key ({dataset}::{matrix_sample_id}); label is display-only.

### 4.3 `resolve_genes(query: str | list[str], limit: int = 50)`

Returns `[{query, gene_id, gene_symbol, gene_biotype, match: "ensg" | "symbol" | "symbol_ci" | "prefix"}]`. Exact ENSG and exact symbol first, then case-insensitive symbol, then prefix on symbol or ENSG (only when the query is >= 3 characters). A symbol with several ENSGs returns all of them. A query with zero matches appears once with `gene_id=null`.

Description:
> Map gene symbols, Ensembl gene IDs (ENSG..., unversioned), or prefixes to the gene rows in this resource (Ensembl GRCh38 release 113; 78,932 genes). Returns gene_id, gene_symbol, gene_biotype and how each query matched. Symbols are not unique: some map to several ENSG IDs (e.g. many snRNA/rRNA families), and roughly 40% of Ensembl genes have no symbol at all; always confirm the ENSG you intend before quoting expression. get_expression and contrast accept symbols or ENSGs directly and call this internally, so use resolve_genes when a symbol is ambiguous, when you want the biotype, or when the user gives a partial name.

### 4.4 `get_expression(genes: list[str], filters: dict = {}, unit: Literal["tpm", "counts"] = "tpm", columns: list[str] | None = None, include_suspect: bool = False, limit: int = 2000)`

Returns `{unit, n_rows, genes: [{gene_id, gene_symbol}], identity_flags, rows: [{sample_id, gene_id, gene_symbol, value, dataset, batch, cell_line, perturbation_class, compound, dose_nM, timepoint_h, resistance_protocol, derivation_replicate, replicate, identity_status, identity_call, ...extra columns}]}`. Hard cap `limit <= 5000`. If `n_genes x n_samples > limit`, raise `ToolError` stating the count and suggesting filters or fewer genes (refuse, never truncate). Unknown genes raise `ToolError` naming them. Ordered by `gene_id`, then the `find_samples` sample order.

Description:
> Return expression values for one or more genes (symbol or ENSG) as a long table, one row per gene x sample, joined to sample metadata so you can sort and group client-side (e.g. by compound, dose_nM, timepoint_h, resistance_protocol). unit "tpm" (default) is RSEM TPM from nf-core/rnaseq 3.14.0 star_rsem, Ensembl 113; unit "counts" is RSEM expected_count (fractional; for differential-expression methods only). Every row carries dataset and batch: TPM is within-sample normalized and the five datasets were sequenced in different labs, so ranking or differencing raw TPM across datasets is not meaningful; compare within a dataset, or use contrast, which pairs within dataset and cell_line. filters uses the same syntax as find_samples. Suspect-identity samples are excluded unless include_suspect=true. Requests that would exceed limit rows are refused with the row count so you can narrow the filter; no silent truncation.

### 4.5 `contrast(gene: str, design: Literal["chronic", "acute", "transgene"], dataset: str | None = None, cell_line: str | list[str] | None = None, on_drug: bool = False, include_suspect: bool = False)`

Returns `{gene_id, gene_symbol, design, unit: "tpm", method: "log2((mean_tpm_A+1)/(mean_tpm_B+1))", rows: [ContrastRow]}` where `ContrastRow = {dataset, cell_line, group: {compound, dose_nM, timepoint_h, resistance_protocol, derivation_replicate, construct}, control: {...same keys...}, n_group, n_control, mean_tpm_group, mean_tpm_control, log2fc, sd_log2_group, sd_log2_control, batches_group, batches_control, identity_group: {confirmed, reassigned, unverified, suspect_excluded}, flags: [str]}`. No p-values in v1 (n is 2-8). Rows with `n_group < 2` or `n_control < 2` are returned with a flag, not dropped.

Pairing rules, all within `dataset`. `parent_line` is not used (empty for every chronic row); if upstream `build_metadata.py` is fixed later, the rule below is a strict superset today.

| design | group | control | notes |
|---|---|---|---|
| `chronic` | `cell_line=L, resistance_protocol=P, derivation_replicate=BR, dose_nM=0` | `cell_line=L, perturbation_class=baseline, dose_nM=0` | LOCAL only. One row per (L, P, BR). `on_drug=true` instead compares the same derivation at its maintenance dose (1300/1500 nM) to itself at 0 nM (residual drug dependence). With the effective `cell_line`, the reassigned derivations pair with the DFCI032 parental and are flagged (`cell_line_recorded=H2228`); H2228 Cmax_BR3 and StartIC50_BR2 then have no non-suspect samples and are reported with n=0 and a flag rather than dropped. |
| `acute` | `cell_line=L, compound=C, dose_nM=D, timepoint_h=T` (D, T may be NULL) | `cell_line=L, compound=none`; `timepoint_h=T` when the controls of L in that dataset carry timepoints (E11342), pooled when they do not (E11834); LOCAL: `dose_nM=0, perturbation_class=baseline` | Never pairs across datasets even for the same line and drug. |
| `transgene` | `construct=K, compound=doxycycline` | `construct=K, compound=none` | E11304 only. The EV row is always included as the dox-only null. |

Description:
> Compute per-group log2 fold changes of one gene for a named experimental design, pairing each treated or derived group with its own control inside the same dataset and cell line. design "chronic": each resistant derivation (cell_line x resistance_protocol x derivation_replicate, dataset LOCAL) at 0 nM versus that line's parental at 0 nM, so the result is resistance state, not acute drug effect; set on_drug=true to instead get residual drug dependence (derivation at its maintenance dose vs itself at 0 nM). design "acute": each compound x dose_nM x timepoint_h arm versus the untreated ("none") control of the same cell line in the same dataset (E11342 controls are time-matched, E11834 controls are pooled across time because they have no timepoint; LOCAL controls are 0 nM baseline). design "transgene": +dox versus -dox within each construct in E11304; the empty-vector row is the doxycycline null and is always included. Returns n, mean TPM and the SD of log2(TPM+1) per side, batches, and identity composition; log2fc = log2((mean_A+1)/(mean_B+1)) with no p-value (n is 2 to 8; treat as descriptive). Never pairs across datasets: TPM offsets between cohorts are not removed by differencing, so compare directions, not magnitudes, across datasets. Suspect-identity samples are excluded unless include_suspect=true; groups that are left with only reassigned samples are flagged.

### 4.6 `dataset_summary()`

Returns `[{dataset, access_tier, n_samples, n_cell_lines, cell_lines, perturbation_classes: {class: n}, compounds, doses_nM, timepoints_h, source_lab, identity_status: {status: n}, caveats: [str]}]` from the DB (`samples` + `datasets`).

Description:
> Inventory of the RNA-seq datasets in this resource (cell lines only; up to 679 samples across 5 datasets, 78,932 Ensembl 113 genes, all quantified with nf-core/rnaseq 3.14.0 star_rsem): per dataset the sample and cell-line counts, perturbation classes, compounds, doses, timepoints, source lab, identity-status counts, access tier, and known caveats. Call this to orient before choosing a design for contrast or to explain to the user what can and cannot be compared. Patient samples are not in this resource.

### 4.7 Resources

| URI | mime | content |
|---|---|---|
| `jtari://schema` | text/markdown | `METADATA_SCHEMA.md` (copied into the package at build as `src/jtari_mcp/resources/METADATA_SCHEMA.md`) plus an addendum: derived columns, observed vs schema-listed levels, the corrections in section 1.1 |
| `jtari://datasets` | text/markdown | the inventory table rendered from the DB (same data as `dataset_summary`) |
| `jtari://interpretation` | text/markdown | how to interpret results: TPM within-sample; the cross-dataset rule; matched-dose rule for chronic; EV null for transgene; identity semantics; n and batch caveats; what `unverified` means |
| `jtari://provenance` | application/json | the `provenance` table |

### 4.8 Prompt (one, as a teaching example)

`explore_gene(gene: str)`: "Using the jtari tools: resolve {gene}, summarize its expression by dataset and perturbation_class with get_expression, then run contrast for the chronic, acute and transgene designs and report directions with n. Do not compare TPM across datasets."

### 4.9 Optional after v1

- `plot_expression(gene, filters, group_by)` returning a PNG strip/box plot via `Image(data=..., format="png")` (matplotlib, Agg backend).
- `contrast(..., method="deseq2")` on `expected_count` via `pydeseq2` for the LOCAL chronic derivations.
- FL3C clinical columns (Stage, Smoker, Sex, ...) if anyone asks; they are sparse (10-55 of 60 lines).

## 5. Phases

Each phase = one branch (`phase-N-<name>`), one PR into `main`, a `docs/phases/phase-N.md` note ("what you just built and why", one page), and a checkpoint: the questions from `docs/LEARNING.md` section 9 asked in chat before the next phase starts. Estimated wall-clock per phase is one working session; the build is sequential because each phase is exercised by you before the next.

| # | Branch | Deliverable | Acceptance (you run it) | Checkpoint (LEARNING.md 9) |
|---|---|---|---|---|
| 0 | `phase-0-scaffold` | `pyproject.toml` (uv, Python 3.12, pinned deps, `[project.scripts] jtari-mcp`), `uv.lock`, `src/jtari_mcp/__init__.py`, `tests/test_smoke.py`, `.gitignore` additions (`data/`, `*.duckdb`, `.memsearch/`), `CITATION.cff`, README (SAB language, with the "cell lines, mostly LUAD, plus ALCL/neuroblastoma controls" qualifier), GitHub Actions CI, `docs/LEARNING.md` and `PLAN.md` merged | `uv sync && uv run pytest` passes; `uv run jtari-mcp --help` prints | none |
| 1 | `phase-1-hello` | `server.py` with `MCPServer("jtari")`, one tool `dataset_summary()` returning the hardcoded inventory, stdio `run()`; in-memory `Client(mcp)` test; stdio subprocess test; registered in Claude Desktop (`claude_desktop_config.json`) and Claude Code (`claude mcp add`; committed `.mcp.json`) | You ask Claude Desktop "what datasets does the jtari server have" and it calls `dataset_summary`; `uv run mcp dev` Inspector shows the tool; tests pass | Q1, Q2 |
| 2 | `phase-2-build-db` | `scripts/build_db.py` (CLI: `--data-dir`, `--out`, `--datasets`, `--gtf PATH` or download, `--no-counts`), schema of section 2.2, `genes` from the Ensembl 113 GTF + annot cross-check, derived `construct` and FL3C columns, typed `dose_nM`/`timepoint_h`/`replicate`/`batch`, `datasets` and `provenance` tables; `tests/test_build_db.py` on a synthetic fixture; an opt-in integration test against the real data dir (`JTARI_DATA_DIR`) asserting the section 1 numbers | Full build < 60 s; `duckdb` CLI reproduces the section 1 counts; ALK TPM for `LOCAL::13807-NM-1` equals the TSV; `jtari-public.duckdb` builds without LOCAL | Q2b |
| 3 | `phase-3-read-tools` | lifespan opening DuckDB read-only from `JTARI_DB`; `describe_metadata`, `find_samples`, `resolve_genes`, `get_expression`, DB-backed `dataset_summary`; resources `schema`, `datasets`, `provenance`; per-tool tests on the fixture DB incl. `ToolError` paths and the row cap | From Claude Desktop: "show ALK expression across all cell-line samples ordered by treatment and dose" produces a correct table with dataset shown; the model declines to rank across datasets when asked | Q3, Q4 |
| 4 | `phase-4-contrast` | `contrast` with the three designs, `interpretation` resource, `explore_gene` prompt; tests for each pairing rule on the fixture (E11834 pooled controls, E11342 time-matched, H2228 reassigned flag, EV null, no cross-dataset row) | The kickoff question ("log2FC of gene X under acute lorlatinib in each CUTO line across both acute cohorts, next to Cmax-resistant vs parental in H3122 and H2228") answered in one Claude Desktop turn with n and flags | Q5, Q6 |
| 5 | `phase-5-http` | `--transport streamable-http --host --port`, bearer-token auth (section 6.3), `--allowed-hosts`, `/health` route, `Dockerfile`, `compose.yaml`, `deploy/launchd/edu.umich.jtari.mcp.plist`, `deploy/Caddyfile`, `docs/DEPLOY.md`; HTTP integration test with a bearer header (401 without, 200 with) | Server runs under launchd on your Mac (or Docker); `claude mcp add --transport http ... --header` from a second machine on the UM network works; Claude Desktop reaches it via `mcp-remote` | Q7 |
| 6 (optional) | `phase-6-extras` | `plot_expression`; Zenodo bundle script for the public matrices; DESeq2 contrast | as agreed | - |

## 6. Deployment comparison and recommendation

Full report with sources (Apple, Anthropic, MCP SDK, Astral, Docker, Tailscale, AWS, and Internet Archive captures of UM ITS / Safe Computing pages, which return 403 to non-browser clients) is in the deployment engineer's notes; the decisive facts:

- **Claude Desktop custom connectors connect from Anthropic's cloud**, must reach the server over the public internet from `160.79.104.0/21`, and reject private, loopback and CGNAT (100.64/10, i.e. Tailscale) addresses. Static "Request headers" auth for connectors is a limited beta; the default is OAuth. Claude Desktop **can** reach a campus server via a local stdio bridge (`npx mcp-remote <url> --header ...`) in `claude_desktop_config.json`. **Claude Code connects from the user's machine** and supports `--header "Authorization: Bearer ..."`, so a campus-only host works for it directly.
- **UM DS-14 Network Security Standard v2.0 (2025-02-18)** requires host firewalls (default deny), ACLs or private IPs for services not needing Internet exposure, and prior approval for "VPN-like devices and programs (e.g. Wireguard)"; unapproved ones "will be blocked without notice". Tailscale therefore needs a unit network administrator's sign-off. Off-campus access to Moderate data must go through U-M Net VPN (free for faculty/staff/students; Friend accounts excluded).
- **Data classification**: unpublished research data is **Moderate** by default at UM; the PI may classify it Low. LOCAL 13807-NM is not PHI. Recommend treating it as Moderate until publication. Whether Moderate data is permitted in AWS at U-M is on a Weblogin-walled Sensitive Data Guide page (unverified; one click for you to check).
- **UM SSO** is mid-migration from Shibboleth to Okta (proxying since 2026-02-25; new OIDC apps self-service via AMP since March 2026; Duo retires 2026-12-01). OIDC for this server is feasible but needs a pre-registered client and an Okta authorization server that mints tokens with the MCP URL as audience; unverified whether the UM tenant exposes that. **v3, not v2.**
- **Michigan Medicine (HITS) networks** are stricter (managed devices only; current server policy is on a login-walled SharePoint). Whether the Mac Studio is on a HITS network or campus UMnet is the blocking unknown.

### 6.1 Comparison

| Criterion | Lab Mac Studio (launchd, campus network) | AWS EC2 t4g.medium (Docker, ITS-provisioned account) |
|---|---|---|
| Monthly cost | $0 marginal (hardware owned) | ~$25 on-demand / ~$15 1-yr reserved, + ~$2 EBS gp3 20 GB; egress rounds to $0 (100 GB/mo free); optional VPC-to-campus VPN $35/mo not needed |
| Data governance | LOCAL never leaves campus; Moderate handled by VPN + TLS + tokens; FileVault | Must confirm Moderate is permitted in AWS at U-M; Enterprise Agreement applies only via the ITS account; DS-14 covers externally hosted systems |
| Auth options | Bearer tokens (v2); Okta OIDC (v3); Tailscale only with DS-14 approval | Same, plus native Claude Desktop connectors (public IP + Anthropic egress allowlist) |
| Claude Desktop reachability | Only via `mcp-remote` stdio bridge | Native connectors if 443 is public |
| Maintenance | macOS patching needs a reboot and, with FileVault, an in-person unlock; otherwise `git pull && uv sync --locked && launchctl kickstart -k` | Unattended upgrades; `compose pull && up -d`; no physical dependency |
| Uptime | ~99% (lab power, macOS updates) | ~99.9% |
| Latency for campus users | LAN | ~10-20 ms; irrelevant at MCP granularity |
| Time to first deploy | Half a day once network placement and DNS are settled; 1-2 weeks waiting on unit IT for hostname + InCommon cert (Caddy internal CA bridges the gap) | 2-3 business days for the ITS account, then half a day; Let's Encrypt immediate |
| Blocking unknowns | Which network the Mac is on (HITS vs UMnet); inbound 443 from campus allowed? | Sensitive Data Guide AWS row for Moderate |

### 6.2 Recommendation for v2

**Mac Studio, bare `launchd` LaunchDaemon (not an agent: agents die at logout and ignore `UserName`), running as a dedicated non-admin service account, Caddy terminating TLS with an InCommon certificate on a `umich.edu` hostname (Caddy internal CA until the cert lands), per-user bearer tokens, Claude Code connecting directly, Claude Desktop through `mcp-remote`, off-campus users on U-M Net VPN.** Zero marginal cost, the unpublished data stays on campus, every lab member has a working client path today, and nothing needs a policy exception. Containers on the Mac are not needed (Docker Desktop and OrbStack have licensing ambiguity for lab use; colima is the only free boot-capable option if ever wanted).

**Fallback**: EC2 t4g.medium (arm64, same architecture as the Mac build) in an ITS-provisioned AWS at U-M account, the same container, Caddy with Let's Encrypt, security group 443 from UM ranges plus `160.79.104.0/21`, bearer tokens, SSM instead of SSH. Trigger: the Mac cannot be made reachable from campus, or the lab wants native Claude Desktop connectors before publication. Gate: the Sensitive Data Guide AWS row must permit Moderate.

Preconditions to settle with unit IT before Phase 5 deploys for real: (1) which network the Mac Studio is on and whether it is HITS-managed; (2) a DNS name and InCommon cert via the unit's certificate managers (two-staff rule); (3) inbound 443 from campus/VPN ranges reaches the machine; (4) PI decision on Moderate vs Low for LOCAL.

### 6.3 Auth implementation

Two SDK-verified options for a pre-shared bearer token:

- **(a) Starlette middleware** on `mcp.streamable_http_app()`: check `Authorization: Bearer`, constant-time compare against a `tokens.toml` of per-person hashed tokens (`hmac.compare_digest`), log the token *name* per call, return a bare 401 otherwise; `/health` stays open. No fake OAuth issuer is advertised. **Recommended for v2.**
- **(b) SDK `TokenVerifier` + `AuthSettings`**: verified end to end; produces the spec's 401 + `WWW-Authenticate: Bearer ... resource_metadata=...` exchange, but `AuthSettings.issuer_url` is required, so the protected-resource metadata advertises a placeholder issuer that OAuth-discovering clients will follow to nowhere. This becomes the right slot when a real issuer (Okta) exists in v3.

Rotation: one token per person; departure or leak = delete the line and `launchctl kickstart -k`; annual regeneration. Mode switch `JTARI_AUTH=none | bearer` so a public instance after publication runs with `none` plus a Caddy rate limit.

Two settings that bite the moment the server has a hostname: DNS-rebinding protection allowlists only localhost by default, so pass `TransportSecuritySettings(allowed_hosts=[...])` (exposed as `--allowed-hosts` / `JTARI_ALLOWED_HOSTS`) or every request gets `421 Misdirected Request`; behind Caddy run uvicorn with `--proxy-headers --forwarded-allow-ips=<caddy>`.

### 6.4 When LOCAL is published

Configuration change, not code change: build `jtari-public.duckdb` today already; publish the public bundle (four public matrices + `sample_metadata.tsv` + build script hash) to Zenodo (DOI into `datasets.source_doi`); flip `JTARI_AUTH=none` on a public read-only instance (EC2 or a UM public host with 443 open, which also makes Claude Desktop connectors work natively); `dataset_summary()` reports `access_tier` so users of the full instance know which rows must stay in the lab.

Appendix A holds the draft plist, Caddyfile, Dockerfile, `.dockerignore`, `compose.yaml` and client snippets (linted with `plutil -lint` and `docker compose config`).

## 7. Test strategy

- **Runner**: `pytest` + `anyio` (the SDK's async backend; no `pytest-asyncio`). `uv run pytest`.
- **Fixture DB** (`tests/conftest.py`, session-scoped): built by the real `build_db` functions from **synthetic** inputs generated in-test: ~60 genes (real ENSG ids incl. ALK/EML4/KIF5B/TFG and one duplicated symbol, fake values) and a synthetic `sample_metadata.tsv` (~70 rows) reproducing every design shape the tools depend on: LOCAL parental + one derivation x 2 batches x 0/maintenance dose, an acute alectinib arm, a `reassigned` and a `suspect` sample; E11342 time-matched controls; E11834 pooled controls; E11304 EV + one fusion +/- dox; FL3C baseline with a multi-line `notes` value; two colliding `matrix_sample_id`s across E11342/E11834. Nothing from the real LOCAL data is committed.
- **Unit, ingest**: column typing (`dose_nM` NULL vs 0), `construct` derivation, GTF parsing, symbol cross-check logging, provenance keys, exact-filename globbing (the `.2.tsv` fragment and `ALKcellLines_` copies are ignored), gene-order assertion fails loudly on a shuffled matrix, `--datasets` subsetting.
- **Unit, tools**: one file per tool via `Client(mcp)`; assert on `result.is_error` and `structured_content`; cover the guardrails explicitly: suspect excluded by default and included on request; row cap refuses with the count; unknown column/gene messages list alternatives; ambiguous symbol points to `resolve_genes`; contrast never returns a cross-dataset row; E11834 control pooling; E11342 time matching; EV row present; reassigned-only group flagged; deterministic row order.
- **Integration**: (a) stdio subprocess via `Client(StdioServerParameters(...))` with `JTARI_DB` pointing at the fixture (catches a stray `print()`); (b) Streamable HTTP with an `httpx2` bearer header: 401 without token, tool call with token (Phase 5); (c) opt-in real-data build test, skipped unless `JTARI_DATA_DIR` is set, asserting the section 1 numbers.
- **CI**: GitHub Actions on PR: `uv sync --frozen`, `uv run ruff check`, `uv run pytest`. The real-data test does not run in CI.

## 8. Open decisions for you

Resolved 2026-09-19 (your answers in chat):

1. **License**: MIT. Confirmed.
4. **Annotation columns**: FL3C `notes` fields become typed `samples` columns (section 2.2). You will produce an updated upstream metadata sheet populating them for non-FL3C samples too (WES for the H3122/H2228 resistant series). Build reads the sheet's columns when present and parses FL3C `notes` as the fallback. Column names (`histology`, `mut_kras`, `mut_egfr`, `mut_tp53`, `mut_braf`, `proliferation_72h`) and the value convention (`p.G12C` / `WT` / NULL) are my proposal; tell me if you want different ones before Phase 2.
8. **Reassigned samples**: relabel. `cell_line` = molecular identity (DFCI032), `cell_line_recorded` = lab label (H2228). Preferably fixed in the upstream sheet; build asserts consistency (section 2.2).
11. **GTF**: you place `Homo_sapiens.GRCh38.113.gtf.gz` in the data folder; build reads it from there.
16. **`PROMPT_mcp_server_kickoff.md`**: not committed; added to `.gitignore` on the planning branch. `PLAN.md` supersedes it as the project spec.

Still open:

2. **Zenodo** for the public E-MTAB re-quantified matrices so others can build `jtari-public.duckdb`? LOCAL stays private until published. (Zenodo: 50 GB / 100 files per record; the four public TPM+counts files total ~430 MB.)
3. **`contrast` v1 method**: descriptive log2FC of mean TPM (section 4.5) now, with a precomputed DESeq2 table as v2 (see the note under this list). Default is the descriptive version unless you object.
5. **E11342 doses** are not on disk. Provide the paper/supplement and I will fill `dose_nM`; otherwise they stay NULL and the tool docs say so.
6. **`none` controls in E11342/E11834**: untreated or vehicle? Needs the papers. Docs say "unknown" until then.
7. **LOCAL dose conflicts** (CUTO46/SNU2535 300000 nM, DFCI032 1000 nM vs the sample names). Ask the wet lab. Until then: serve as recorded and add a `notes` flag at build (recommended), or fix the values in the updated sheet.
9. **`parent_line` self-reference** `CUTO29.1 -> CUTO29.1` in `build_metadata.py`: fix upstream or ignore? The server ignores `parent_line` either way.
10. **Default `include_suspect=False`**: confirm.
12. **Symbol display**: report Ensembl 113 `gene_symbol` even where `annot.tsv` disagrees (recommended; disagreements logged to provenance).
13. **Bearer auth implementation**: Starlette middleware (recommended, 6.3a) vs SDK `TokenVerifier` (6.3b).
14. **Mac Studio network placement**: HITS-managed or campus UMnet? Determines whether Phase 5 needs a HITS ticket or a unit firewall rule. Also: who are the two certificate managers for the InCommon cert?
15. **LOCAL classification**: Moderate (default) or Low, per the PI.

Note on decision 3. Two ways to compute a fold change. (a) **Descriptive**: `log2((mean TPM_group + 1) / (mean TPM_control + 1))`, with n and the SD of log2(TPM+1) per side. Works at any n, costs one SQL aggregate, gives no p-value, and treats TPM as the unit. (b) **Model-based DE**: DESeq2 or edgeR on raw counts; estimates a negative-binomial dispersion per gene by borrowing information across all genes, then reports a shrunken log2FC, a p-value and an FDR. Needs the count matrix, needs at least 2 (better 3+) replicates per side, and is a whole-matrix fit, so it cannot be run per gene on demand: each contrast pair takes seconds to a minute for the full 78,932 genes. Because the contrast space here is finite and fixed (roughly 20 chronic derivations, 60 acute arms, 5 transgene constructs), option (b) fits best as a **precomputed table** written by `build_db.py` (pydeseq2, one fit per design pair, results stored as `de_results(contrast_id, gene_id, log2fc, lfc_se, pvalue, padj, base_mean)`), which `contrast` then serves with `method="deseq2"`. That is v2. For v1, (a) ships the interface and the pairing rules, which is where the correctness risk lives.

## 9. Agent team (as run, and going forward)

Planning phase used: legacy analyst (metadata verification), data/DB engineer (matrix inventory, benchmark), MCP SDK researcher (verified `mcp` 2.2.0 facts, executed snippets), deployment engineer (launchd, network, auth, EC2, container), teacher (`docs/LEARNING.md`). Build phases will use the data/DB engineer (Phase 2), MCP engineer (Phases 1, 3, 4), deployment engineer (Phase 5), teacher (phase notes and checkpoints), and a reviewer on every PR checking against `METADATA_SCHEMA.md`, the pairing rules in 4.5 and the guardrails, and running the tests. The orchestrator writes PLAN updates and talks to you.

## Appendix A: deployment drafts (Phase 5 inputs; not yet in the repo)

### A.1 `deploy/launchd/edu.umich.jtari.mcp.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>edu.umich.jtari.mcp</string>

  <!-- Dedicated non-admin account. UserName is honored only in the system
       domain (/Library/LaunchDaemons); agents ignore it. -->
  <key>UserName</key>
  <string>jtari</string>
  <key>GroupName</key>
  <string>staff</string>

  <!-- uv run form. More robust for a boot-time daemon: after a one-time
       `uv sync --locked --no-dev` as the service user, exec
       /Users/jtari/2026-09-JTARI-RNA-MCP/.venv/bin/jtari-mcp directly. -->
  <key>ProgramArguments</key>
  <array>
    <string>/Users/jtari/.local/bin/uv</string>
    <string>run</string>
    <string>--locked</string>
    <string>--directory</string>
    <string>/Users/jtari/2026-09-JTARI-RNA-MCP</string>
    <string>jtari-mcp</string>
    <string>--transport</string>
    <string>streamable-http</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8000</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/jtari/2026-09-JTARI-RNA-MCP</string>

  <!-- Daemons start with an empty environment. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/jtari/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>/Users/jtari</string>
    <key>UV_CACHE_DIR</key>
    <string>/Users/jtari/.cache/uv</string>
    <key>JTARI_DB</key>
    <string>/Users/jtari/jtari-data/jtari-full.duckdb</string>
    <key>JTARI_AUTH</key>
    <string>bearer</string>
    <key>JTARI_TOKENS_FILE</key>
    <string>/Users/jtari/jtari-secrets/tokens.toml</string>
    <key>JTARI_ALLOWED_HOSTS</key>
    <string>jtari-mcp.rogel.umich.edu,jtari-mcp.rogel.umich.edu:*,127.0.0.1:8000</string>
  </dict>

  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <!-- Without this launchd throttles CPU and I/O; DuckDB scans want none. -->
  <key>ProcessType</key>
  <string>Interactive</string>

  <!-- launchd never rotates these; app logs use TimedRotatingFileHandler. -->
  <key>StandardOutPath</key>
  <string>/var/log/jtari-mcp/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/jtari-mcp/launchd.err.log</string>
</dict>
</plist>
```

Operate with the domain-explicit `launchctl` forms (`load`/`unload` are legacy):

```bash
sudo mkdir -p /var/log/jtari-mcp && sudo chown jtari:staff /var/log/jtari-mcp
sudo install -o root -g wheel -m 644 edu.umich.jtari.mcp.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/edu.umich.jtari.mcp.plist
sudo launchctl print system/edu.umich.jtari.mcp | grep -E 'state|pid|last exit'
sudo launchctl kickstart -k system/edu.umich.jtari.mcp   # after a deploy
sudo launchctl bootout system/edu.umich.jtari.mcp        # remove
```

FileVault note: with FileVault on, nothing runs after a reboot until someone unlocks the disk at the console. Keep it on (Moderate data) and accept the walk.

### A.2 `deploy/Caddyfile` (Mac Studio)

```text
jtari-mcp.rogel.umich.edu {
    tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem
    # tls internal   # week-one fallback; run `caddy trust` on each client Mac
    reverse_proxy 127.0.0.1:8000
}
```

### A.3 `Dockerfile`

```dockerfile
# syntax=docker/dockerfile:1
# Pattern from https://docs.astral.sh/uv/guides/integration/docker/
FROM python:3.12-slim-bookworm AS base
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Layer 1: dependencies only (cached until uv.lock changes)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable

# Layer 2: the project itself
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /data && chown app:app /data
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    JTARI_DB=/data/jtari.duckdb

EXPOSE 8000
CMD ["jtari-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
```

`.dockerignore`: `data/`, `*.duckdb`, `*.duckdb.wal`, `.venv/`, `.git/`, `tests/fixtures/*.duckdb`.

Build arm64 on the Mac and deploy to t4g (arm64); keep the Dockerfile arch-neutral so `docker buildx build --platform linux/amd64,linux/arm64` works if an x86 target appears. DuckDB ships manylinux wheels for both.

### A.4 `compose.yaml`

```yaml
services:
  mcp:
    image: ghcr.io/umich-jtari-bioinformatics/jtari-mcp:0.2.0
    build: .
    restart: unless-stopped
    user: "10001:10001"
    read_only: true
    tmpfs:
      - /tmp                          # DuckDB spill; set temp_directory=/tmp in the server
    volumes:
      - ${JTARI_DATA_DIR}:/data:ro    # the .duckdb lives outside the image, read-only
      - ${JTARI_SECRETS_DIR}:/secrets:ro
    environment:
      JTARI_DB: /data/jtari-full.duckdb
      JTARI_TOKENS_FILE: /secrets/tokens.toml
      JTARI_AUTH: bearer              # none | bearer
      JTARI_ALLOWED_HOSTS: "jtari-mcp.example.umich.edu,jtari-mcp.example.umich.edu:*"
      JTARI_FORWARDED_ALLOW_IPS: "*"  # trust X-Forwarded-Proto from the caddy service
    ports:
      - "127.0.0.1:8000:8000"         # never published directly; Caddy fronts it
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "5" }

  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "443:443"
      - "80:80"                       # ACME HTTP-01 on EC2; drop on the Mac if using InCommon files
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      mcp:
        condition: service_healthy

volumes:
  caddy_data:
  caddy_config:
```

### A.5 Client snippets

Claude Code, project scope (`.mcp.json`, committed; the secret comes from the user's environment):

```json
{
  "mcpServers": {
    "jtari": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--frozen", "--directory", "${CLAUDE_PROJECT_DIR}", "jtari-mcp"],
      "env": {"JTARI_DB": "${JTARI_DB:-${CLAUDE_PROJECT_DIR}/data/jtari.duckdb}"}
    },
    "jtari-lab": {
      "type": "http",
      "url": "https://jtari-mcp.rogel.umich.edu/mcp",
      "headers": {"Authorization": "Bearer ${JTARI_TOKEN}"}
    }
  }
}
```

Claude Desktop, local stdio (v1):

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

Claude Desktop, remote via the `mcp-remote` stdio bridge (v2; runs on the user's Mac, so campus/VPN reachability is enough):

```json
{
  "mcpServers": {
    "jtari-lab": {
      "command": "/opt/homebrew/bin/npx",
      "args": ["-y", "mcp-remote", "https://jtari-mcp.rogel.umich.edu/mcp",
               "--header", "Authorization: Bearer ${JTARI_TOKEN}"],
      "env": {"JTARI_TOKEN": "paste-token-here"}
    }
  }
}
```
