"""Query logic behind the MCP tools: plain Python, no MCP imports, so every
function can be tested directly against a fixture database.

Each tool returns a dict (the server serialises it as compact JSON). Expected
problems raise QueryError with a message written for Claude, e.g. an unknown
column with the closest valid ones.
"""

import csv
import difflib
import io
import math
import re
import statistics
from collections import Counter, defaultdict

from jtari_mcp import contrasts as cx
from jtari_mcp.db import Database


class QueryError(ValueError):
    """A request the tools can't answer as asked; the message says how to fix it."""


# ---------------------------------------------------------------- vocabulary

# What each column means. Source: METADATA_SCHEMA.md, plus the build's derived columns.
COLUMN_DOCS = {
    "sample_id": "Global primary key '{dataset}::{matrix_sample_id}'. Use it to refer to a sample.",
    "matrix_sample_id": "Column name in the dataset's expression matrix; repeats across datasets.",
    "label": "Display label generated from the metadata. Never use as a key.",
    "dataset": "Cohort: LOCAL (unpublished resistance series), E11304, E11342, E11834, FL3C.",
    "cell_line": "Cell line as recorded by the lab (canonical name). Never relabelled.",
    "cell_line_raw": "Cell line name as given by the source, for traceability.",
    "parent_line": "Parental line where recorded. Empty for the chronic-resistance series; "
    "use chronic_state instead.",
    "lineage": "Tissue/lineage class, e.g. lung_adeno, alcl, neuroblastoma.",
    "fusion": "ALK fusion, e.g. EML4-ALK_V1; 'unknown' where not annotated, 'none' if absent.",
    "fusion_source": "endogenous, transgene or none.",
    "perturbation_class": "baseline, acute_drug, chronic_resistance or transgene_induction.",
    "compound": "Drug: alectinib, lorlatinib, brigatinib, trametinib, doxycycline, or none.",
    "dose_nM": "Dose in nM (integer). 0 = explicitly no drug; empty = unrecorded or not "
    "applicable (all public datasets are unrecorded).",
    "timepoint_h": "Hours of treatment (6 or 24). Empty = unrecorded (E11834 controls, all LOCAL).",
    "resistance_protocol": "StartLow, StartIC50 or Cmax: three independent protocols, not a "
    "dose ladder.",
    "derivation_replicate": "BR1-BR3: independent resistance derivations (the unit of "
    "evolutionary replication).",
    "replicate": "Library/technical replicate number. Unreliable for pairing in LOCAL; use batch.",
    "batch": "Sequencing batch (LOCAL only: 1 or 2). Empty for public datasets.",
    "source_lab": "Lab that generated the samples.",
    "identity_status": "Somalier identity check: confirmed, reassigned (genotype is another line), "
    "suspect (unresolved; excluded by default), unverified (never tested; not doubtful).",
    "identity_call": "Cell line the genotype matches, 'unresolved', or empty if not assessed.",
    "identity_confidence": "Confidence of the identity call.",
    "identity_basis": "Evidence behind the identity call.",
    "identity_qc_status": "QC note from the identity workflow.",
    "notes": "Free text. FL3C rows carry subtype, KRAS, EGFR, TP53 and 72 h proliferation here. "
    "Filter with a substring, e.g. {'notes': 'KRAS=p.G12C'}.",
    "effective_cell_line": "Molecular identity: identity_call for reassigned samples, otherwise "
    "cell_line. Contrasts group by this.",
    "identity_mismatch": "True when identity_call is set and differs from cell_line.",
    "construct": "E11304 transgene construct: EV, KIF5B-ALK, TFG-ALK, EML4-ALK_V1, EML4-ALK_V3.",
    "chronic_state": "LOCAL resistance series: parental (0 nM), resistant_off_drug (0 nM), "
    "resistant_on_drug (1300/1500 nM).",
    "derivative_id": "Resistant derivative 'line:protocol:BR', e.g. H3122:Cmax:BR2.",
    "data_flags": "Known data issues, e.g. dose_suspect (300000 nM, probably 300 nM).",
}

INT_COLUMNS = {"dose_nM", "timepoint_h", "replicate"}
BOOL_COLUMNS = {"identity_mismatch"}
# Too many distinct values to list; describe_metadata shows a count and examples.
FREE_TEXT_COLUMNS = {"sample_id", "matrix_sample_id", "label", "notes"}
SAMPLE_COLUMNS = list(COLUMN_DOCS)

# Default sort: what "ordered by treatment" usually means.
DEFAULT_ORDER = [
    "dataset",
    "cell_line",
    "chronic_state",
    "resistance_protocol",
    "derivation_replicate",
    "construct",
    "compound",
    "dose_nM",
    "timepoint_h",
    "sample_id",
]
# Always returned with expression values (the guardrail context), when not empty.
EXPRESSION_CONTEXT = [
    "sample_id",
    "dataset",
    "batch",
    "cell_line",
    "identity_status",
    "identity_call",
    "perturbation_class",
    "compound",
    "dose_nM",
    "timepoint_h",
    "chronic_state",
    "resistance_protocol",
    "derivation_replicate",
    "construct",
]
IDENTITY_COLUMNS = ["identity_status", "identity_call", "identity_mismatch"]
# What makes two samples "the same condition" (for flagging pooled groups).
CONDITION_COLUMNS = [
    "perturbation_class",
    "compound",
    "dose_nM",
    "timepoint_h",
    "chronic_state",
    "construct",
]

# Reply-size controls. Claude Code warns above ~10k tokens and truncates at 25k;
# numeric CSV runs ~3 characters per token, so 40k characters stays well under 25k.
MAX_TABLE_CHARS = 40_000
MAX_GENES = 50
MAX_GROUP_GENES = 10


# ---------------------------------------------------------------- helpers


def round_sig(x, digits: int = 4):
    """Round to `digits` significant figures, but never round away integer digits
    (so 1234567 counts stay exact and 0.000123 TPM doesn't become 0). NaN -> None."""
    if x is None or isinstance(x, (bool, int, str)):
        return x
    if math.isnan(x) or math.isinf(x):
        return None
    if x == 0:
        return 0
    decimals = max(0, digits - 1 - math.floor(math.log10(abs(x))))
    r = round(x, decimals)
    return int(r) if decimals == 0 else r


def to_csv(columns: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for row in rows:
        w.writerow(["" if v is None else ("|".join(v) if isinstance(v, list) else v) for v in row])
    return buf.getvalue()


def suggest(value: str, choices, n: int = 5) -> str:
    choices = [str(c) for c in choices]
    lower = {c.lower(): c for c in choices}
    close = difflib.get_close_matches(str(value).lower(), list(lower), n=n, cutoff=0.5)
    return ", ".join(lower[c] for c in close) or "none close"


def table_envelope(
    columns: list[str], rows: list[list], offset: int, limit: int, query_echo: dict
) -> dict:
    """Page `rows` by limit and by the character budget; say whether the table is complete."""
    total = len(rows)
    page = rows[offset : offset + limit]
    # Shrink the page until the CSV fits the budget.
    text = to_csv(columns, page)
    while len(text) > MAX_TABLE_CHARS and len(page) > 1:
        page = page[: max(1, int(len(page) * MAX_TABLE_CHARS / len(text) * 0.95))]
        text = to_csv(columns, page)
    end = offset + len(page)
    return {
        "query": query_echo,
        "n_rows_total": total,
        "returned": len(page),
        "offset": offset,
        "complete": offset == 0 and end >= total,
        "next_offset": end if end < total else None,
        "table_csv": text,
    }


# ---------------------------------------------------------------- filters


LIST_COLUMNS = {"data_flags", "flags"}


def normalise_filters(
    db: Database,
    filters: dict | None,
    rows: list[dict] | None = None,
    columns: list[str] | None = None,
    help_tool: str = "describe_metadata",
) -> dict:
    """Check filter keys and values against the vocabulary and map them to stored
    values (case-insensitive), so Claude gets a precise error rather than 0 rows.
    Works on samples by default, or on any rows (e.g. contrasts) given `columns`."""
    rows = db.samples if rows is None else rows
    columns = SAMPLE_COLUMNS if columns is None else columns
    out = {}
    for col, raw in (filters or {}).items():
        if col not in columns:
            raise QueryError(
                f"Unknown column '{col}'. Closest: {suggest(col, columns)}. "
                f"Call {help_tool} for the full list."
            )
        values = raw if isinstance(raw, list) else [raw]
        if not values:
            raise QueryError(f"Filter '{col}' has an empty list.")
        if col == "notes":
            # substring match; None matches samples without notes
            out[col] = [None if v is None else str(v).lower() for v in values]
            continue
        if col in LIST_COLUMNS:
            # None matches rows without any flag
            known = {f for r in rows for f in (r[col] or [])}
            for v in values:
                if v is not None and str(v) not in known:
                    raise QueryError(f"No row has {col} '{v}'. Known values: {sorted(known)}.")
            out[col] = [None if v is None else str(v) for v in values]
            continue
        levels = {r[col] for r in rows}
        mapped = []
        for v in values:
            if v is None:
                mapped.append(None)
            elif col in INT_COLUMNS:
                try:
                    if isinstance(v, bool):
                        raise TypeError(v)
                    number = int(v)
                except (TypeError, ValueError):
                    raise QueryError(f"'{col}' takes integers; got {v!r}.") from None
                if number not in levels:
                    present = sorted(lv for lv in levels if lv is not None)
                    raise QueryError(f"No sample has {col}={number}. Values present: {present}.")
                mapped.append(number)
            elif col in BOOL_COLUMNS:
                if str(v).lower() not in ("true", "false"):
                    raise QueryError(f"'{col}' takes true or false; got {v!r}.")
                mapped.append(str(v).lower() == "true")
            else:
                match = {str(lv).lower(): lv for lv in levels if lv is not None}.get(str(v).lower())
                if match is None:
                    raise QueryError(
                        f"No sample has {col}={v!r}. Closest values: "
                        f"{suggest(v, [lv for lv in levels if lv is not None])}."
                    )
                mapped.append(match)
        out[col] = mapped
    return out


def matches(sample: dict, filters: dict) -> bool:
    for col, wanted in filters.items():
        value = sample[col]
        if col == "notes":
            if not any(
                (value is None) if w is None else (w in (value or "").lower()) for w in wanted
            ):
                return False
        elif col in LIST_COLUMNS:
            if not any((not value) if w is None else (w in (value or [])) for w in wanted):
                return False
        elif value not in wanted:
            return False
    return True


def select_samples(
    db: Database, filters: dict | None, exclude_identity_status: list[str] | None
) -> tuple[list[dict], dict, int]:
    """Samples matching the filters, minus excluded identity statuses.
    Returns (samples, normalised filters, number excluded by identity)."""
    norm = normalise_filters(db, filters)
    excluded = set(exclude_identity_status or [])
    known = {s["identity_status"] for s in db.samples}
    if excluded - known:
        raise QueryError(
            f"Unknown identity_status {sorted(excluded - known)}; valid: {sorted(known)}."
        )
    hits = [s for s in db.samples if matches(s, norm)]
    kept = [s for s in hits if s["identity_status"] not in excluded]
    return kept, norm, len(hits) - len(kept)


def sort_rows(rows: list[dict], order_by: list[str] | None, allowed: list[str]) -> list[dict]:
    spec = []
    for item in order_by or DEFAULT_ORDER:
        col, _, direction = item.strip().partition(" ")
        if col not in allowed:
            raise QueryError(f"Can't order by '{col}'. Closest: {suggest(col, allowed)}.")
        if direction.lower() not in ("", "asc", "desc"):
            raise QueryError(f"Order direction must be asc or desc; got '{direction}'.")
        spec.append((col, direction.lower() == "desc"))
    # Stable sorts applied last key first give a multi-key sort. Empty values and
    # 'none' always come first, so untreated controls sort before drug arms.
    out = list(rows)
    for col, desc in reversed(spec):
        empty = [r for r in out if r[col] is None or r[col] == "none"]
        empty.sort(key=lambda r, c=col: r[c] is not None)  # None before 'none'
        filled = [r for r in out if r[col] is not None and r[col] != "none"]
        numeric = all(isinstance(r[col], (int, float)) for r in filled)
        filled.sort(key=(lambda r, c=col: r[c]) if numeric else (lambda r, c=col: str(r[c])),
                    reverse=desc)  # fmt: skip
        out = empty + filled
    return out


# ---------------------------------------------------------------- tools


def dataset_summary(db: Database) -> dict:
    out = []
    for d in db.datasets:
        rows = [s for s in db.samples if s["dataset"] == d["dataset"]]

        def counts(col, rows=rows):
            return dict(
                sorted(Counter("(empty)" if r[col] is None else str(r[col]) for r in rows).items())
            )

        out.append(
            {
                "dataset": d["dataset"],
                "accession": d["accession"],
                "public": d["public"],
                "n_samples": d["n_samples"],
                "n_cell_lines": d["n_cell_lines"],
                "design": d["design"],
                "caveats": d["caveats"],
                "perturbation_class": counts("perturbation_class"),
                "compound": counts("compound"),
                "dose_nM": counts("dose_nM"),
                "timepoint_h": counts("timepoint_h"),
                "batch": counts("batch"),
                "identity_status": counts("identity_status"),
            }
        )
    return {
        "datasets": out,
        "pipeline": "nf-core/rnaseq 3.14.0 (STAR+RSEM), Ensembl GRCh38 r113; same gene IDs in all",
        "n_samples_total": len(db.samples),
        "n_genes": len(db.genes),
    }


def describe_metadata(
    db: Database, columns: list[str] | None = None, dataset: str | None = None
) -> dict:
    cols = columns or SAMPLE_COLUMNS
    for c in cols:
        if c not in COLUMN_DOCS:
            raise QueryError(f"Unknown column '{c}'. Closest: {suggest(c, SAMPLE_COLUMNS)}.")
    rows = db.samples
    if dataset is not None:
        rows = [s for s in rows if s["dataset"].lower() == dataset.lower()]
        if not rows:
            raise QueryError(
                f"Unknown dataset '{dataset}'. Valid: {sorted({s['dataset'] for s in db.samples})}."
            )
    out = []
    for c in cols:
        entry = {"column": c, "description": COLUMN_DOCS[c]}
        if c == "data_flags":
            entry["levels"] = dict(Counter(f for r in rows for f in (r[c] or [])))
            entry["n_without_flags"] = sum(1 for r in rows if not r[c])
        elif c in FREE_TEXT_COLUMNS:
            values = [r[c] for r in rows if r[c] is not None]
            entry["n_distinct"] = len(set(values))
            entry["n_empty"] = len(rows) - len(values)
            entry["examples"] = sorted(set(values))[:3]
        else:
            levels = Counter(r[c] for r in rows)
            entry["levels"] = {
                ("null" if k is None else str(k)): v
                for k, v in sorted(levels.items(), key=lambda kv: (kv[0] is None, str(kv[0])))
            }
        out.append(entry)
    return {"dataset": dataset, "n_samples": len(rows), "columns": out}


def find_samples(
    db: Database,
    filters: dict | None = None,
    columns: list[str] | None = None,
    exclude_identity_status: list[str] | None = ("suspect",),
    order_by: list[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    samples, norm, n_excluded = select_samples(db, filters, list(exclude_identity_status or []))
    extra = columns or ["dataset", "cell_line", "perturbation_class", "compound", "dose_nM",
                        "timepoint_h"]  # fmt: skip
    for c in extra:
        if c not in COLUMN_DOCS:
            raise QueryError(f"Unknown column '{c}'. Closest: {suggest(c, SAMPLE_COLUMNS)}.")
    out_cols = list(dict.fromkeys(["sample_id", "label", *extra, *IDENTITY_COLUMNS]))
    ordered = sort_rows(samples, order_by, SAMPLE_COLUMNS)
    env = table_envelope(
        out_cols,
        [[s[c] for c in out_cols] for s in ordered],
        offset,
        limit,
        {"filters": norm, "exclude_identity_status": list(exclude_identity_status or [])},
    )
    env["n_excluded_by_identity"] = n_excluded
    return env


ENSG = re.compile(r"^ENSG\d{11}(\.\d+)?$", re.IGNORECASE)


class GeneIndex:
    """Lookups over the genes table, built once per database."""

    def __init__(self, db: Database):
        self.by_id = {g["gene_id"]: g for g in db.genes}
        self.by_symbol = defaultdict(list)
        for g in db.genes:
            if g["gene_symbol"]:
                self.by_symbol[g["gene_symbol"].lower()].append(g)
        self.symbols = sorted({g["gene_symbol"] for g in db.genes if g["gene_symbol"]})


def gene_index(db: Database) -> GeneIndex:
    """Built once per Database and stored on it."""
    if getattr(db, "_gene_index", None) is None:
        db._gene_index = GeneIndex(db)
    return db._gene_index


def gene_row(g: dict, query: str, match: str) -> dict:
    desc = g["description"] or ""
    return {
        "query": query,
        "match": match,
        "gene_id": g["gene_id"],
        "gene_symbol": g["gene_symbol"],
        "gene_biotype": g["gene_biotype"],
        "is_ambiguous": g["symbol_is_ambiguous"],
        "description": desc.split(" [Source:")[0][:120] or None,
    }


def resolve_genes(db: Database, query: list[str], limit_per_query: int = 10) -> dict:
    idx = gene_index(db)
    results = []
    if any(not q.strip() for q in query):
        raise QueryError("Gene queries must not be blank.")
    for q in query:
        q_clean = q.strip()
        if ENSG.match(q_clean):
            gid = q_clean.split(".")[0].upper()
            g = idx.by_id.get(gid)
            results.extend([gene_row(g, q, "gene_id")] if g else [])
            if not g:
                results.append({"query": q, "match": "none", "note": "no such Ensembl gene"})
            continue
        exact = idx.by_symbol.get(q_clean.lower(), [])
        if exact:
            results.extend(gene_row(g, q, "symbol") for g in exact[:limit_per_query])
            if len(exact) > limit_per_query:
                results.append(
                    {
                        "query": q,
                        "match": "truncated",
                        "note": f"{len(exact)} genes share this symbol; showing "
                        f"{limit_per_query}. Ask the user which one, or use gene_ids.",
                    }
                )
            continue
        prefix = [
            g
            for sym in idx.symbols
            if sym.lower().startswith(q_clean.lower())
            for g in idx.by_symbol[sym.lower()]
        ]
        if prefix:
            results.extend(gene_row(g, q, "prefix") for g in prefix[:limit_per_query])
            if len(prefix) > limit_per_query:
                results.append(
                    {"query": q, "match": "truncated", "note": f"{len(prefix)} prefix matches"}
                )
            continue
        results.append(
            {"query": q, "match": "none", "note": f"closest symbols: {suggest(q, idx.symbols)}"}
        )
    return {"results": results}


def genes_for_expression(db: Database, genes: list[str]) -> list[dict]:
    """Accept gene_ids, or symbols that map to exactly one gene."""
    if not genes:
        raise QueryError("Give at least one gene.")
    if len(genes) > MAX_GENES:
        raise QueryError(f"At most {MAX_GENES} genes per call; got {len(genes)}.")
    idx = gene_index(db)
    out = []
    for q in genes:
        q_clean = q.strip()
        if ENSG.match(q_clean):
            g = idx.by_id.get(q_clean.split(".")[0].upper())
            if g is None:
                raise QueryError(f"No gene with id {q}. Use resolve_genes to search.")
        else:
            hits = idx.by_symbol.get(q_clean.lower(), [])
            if not hits:
                raise QueryError(
                    f"No gene named '{q}'. Closest: {suggest(q, idx.symbols)}. "
                    "Use resolve_genes to search."
                )
            if len(hits) > 1:
                ids = ", ".join(h["gene_id"] for h in hits[:10])
                raise QueryError(
                    f"'{q}' matches {len(hits)} Ensembl genes ({ids}). Pass one gene_id."
                )
            g = hits[0]
        if g not in out:
            out.append(g)
    return out


def rank_within_dataset(
    order_by: list[str] | None, value_cols: list[str], samples: list[dict]
) -> tuple[list[str] | None, list[str]]:
    """Sorting by an expression value across datasets would rank TPM across
    cohorts, which is not meaningful. Put dataset first in that case."""
    if not order_by or len({s["dataset"] for s in samples}) < 2:
        return order_by, []
    keys = [item.strip().partition(" ")[0] for item in order_by]
    if keys[0] == "dataset" or not set(keys) & set(value_cols):
        return order_by, []
    note = (
        "Sorted by dataset first: expression values are ranked within each dataset, "
        "because TPM is not comparable across datasets."
    )
    return ["dataset", *order_by], [note]


def get_expression(
    db: Database,
    genes: list[str],
    filters: dict | None = None,
    unit: str = "tpm",
    exclude_identity_status: list[str] | None = ("suspect",),
    order_by: list[str] | None = None,
    group_by: list[str] | None = None,
    columns: list[str] | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> dict:
    gene_rows = genes_for_expression(db, genes)
    samples, norm, n_excluded = select_samples(db, filters, list(exclude_identity_status or []))
    if not samples:
        raise QueryError("No samples match these filters. Use find_samples to check them.")
    values = db.expression(
        [g["gene_idx"] for g in gene_rows], [s["sample_idx"] for s in samples], unit
    )
    # Column per gene: the symbol when it names one gene, otherwise the gene_id.
    symbols = Counter(g["gene_symbol"] for g in gene_rows)
    gene_cols = [
        g["gene_symbol"]
        if g["gene_symbol"] and not g["symbol_is_ambiguous"] and symbols[g["gene_symbol"]] == 1
        else g["gene_id"]
        for g in gene_rows
    ]
    gene_map = [
        {"column": c, "gene_id": g["gene_id"], "gene_symbol": g["gene_symbol"]}
        for c, g in zip(gene_cols, gene_rows, strict=True)
    ]
    echo = {
        "genes": gene_map,
        "unit": unit,
        "filters": norm,
        "exclude_identity_status": list(exclude_identity_status or []),
    }
    notes = [
        "Values are "
        + ("TPM" if unit == "tpm" else "RSEM expected counts (raw, depth-dependent)")
        + ". Compare within a dataset only; TPM is not comparable across datasets."
    ]
    if n_excluded:
        notes.append(f"{n_excluded} sample(s) excluded by identity_status.")

    if group_by:
        if len(gene_rows) > MAX_GROUP_GENES:
            raise QueryError(f"group_by takes at most {MAX_GROUP_GENES} genes.")
        for c in group_by:
            if c not in COLUMN_DOCS or c in FREE_TEXT_COLUMNS or c == "data_flags":
                raise QueryError(f"Can't group by '{c}'. Closest: {suggest(c, SAMPLE_COLUMNS)}.")
        # Groups never span datasets: dataset is always the first key. Cell-line groups
        # use molecular identity, so reassigned DFCI032 libraries never pool into H2228.
        if "cell_line" in group_by:
            group_by = ["effective_cell_line" if c == "cell_line" else c for c in group_by]
            notes.append(
                "Grouped by effective_cell_line (molecular identity) instead of the recorded "
                "cell_line, so reassigned samples sit with the line they genetically are."
            )
        keys = list(dict.fromkeys(["dataset", *group_by]))
        groups = defaultdict(list)
        for s in samples:
            groups[tuple(s[k] for k in keys)].append(s)
        cols = [*keys, "n", "n_identity_mismatch", "n_conditions"]
        for gc in gene_cols:
            cols += [f"{gc}_mean", f"{gc}_sd", f"{gc}_median", f"{gc}_min", f"{gc}_max"]
        rows = []
        for key, members in groups.items():
            row = dict(zip(keys, key, strict=True))
            row["n"] = len(members)
            row["n_identity_mismatch"] = sum(bool(s["identity_mismatch"]) for s in members)
            row["n_conditions"] = len({tuple(s[c] for c in CONDITION_COLUMNS) for s in members})
            for g, gc in zip(gene_rows, gene_cols, strict=True):
                vals = [values[(g["gene_idx"], s["sample_idx"])] for s in members]
                row[f"{gc}_mean"] = round_sig(statistics.fmean(vals))
                row[f"{gc}_sd"] = round_sig(statistics.stdev(vals)) if len(vals) > 1 else None
                row[f"{gc}_median"] = round_sig(statistics.median(vals))
                row[f"{gc}_min"] = round_sig(min(vals))
                row[f"{gc}_max"] = round_sig(max(vals))
            rows.append(row)
        stat_cols = cols[len(keys) + 3 :]
        if any(r["n_conditions"] > 1 for r in rows):
            notes.append(
                "Some groups pool different conditions (n_conditions > 1), e.g. parental and "
                "resistant, or drug and control. Add perturbation_class, compound, dose_nM, "
                "timepoint_h or chronic_state to group_by, or say the values are pooled."
            )
        order, note = rank_within_dataset(order_by or keys, stat_cols, samples)
        notes += note
        rows = sort_rows(rows, order, cols)
        env = table_envelope(cols, [[r[c] for c in cols] for r in rows], offset, limit, echo)
        env["notes"] = notes + ["One row per group; groups never span datasets."]
        return env

    extra = [c for c in (columns or []) if c not in EXPRESSION_CONTEXT]
    for c in extra:
        if c not in COLUMN_DOCS:
            raise QueryError(f"Unknown column '{c}'. Closest: {suggest(c, SAMPLE_COLUMNS)}.")
    rows = []
    for s in samples:
        row = {c: s[c] for c in [*EXPRESSION_CONTEXT, *extra]}
        for g, gc in zip(gene_rows, gene_cols, strict=True):
            row[gc] = round_sig(values[(g["gene_idx"], s["sample_idx"])])
        rows.append(row)
    order, note = rank_within_dataset(order_by, gene_cols, samples)
    notes += note
    rows = sort_rows(rows, order, [*EXPRESSION_CONTEXT, *extra, *gene_cols])
    # Drop context columns that are empty for every returned sample (keeps replies small),
    # but never ones the caller asked for.
    keep_always = {"sample_id", "dataset", "batch", "identity_status", *(columns or [])}
    context = [
        c
        for c in [*EXPRESSION_CONTEXT, *extra]
        if c in keep_always or any(r[c] is not None for r in rows)
    ]
    cols = [*context, *gene_cols]
    env = table_envelope(cols, [[r[c] for c in cols] for r in rows], offset, limit, echo)
    env["notes"] = notes
    return env


# ---------------------------------------------------------------- contrasts

CONTRAST_FILTER_COLUMNS = [
    "contrast_id",
    "kind",
    "dataset",
    "cell_line",
    "compound",
    "dose_nM",
    "timepoint_h",
    "resistance_protocol",
    "derivation_replicate",
    "derivative_id",
    "construct",
    "flags",
]
CONTRAST_ORDER = [
    "gene",
    "kind",
    "dataset",
    "cell_line",
    "compound",
    "dose_nM",
    "timepoint_h",
    "resistance_protocol",
    "derivation_replicate",
    "construct",
]
MAX_CONTRAST_GENES = 20
FLAG_HELP = (
    "untimed_control = drug arm is timed but controls are not (E11834); "
    "timepoint_unrecorded = neither arm has a timepoint (LOCAL); "
    "vehicle_unknown = 'none' may be untreated or vehicle; "
    "dose_unrecorded = public dataset without doses; "
    "dose_suspect = 300000 nM recorded, probably 300 nM; "
    "identity_reassigned = genotype is another line; "
    "identity_suspect = unresolved-identity samples included at your request; "
    "derivation_split:X = this lab derivation (as recorded) also has libraries that are "
    "genetically line X; those are counted under X, not here; "
    "identity_excluded:N = N suspect samples left out; "
    "no_reference / no_samples_after_exclusion = nothing to compare; "
    "n_lt_2_no_ci = a group has one sample, so no CI; "
    "BR_unusable:BRk = derivation k had no usable samples; "
    "did_se_inflated = difference in differences, wider CI; "
    "ev_is_dox_null = this row is the doxycycline-only effect."
)


def select_contrasts(db: Database, contrast_ids: list[str] | None, filters: dict | None) -> list:
    if not contrast_ids and not filters:
        raise QueryError("Give contrast_ids or filters. Use list_contrasts to browse them.")
    chosen = db.contrasts
    if contrast_ids:
        known = {c["contrast_id"]: c for c in db.contrasts}
        for cid in contrast_ids:
            if cid not in known:
                raise QueryError(f"Unknown contrast_id '{cid}'. Closest: {suggest(cid, known)}.")
        chosen = [known[cid] for cid in dict.fromkeys(contrast_ids)]
    if filters:
        norm = normalise_filters(
            db, filters, db.contrasts, CONTRAST_FILTER_COLUMNS, "list_contrasts"
        )
        chosen = [c for c in chosen if matches(c, norm)]
    if not chosen:
        raise QueryError("No contrast matches. Use list_contrasts to see what exists.")
    return chosen


def list_contrasts(
    db: Database, filters: dict | None = None, limit: int = 200, offset: int = 0
) -> dict:
    norm = normalise_filters(db, filters, db.contrasts, CONTRAST_FILTER_COLUMNS, "list_contrasts")
    chosen = [c for c in db.contrasts if matches(c, norm)]
    cols = [
        "contrast_id", "kind", "dataset", "cell_line", "compound", "dose_nM", "timepoint_h",
        "resistance_protocol", "derivation_replicate", "construct", "test_desc", "ref_desc",
        "n_test", "n_ref", "flags",
    ]  # fmt: skip
    rows = []
    for c in chosen:
        members = db.contrast_members.get(c["contrast_id"], {})
        rows.append(
            {
                **c,
                "n_test": len(members.get("test", [])),
                "n_ref": len(members.get("ref", [])),
            }
        )
    rows = sort_rows(rows, ["kind", "dataset", "cell_line", "compound", "dose_nM",
                            "timepoint_h", "resistance_protocol", "derivation_replicate",
                            "construct"], cols)  # fmt: skip
    env = table_envelope(
        cols, [[r[c] for c in cols] for r in rows], offset, limit, {"filters": norm}
    )
    env["notes"] = [
        (
            "n_test/n_ref count all member samples before identity exclusion; contrast reports "
            "the n actually used."
        )
    ]
    return env


def _batches(samples: list[dict]) -> str | None:
    counts = Counter(s["batch"] for s in samples if s["batch"] is not None)
    return ",".join(f"{b}:{n}" for b, n in sorted(counts.items())) or None


def contrast(
    db: Database,
    genes: list[str],
    contrast_ids: list[str] | None = None,
    filters: dict | None = None,
    level: str = "derivative",
    exclude_identity_status: list[str] | None = ("suspect",),
    order_by: list[str] | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> dict:
    if len(genes) > MAX_CONTRAST_GENES:
        raise QueryError(f"At most {MAX_CONTRAST_GENES} genes per contrast call.")
    if level not in ("derivative", "protocol"):
        raise QueryError("level must be 'derivative' or 'protocol'.")
    gene_rows = genes_for_expression(db, genes)
    chosen = select_contrasts(db, contrast_ids, filters)
    excluded = set(exclude_identity_status or [])
    by_idx = {s["sample_idx"]: s for s in db.samples}

    # Members per contrast and role, after identity exclusion.
    used, n_dropped = {}, {}
    for c in chosen:
        roles = db.contrast_members.get(c["contrast_id"], {})
        used[c["contrast_id"]] = {
            role: [by_idx[i] for i in idxs if by_idx[i]["identity_status"] not in excluded]
            for role, idxs in roles.items()
        }
        n_dropped[c["contrast_id"]] = sum(len(v) for v in roles.values()) - sum(
            len(v) for v in used[c["contrast_id"]].values()
        )
    all_idx = sorted({s["sample_idx"] for u in used.values() for v in u.values() for s in v})
    values = db.expression([g["gene_idx"] for g in gene_rows], all_idx, "tpm")

    rows = []
    for g in gene_rows:
        label = g["gene_symbol"] or g["gene_id"]
        for c in chosen:
            u = used[c["contrast_id"]]

            def summary(role, u=u, g=g):
                vals = [
                    cx.log2p1(values[(g["gene_idx"], s["sample_idx"])]) for s in u.get(role, [])
                ]
                return cx.summarise(vals)

            def mean_tpm(role, u=u, g=g):
                vals = [values[(g["gene_idx"], s["sample_idx"])] for s in u.get(role, [])]
                return statistics.fmean(vals) if vals else None

            t, r = summary("test"), summary("ref")
            parts = [(1, t), (-1, r)]
            null_fc = n_null = None
            arms = [t, r]
            if c["kind"] == "transgene_dox_corrected":
                nt, nr = summary("null_test"), summary("null_ref")
                arms += [nt, nr]
                parts += [(-1, nt), (1, nr)]
                null_fc = cx.welch_difference([(1, nt), (-1, nr)])["estimate"]
                n_null = f"{nt['n']}+{nr['n']}"
            est = cx.welch_difference(parts)
            flags = list(c["flags"] or [])
            if n_dropped[c["contrast_id"]]:
                flags.append(f"identity_excluded:{n_dropped[c['contrast_id']]}")
            if any(s["identity_status"] == "suspect" for v in u.values() for s in v):
                flags.append("identity_suspect")  # only when the caller included them
            if any(a["n"] == 0 for a in arms):
                flags.append("no_samples_after_exclusion")
            elif min(a["n"] for a in arms) < 2:
                flags.append("n_lt_2_no_ci")
            rows.append(
                {
                    "gene": label,
                    "gene_id": g["gene_id"],
                    **{k: c[k] for k in CONTRAST_FILTER_COLUMNS if k not in ("flags",)},
                    "log2fc": est["estimate"],
                    "ci_low": est["ci_low"],
                    "ci_high": est["ci_high"],
                    "se": est["se"],
                    "n_test": t["n"],
                    "n_ref": r["n"],
                    "mean_log2_test": t["mean"],
                    "mean_log2_ref": r["mean"],
                    "sd_test": t["sd"],
                    "sd_ref": r["sd"],
                    "mean_tpm_test": mean_tpm("test"),
                    "mean_tpm_ref": mean_tpm("ref"),
                    "batches_test": _batches(u.get("test", [])),
                    "batches_ref": _batches(u.get("ref", [])),
                    "null_log2fc": null_fc,
                    "n_null": n_null,
                    "n_br": None,
                    "br_estimates": None,
                    "flags": sorted(flags),
                }
            )

    if level == "protocol":
        rows = _summarise_protocols(rows)

    cols = [
        "gene", "contrast_id", "kind", "dataset", "cell_line", "compound", "dose_nM",
        "timepoint_h", "resistance_protocol", "derivation_replicate", "construct", "log2fc",
        "ci_low", "ci_high", "se", "n_test", "n_ref", "n_br", "br_estimates", "mean_log2_test",
        "mean_log2_ref", "sd_test", "sd_ref", "mean_tpm_test", "mean_tpm_ref", "batches_test",
        "batches_ref", "null_log2fc", "n_null", "flags",
    ]  # fmt: skip
    for r in rows:
        for k in cols:
            if isinstance(r.get(k), float):
                r[k] = round_sig(r[k])
    rows = sort_rows(rows, order_by or CONTRAST_ORDER, cols)
    keep = {"gene", "contrast_id", "log2fc", "n_test", "n_ref", "flags"}
    cols = [c for c in cols if c in keep or any(r.get(c) not in (None, []) for r in rows)]
    env = table_envelope(
        cols,
        [[r.get(c) for c in cols] for r in rows],
        offset,
        limit,
        {
            "genes": [g["gene_id"] for g in gene_rows],
            "n_contrasts": len(chosen),
            "level": level,
            "exclude_identity_status": sorted(excluded),
        },
    )
    env["method"] = (
        "log2fc = mean(log2(TPM+1)) of test minus reference (difference-in-differences for "
        "transgene_dox_corrected); 95% CI from a Welch t interval. Descriptive effect size: "
        "no p-values, no multiple-testing correction."
    )
    env["notes"] = [
        (
            "Every contrast is within one dataset and one cell line. Across datasets compare the "
            "direction of effects, not their size."
        ),
        "Flags: " + FLAG_HELP,
    ]
    if level == "protocol":
        env["notes"].append(
            "Chronic rows are summarised across derivation replicates (BR): log2fc is the mean "
            "of BR-level effects and n_br the number of independent derivations. For "
            "chronic_resistance all BRs share one parental reference, so the CI adds the "
            "parental's variance to the spread between BRs (Satterthwaite df); with n_br < 2 "
            "there is no CI."
        )
    return env


def _summarise_protocols(rows: list[dict]) -> list[dict]:
    """Collapse chronic BR-level rows into one row per gene x kind x line x protocol."""
    out, groups = [], defaultdict(list)
    for r in rows:
        if r["kind"] in cx.CHRONIC_KINDS:
            groups[
                (r["gene"], r["kind"], r["dataset"], r["cell_line"], r["resistance_protocol"])
            ].append(r)
        else:
            out.append(r)
    for (gene, kind, ds, line, protocol), members in groups.items():
        usable = [m for m in members if m["log2fc"] is not None]
        # chronic_resistance BRs share one parental reference; pass its variance so the
        # interval includes it (drug_dependence BRs each have their own reference).
        shared = None
        if kind == "chronic_resistance" and usable:
            shared = {"n": usable[0]["n_ref"], "sd": usable[0]["sd_ref"]}
        agg = cx.across_replicates([m["log2fc"] for m in usable], shared)
        per_br = {"no_samples_after_exclusion", "n_lt_2_no_ci"}
        flags = {
            f
            for m in members
            for f in m["flags"]
            if f not in per_br and not f.startswith("identity_excluded")
        }
        flags |= {f"BR_unusable:{m['derivation_replicate']}" for m in members if m not in usable}
        dropped = sum(
            int(f.split(":")[1])
            for m in members
            for f in m["flags"]
            if f.startswith("identity_excluded")
        )
        if dropped:
            flags.add(f"identity_excluded:{dropped}")
        flags = sorted(flags)
        out.append(
            {
                **{k: None for k in members[0]},
                "gene": gene,
                "gene_id": members[0]["gene_id"],
                "contrast_id": f"{kind}:{ds}:{line}:{protocol}:all_BR",
                "kind": kind,
                "dataset": ds,
                "cell_line": line,
                "compound": members[0]["compound"],
                "dose_nM": members[0]["dose_nM"],
                "resistance_protocol": protocol,
                "log2fc": agg["estimate"],
                "ci_low": agg["ci_low"],
                "ci_high": agg["ci_high"],
                "se": agg["se"],
                "n_br": agg["n"],
                "n_test": sum(m["n_test"] for m in usable),
                "n_ref": max((m["n_ref"] for m in usable), default=0),
                "br_estimates": "|".join(
                    f"{m['derivation_replicate']}:{round_sig(m['log2fc'])}" for m in usable
                ),
                "flags": flags,
            }
        )
    return out
