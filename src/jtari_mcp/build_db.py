"""Build the DuckDB database the server reads, from the five RSEM matrices and
sample_metadata.tsv.

    uv run jtari-build-db --data-dir "<folder with sample_metadata.tsv and the 5 result folders>"

Writes data/jtari.duckdb (gitignored). The build is all-or-nothing: it writes to a
temporary file, runs every consistency check, and only then moves the result into
place, so the server never sees a half-built database.

Tables:
  samples           one row per sample: the metadata, typed, plus derived columns
  genes             gene_id -> symbol, biotype, entrez, description
  expr              long table (gene_idx, sample_idx, tpm, counts), sorted by gene
  datasets          one row per dataset: design and caveats
  docs              reference documents served as MCP resources (METADATA_SCHEMA.md)
  contrasts         every valid comparison (contrasts.py), one row each
  contrast_members  (contrast_id, role, sample_idx): which samples form each side
  provenance_inputs every input file with its sha256
  provenance_build  key/value facts about this build
"""

import argparse
import csv
import datetime as dt
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

import duckdb

from jtari_mcp.contrasts import CONTRAST_COLUMNS, define_contrasts
from jtari_mcp.fetch_reference import DEFAULT_DEST as DEFAULT_GTF
from jtari_mcp.fetch_reference import GTF_SHA256, GTF_URL, sha256_of

# Bump when the table layout changes; the server refuses a database it doesn't know.
SCHEMA_VERSION = 2  # 2: contrasts + contrast_members

# Matrix file names are matched exactly, so strays like rsem.merged.gene_tpm.2.tsv
# (a 2-line fragment in e-mtab-11304_results/) are never picked up.
MATRIX_FILES = {"tpm": "rsem.merged.gene_tpm.tsv", "counts": "rsem.merged.gene_counts.tsv"}
# Only source of Entrez IDs and gene descriptions (its sample columns are ignored).
ANNOT_FILE = ("soellner_13807-nm", "rsem.merged.gene_counts.annot.tsv")

DATASETS = {
    "LOCAL": {
        "dir": "soellner_13807-nm",
        "accession": None,
        "public": False,
        "design": (
            "Unpublished Soellner lab series (13807-NM). H2228 and H3122: parental plus "
            "chronic-alectinib resistant derivatives (StartLow, StartIC50, Cmax x BR1-3), "
            "each on drug and at 0 nM; acute alectinib arms in 13 lines; parental-only "
            "baseline lines incl. non-lung ALK+ lines (ALCL, neuroblastoma)."
        ),
        "caveats": (
            "Acute arms and controls have no timepoint. Batch is balanced 2+2 within "
            "H2228/H3122 groups but parental-only lines are batch 1 only. The 'replicate' "
            "column is unreliable for 8 samples; use batch. Identity was assessed here only "
            "(somalier); some H2228 derivatives are DFCI032."
        ),
    },
    "E11304": {
        "dir": "e-mtab-11304_results",
        "accession": "E-MTAB-11304",
        "public": True,
        "design": (
            "NL20 isogenic: dox-inducible EML4-ALK V1, V3, KIF5B-ALK, TFG-ALK and empty "
            "vector (EV), each +/- doxycycline, n=6."
        ),
        "caveats": (
            "All fusions are transgenes. No timepoint or dox dose recorded. Doxycycline "
            "itself changes expression: EV +/- dox is the null."
        ),
    },
    "E11342": {
        "dir": "e-mtab-11342_results",
        "accession": "E-MTAB-11342",
        "public": True,
        "design": (
            "CUTO8, CUTO9, CUTO29 x {alectinib, brigatinib, lorlatinib}; YU1077 x "
            "{brigatinib, lorlatinib}; 6 h and 24 h, n=3; timed untreated controls."
        ),
        "caveats": (
            "Doses unrecorded (empty in the SDRF). 'none' controls may be untreated or vehicle."
        ),
    },
    "E11834": {
        "dir": "e-mtab-11834_results",
        "accession": "E-MTAB-11834",
        "public": True,
        "design": (
            "CUTO8, CUTO9, CUTO29, H2228, H3122 x {lorlatinib, trametinib} x {6 h, 24 h}, n=4."
        ),
        "caveats": (
            "Controls have no timepoint: one control group (n=8) per line serves both 6 h "
            "and 24 h. Doses unrecorded. 'none' controls may be untreated or vehicle."
        ),
    },
    "FL3C": {
        "dir": "fl3c",
        "accession": None,
        "public": True,
        "design": "Panel of 60 lung lines (mostly LUAD), baseline only, 2-3 replicates.",
        "caveats": "Baseline only: no perturbations. Mutation and proliferation notes in 'notes'.",
    },
}

INT_COLUMNS = {"dose_nM", "timepoint_h", "replicate"}

# E11304 construct codes in notes ('construct=V1; +dox') -> canonical construct name.
CONSTRUCTS = {
    "EV": "EV",
    "KIF5B": "KIF5B-ALK",
    "TFG": "TFG-ALK",
    "V1": "EML4-ALK_V1",
    "V3": "EML4-ALK_V3",
}

# Known data issues flagged per sample: LOCAL alectinib recorded at
# 300000 nM (300 uM) in CUTO46 and SNU2535 looks like a unit slip for 300 nM.
SUSPECT_DOSES_NM = {300000}


class BuildError(RuntimeError):
    """An input failed a consistency check; the database is not written."""


def sql_str(value: str | os.PathLike) -> str:
    """Quote a string (e.g. a file path with spaces) as an SQL literal."""
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------- metadata


def read_metadata(path: Path, keep: set[str]) -> list[dict]:
    """Parse sample_metadata.tsv. The csv module handles the quoted multi-line
    'notes' values that line-based tools (cut, awk) would split."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f, delimiter="\t") if r["dataset"] in keep]
    for r in rows:
        for k, v in r.items():
            if v == "":
                r[k] = None  # empty means "not applicable / unrecorded" everywhere
            elif k in INT_COLUMNS:
                r[k] = int(v)
    ids = [r["sample_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise BuildError("sample_id is not unique in sample_metadata.tsv")
    for r in rows:
        if r["sample_id"] != f"{r['dataset']}::{r['matrix_sample_id']}":
            raise BuildError(f"sample_id {r['sample_id']} != dataset::matrix_sample_id")
    return rows


def add_derived_columns(rows: list[dict]) -> None:
    """Columns the server needs that the metadata doesn't hold directly.
    `cell_line` is never changed (METADATA_SCHEMA.md: recorded values stay as supplied)."""
    for r in rows:
        reassigned = r["identity_status"] == "reassigned"
        r["effective_cell_line"] = r["identity_call"] if reassigned else r["cell_line"]
        r["identity_mismatch"] = bool(r["identity_call"]) and r["identity_call"] != r["cell_line"]

        r["construct"] = None
        if r["dataset"] == "E11304":
            m = re.search(r"construct=(\w+)", r["notes"] or "")
            if not m or m.group(1) not in CONSTRUCTS:
                raise BuildError(f"{r['sample_id']}: no recognisable construct in notes")
            r["construct"] = CONSTRUCTS[m.group(1)]

        r["derivative_id"] = None
        if r["perturbation_class"] == "chronic_resistance":
            # Keyed on molecular identity: a derivation whose libraries somalier splits
            # between H2228 and DFCI032 becomes two derivatives, never one mixed group.
            parts = (
                r["effective_cell_line"],
                r["resistance_protocol"],
                r["derivation_replicate"],
            )
            if None in parts:
                raise BuildError(f"{r['sample_id']}: chronic sample missing protocol or BR")
            r["derivative_id"] = ":".join(parts)

        flags = []
        if r["dose_nM"] in SUSPECT_DOSES_NM:
            flags.append("dose_suspect")
        r["data_flags"] = flags

    # chronic_state: parental at 0 nM for every line that has resistant derivatives
    # (by molecular identity, so DFCI032 parental pairs with the DFCI032-like derivatives).
    chronic_lines = {
        r["effective_cell_line"] for r in rows if r["perturbation_class"] == "chronic_resistance"
    }
    for r in rows:
        r["chronic_state"] = None
        if r["perturbation_class"] == "chronic_resistance":
            r["chronic_state"] = "resistant_off_drug" if r["dose_nM"] == 0 else "resistant_on_drug"
        elif (
            r["dataset"] == "LOCAL"
            and r["perturbation_class"] == "baseline"
            and r["dose_nM"] == 0
            and r["effective_cell_line"] in chronic_lines
        ):
            r["chronic_state"] = "parental"


SAMPLE_COLUMNS = [
    ("sample_idx", "SMALLINT PRIMARY KEY"),
    ("sample_id", "VARCHAR UNIQUE NOT NULL"),
    ("matrix_sample_id", "VARCHAR NOT NULL"),
    ("label", "VARCHAR"),
    ("dataset", "VARCHAR NOT NULL"),
    ("cell_line", "VARCHAR"),
    ("cell_line_raw", "VARCHAR"),
    ("parent_line", "VARCHAR"),
    ("lineage", "VARCHAR"),
    ("fusion", "VARCHAR"),
    ("fusion_source", "VARCHAR"),
    ("perturbation_class", "VARCHAR"),
    ("compound", "VARCHAR"),
    ("dose_nM", "INTEGER"),
    ("timepoint_h", "INTEGER"),
    ("resistance_protocol", "VARCHAR"),
    ("derivation_replicate", "VARCHAR"),
    ("replicate", "INTEGER"),
    ("batch", "VARCHAR"),
    ("source_lab", "VARCHAR"),
    ("identity_status", "VARCHAR"),
    ("identity_call", "VARCHAR"),
    ("identity_confidence", "VARCHAR"),
    ("identity_basis", "VARCHAR"),
    ("identity_qc_status", "VARCHAR"),
    ("notes", "VARCHAR"),
    # derived
    ("effective_cell_line", "VARCHAR"),
    ("identity_mismatch", "BOOLEAN"),
    ("construct", "VARCHAR"),
    ("chronic_state", "VARCHAR"),
    ("derivative_id", "VARCHAR"),
    ("data_flags", "VARCHAR[]"),
]


def load_samples(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
    cols = ", ".join(f'"{c}" {t}' for c, t in SAMPLE_COLUMNS)
    con.execute(f"CREATE TABLE samples ({cols})")
    names = [c for c, _ in SAMPLE_COLUMNS]
    placeholders = ", ".join("?" for _ in names)
    values = [[i if c == "sample_idx" else r[c] for c in names] for i, r in enumerate(rows)]
    con.executemany(f"INSERT INTO samples VALUES ({placeholders})", values)


# ---------------------------------------------------------------- matrices


def matrix_path(data_dir: Path, dataset: str, unit: str) -> Path:
    return data_dir / DATASETS[dataset]["dir"] / MATRIX_FILES[unit]


def read_header(path: Path) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.readline().rstrip("\n").split("\t")


def read_gene_ids(path: Path) -> list[str]:
    """First column of a matrix, in file order (streams the file; ~1 s for 100 MB)."""
    with open(path, encoding="utf-8") as f:
        next(f)
        return [line.split("\t", 1)[0] for line in f]


def check_matrices(data_dir: Path, rows: list[dict], datasets: list[str]) -> list[str]:
    """Every matrix must have the same genes in the same order, and its sample
    columns must be exactly the metadata's matrix_sample_ids for that dataset.
    Returns the shared gene_id list."""
    reference_genes = None
    for ds in datasets:
        expected = {r["matrix_sample_id"] for r in rows if r["dataset"] == ds}
        for unit in MATRIX_FILES:
            path = matrix_path(data_dir, ds, unit)
            if not path.exists():
                raise BuildError(f"missing matrix: {path}")
            header = read_header(path)
            if header[:2] != ["gene_id", "transcript_id(s)"]:
                raise BuildError(f"{path}: unexpected first columns {header[:2]}")
            samples = header[2:]
            if len(samples) != len(set(samples)):
                raise BuildError(f"{path}: duplicate sample columns")
            if set(samples) != expected:
                extra, missing = set(samples) - expected, expected - set(samples)
                raise BuildError(
                    f"{path}: columns differ from metadata "
                    f"(not in metadata: {sorted(extra)[:5]}, not in matrix: {sorted(missing)[:5]})"
                )
            genes = read_gene_ids(path)
            if reference_genes is None:
                reference_genes = genes
                if len(genes) != len(set(genes)):
                    raise BuildError(f"{path}: duplicate gene_id rows")
            elif genes != reference_genes:
                raise BuildError(f"{path}: gene_id rows differ from the other matrices")
    return reference_genes


def read_csv_sql(path: Path) -> str:
    # Sample columns parse as DOUBLE; pinning the candidate types avoids a slow full-file sniff.
    return (
        f"read_csv({sql_str(path)}, delim='\t', header=true, "
        "auto_type_candidates=['VARCHAR','DOUBLE'])"
    )


def unpivot_sql(path: Path) -> str:
    """Wide matrix -> long (matrix_sample_id, gene_id, v)."""
    return (
        f'(UNPIVOT (SELECT * EXCLUDE ("transcript_id(s)") FROM {read_csv_sql(path)}) '
        "ON COLUMNS(* EXCLUDE (gene_id)) INTO NAME matrix_sample_id VALUE v)"
    )


def load_expression(con: duckdb.DuckDBPyConnection, data_dir: Path, datasets: list[str]) -> None:
    con.execute(
        "CREATE TEMP TABLE staged (gene_idx INTEGER, sample_idx SMALLINT, tpm DOUBLE, counts DOUBLE)"
    )
    for ds in datasets:
        tpm = unpivot_sql(matrix_path(data_dir, ds, "tpm"))
        counts = unpivot_sql(matrix_path(data_dir, ds, "counts"))
        con.execute(
            f"""
            INSERT INTO staged
            SELECT g.gene_idx, s.sample_idx, t.v, c.v
            FROM {tpm} t
            JOIN {counts} c USING (matrix_sample_id, gene_id)
            JOIN samples s ON s.dataset = ? AND s.matrix_sample_id = t.matrix_sample_id
            JOIN genes g ON g.gene_id = t.gene_id
            """,
            [ds],
        )
        print(f"  loaded {ds}", file=sys.stderr)
    # Sorted by gene so a one-gene query reads one small contiguous block
    # (2.4 ms vs 15 ms unsorted with string keys, in benchmarks).
    con.execute("CREATE TABLE expr AS SELECT * FROM staged ORDER BY gene_idx, sample_idx")
    con.execute("DROP TABLE staged")


# ---------------------------------------------------------------- genes


def load_genes(
    con: duckdb.DuckDBPyConnection, gene_ids: list[str], gtf: Path, annot: Path | None
) -> None:
    con.execute("CREATE TEMP TABLE matrix_genes (gene_id VARCHAR)")
    con.executemany("INSERT INTO matrix_genes VALUES (?)", [[g] for g in gene_ids])
    con.execute(
        f"""
        CREATE TEMP TABLE gtf AS
        SELECT regexp_extract(attr, 'gene_id "([^"]+)"', 1) AS gene_id,
               NULLIF(regexp_extract(attr, 'gene_name "([^"]+)"', 1), '') AS gene_name,
               NULLIF(regexp_extract(attr, 'gene_biotype "([^"]+)"', 1), '') AS gene_biotype
        FROM read_csv({sql_str(gtf)}, delim='\t', header=false, auto_detect=false,
                      comment='#', quote='', escape='',
                      columns={{'chrom':'VARCHAR','src':'VARCHAR','feature':'VARCHAR',
                                'start':'BIGINT','end':'BIGINT','score':'VARCHAR',
                                'strand':'VARCHAR','frame':'VARCHAR','attr':'VARCHAR'}})
        WHERE feature = 'gene'
        """
    )
    if annot is not None:
        con.execute(
            f"""
            CREATE TEMP TABLE annot AS
            SELECT gene_id,
                   NULLIF(NULLIF(external_gene_name, '.'), '') AS symbol,
                   NULLIF(NULLIF(entrezgene_id, '.'), '') AS entrez_id,
                   NULLIF(NULLIF(description, '.'), '') AS description
            FROM read_csv({sql_str(annot)}, delim='\t', header=true, all_varchar=true)
            """
        )
    else:
        con.execute(
            "CREATE TEMP TABLE annot (gene_id VARCHAR, symbol VARCHAR, entrez_id VARCHAR, "
            "description VARCHAR)"
        )
    # Symbol and biotype from the Ensembl 113 GTF (the pipeline's annotation);
    # the annot file only fills a symbol the GTF lacks.
    con.execute(
        """
        CREATE TABLE genes AS
        WITH g AS (
            SELECT m.gene_id,
                   coalesce(gtf.gene_name, a.symbol) AS gene_symbol,
                   gtf.gene_biotype,
                   a.entrez_id,
                   a.description
            FROM matrix_genes m
            LEFT JOIN gtf USING (gene_id)
            LEFT JOIN annot a USING (gene_id)
        )
        SELECT (row_number() OVER (ORDER BY gene_id) - 1)::INTEGER AS gene_idx,
               gene_id, gene_symbol, gene_biotype, entrez_id, description,
               gene_symbol IS NOT NULL
                 AND count(*) OVER (PARTITION BY gene_symbol) > 1 AS symbol_is_ambiguous
        FROM g
        ORDER BY gene_idx
        """
    )
    con.execute("ALTER TABLE genes ADD PRIMARY KEY (gene_idx)")


# ---------------------------------------------------------------- the rest


def load_contrasts(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    contrasts = define_contrasts(rows)
    cols = ", ".join(f'"{c}" {t}' for c, t in CONTRAST_COLUMNS)
    con.execute(f"CREATE TABLE contrasts ({cols})")
    con.execute(
        "CREATE TABLE contrast_members (contrast_id VARCHAR, role VARCHAR, sample_idx SMALLINT)"
    )
    names = [c for c, _ in CONTRAST_COLUMNS]
    placeholders = ", ".join("?" for _ in names)
    con.executemany(
        f"INSERT INTO contrasts VALUES ({placeholders})",
        [[c[n] for n in names] for c in contrasts],
    )
    con.executemany(
        "INSERT INTO contrast_members VALUES (?, ?, ?)",
        [
            [c["contrast_id"], role, idx]
            for c in contrasts
            for role, idxs in c["members"].items()
            for idx in idxs
        ],
    )
    return len(contrasts)


def load_datasets(con: duckdb.DuckDBPyConnection, datasets: list[str]) -> None:
    con.execute(
        """
        CREATE TABLE datasets (dataset VARCHAR PRIMARY KEY, accession VARCHAR,
          public BOOLEAN, n_samples INTEGER, n_cell_lines INTEGER, design VARCHAR,
          caveats VARCHAR, pipeline VARCHAR)
        """
    )
    for ds in datasets:
        d = DATASETS[ds]
        n_samples, n_lines = con.execute(
            "SELECT count(*), count(DISTINCT cell_line) FROM samples WHERE dataset = ?", [ds]
        ).fetchone()
        con.execute(
            "INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ds,
                d["accession"],
                d["public"],
                n_samples,
                n_lines,
                d["design"],
                d["caveats"],
                "nf-core/rnaseq 3.14.0 --aligner star_rsem, Ensembl GRCh38 r113",
            ],
        )


def load_docs(con: duckdb.DuckDBPyConnection, schema_doc: Path | None) -> None:
    con.execute("CREATE TABLE docs (name VARCHAR PRIMARY KEY, content VARCHAR)")
    if schema_doc is not None:
        con.execute(
            "INSERT INTO docs VALUES ('METADATA_SCHEMA.md', ?)", [schema_doc.read_text("utf-8")]
        )


def git_commit() -> tuple[str, bool]:
    here = Path(__file__).resolve().parent
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=here, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=here,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return "unknown", False


def load_provenance(
    con: duckdb.DuckDBPyConnection,
    inputs: list[dict],
    data_dir: Path,
    gtf_sha256: str,
    gtf_verified: bool,
) -> None:
    con.execute(
        """
        CREATE TABLE provenance_inputs (role VARCHAR, dataset VARCHAR, rel_path VARCHAR,
          sha256 VARCHAR, size_bytes BIGINT, mtime TIMESTAMPTZ, n_rows INTEGER,
          n_cols INTEGER, source_url VARCHAR)
        """
    )
    for item in inputs:
        path = item["path"]
        # Paths are stored relative to their folder, so no personal paths end up in the DB.
        rel = path.relative_to(data_dir) if path.is_relative_to(data_dir) else Path(path.name)
        st = path.stat()
        con.execute(
            "INSERT INTO provenance_inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                item["role"],
                item.get("dataset"),
                str(rel),
                sha256_of(path),
                st.st_size,
                dt.datetime.fromtimestamp(st.st_mtime, dt.UTC),
                item.get("n_rows"),
                item.get("n_cols"),
                item.get("source_url"),
            ],
        )
    commit, dirty = git_commit()
    facts = {
        "schema_version": SCHEMA_VERSION,
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "builder_git_commit": commit,
        "builder_git_dirty": dirty,
        "duckdb_version": duckdb.__version__,
        "python_version": platform.python_version(),
        "pipeline": "nf-core/rnaseq 3.14.0 --aligner star_rsem",
        "genome": "GRCh38",
        "ensembl_release": 113,
        "gtf_sha256": gtf_sha256,
        "gtf_verified": gtf_verified,
    }
    con.execute("CREATE TABLE provenance_build (key VARCHAR PRIMARY KEY, value VARCHAR)")
    con.executemany(
        "INSERT INTO provenance_build VALUES (?, ?)", [[k, str(v)] for k, v in facts.items()]
    )


def build(
    data_dir: Path,
    out: Path,
    gtf: Path,
    schema_doc: Path | None = None,
    exclude: tuple[str, ...] = (),
    memory_limit: str = "2GB",
    expected_gtf_sha256: str | None = GTF_SHA256,
) -> dict:
    """Build the database at `out`. Returns a summary of row counts.

    `expected_gtf_sha256=None` skips the GTF check (tests use a tiny synthetic GTF);
    provenance then records gtf_verified=False."""
    data_dir = Path(data_dir).resolve()
    unknown = set(exclude) - set(DATASETS)
    if unknown:
        raise BuildError(f"unknown dataset(s) to exclude: {sorted(unknown)}")
    datasets = [ds for ds in DATASETS if ds not in exclude]
    if not gtf.exists():
        raise BuildError(f"GTF not found: {gtf} (run: uv run jtari-fetch-reference)")
    gtf_sha256 = sha256_of(gtf)
    if expected_gtf_sha256 is not None and gtf_sha256 != expected_gtf_sha256:
        raise BuildError(f"{gtf} is not the pinned Ensembl r113 GTF (sha256 {gtf_sha256})")

    rows = read_metadata(data_dir / "sample_metadata.tsv", set(datasets))
    add_derived_columns(rows)
    for i, r in enumerate(rows):
        r["sample_idx"] = i
    print(f"metadata: {len(rows)} samples in {datasets}", file=sys.stderr)
    gene_ids = check_matrices(data_dir, rows, datasets)
    print(f"matrices: consistent, {len(gene_ids)} genes", file=sys.stderr)

    annot = data_dir.joinpath(*ANNOT_FILE)
    annot = annot if annot.exists() else None

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".building")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        con.execute(f"SET memory_limit = {sql_str(memory_limit)}")
        con.execute("SET preserve_insertion_order = false")
        load_samples(con, rows)
        load_genes(con, gene_ids, gtf, annot)
        # A gene_id repeated in the GTF or annot file would duplicate genes rows and,
        # through the join, that gene's expr rows. Catch it here.
        n_genes, n_distinct = con.execute(
            "SELECT count(*), count(DISTINCT gene_id) FROM genes"
        ).fetchone()
        if n_genes != len(gene_ids) or n_distinct != len(gene_ids):
            raise BuildError(f"genes has {n_genes} rows for {len(gene_ids)} matrix genes")
        load_expression(con, data_dir, datasets)
        load_datasets(con, datasets)
        load_docs(con, schema_doc)
        n_contrasts = load_contrasts(con, rows)

        inputs = [
            {
                "role": "sample_metadata",
                "path": data_dir / "sample_metadata.tsv",
                "n_rows": len(rows),
            }
        ]
        for ds in datasets:
            n_cols = sum(r["dataset"] == ds for r in rows)
            for unit in MATRIX_FILES:
                path = matrix_path(data_dir, ds, unit)
                inputs.append(
                    {
                        "role": unit,
                        "dataset": ds,
                        "path": path,
                        "n_rows": len(gene_ids),
                        "n_cols": n_cols,
                    }
                )
        if annot is not None:
            inputs.append({"role": "gene_annot", "path": annot})
        inputs.append({"role": "gtf", "path": gtf, "source_url": GTF_URL})
        if schema_doc is not None:
            inputs.append({"role": "schema_doc", "path": schema_doc})
        load_provenance(con, inputs, data_dir, gtf_sha256, expected_gtf_sha256 is not None)

        summary = {
            "samples": con.execute("SELECT count(*) FROM samples").fetchone()[0],
            "genes": con.execute("SELECT count(*) FROM genes").fetchone()[0],
            "expr_rows": con.execute("SELECT count(*) FROM expr").fetchone()[0],
            "contrasts": n_contrasts,
        }
        if summary["expr_rows"] != summary["samples"] * summary["genes"]:
            raise BuildError(f"expr has {summary['expr_rows']} rows, expected samples x genes")
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    con.close()
    tmp.replace(out)
    summary["path"] = str(out)
    summary["size_mb"] = round(out.stat().st_size / 1e6, 1)
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description="Build the JTARI expression database.")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=os.environ.get("JTARI_DATA_DIR"),
        help="folder with sample_metadata.tsv and the dataset folders (or set JTARI_DATA_DIR)",
    )
    p.add_argument("--out", type=Path, default=Path("data/jtari.duckdb"))
    p.add_argument("--gtf", type=Path, default=DEFAULT_GTF)
    p.add_argument(
        "--schema-doc",
        type=Path,
        default=Path("data/METADATA_SCHEMA.md"),
        help="METADATA_SCHEMA.md to serve as a resource (skipped with a warning if absent)",
    )
    p.add_argument(
        "--exclude-dataset",
        action="append",
        default=[],
        choices=list(DATASETS),
        help="leave a dataset out, e.g. LOCAL for a public-only build (repeatable)",
    )
    args = p.parse_args()
    if args.data_dir is None:
        p.error("--data-dir is required (or set JTARI_DATA_DIR)")
    schema_doc = args.schema_doc if args.schema_doc.exists() else None
    if schema_doc is None:
        print(
            f"warning: {args.schema_doc} not found; schema resource will be empty", file=sys.stderr
        )
    try:
        summary = build(args.data_dir, args.out, args.gtf, schema_doc, tuple(args.exclude_dataset))
    except BuildError as e:
        raise SystemExit(f"build failed: {e}")
    print(summary)


if __name__ == "__main__":
    main()
