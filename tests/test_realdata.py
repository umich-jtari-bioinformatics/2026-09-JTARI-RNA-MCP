"""Checks against the real database. Skipped unless JTARI_DB points at a built
database (never in CI: the inputs include unpublished data).

    JTARI_DB=data/jtari.duckdb uv run pytest tests/test_realdata.py
"""

import os
from pathlib import Path

import duckdb
import pytest

DB = os.environ.get("JTARI_DB")
pytestmark = pytest.mark.skipif(
    not DB or not Path(DB).exists(), reason="set JTARI_DB to a built database"
)


@pytest.fixture(scope="module")
def con():
    with duckdb.connect(DB, read_only=True) as c:
        yield c


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def test_counts(con):
    """Numbers verified independently against the input files during planning."""
    assert one(con, "SELECT count(*) FROM samples") == 679
    assert one(con, "SELECT count(*) FROM genes") == 78932
    assert one(con, "SELECT count(*) FROM expr") == 679 * 78932
    per_dataset = dict(con.execute("SELECT dataset, count(*) FROM samples GROUP BY 1").fetchall())
    assert per_dataset == {"LOCAL": 234, "E11304": 60, "E11342": 90, "E11834": 120, "FL3C": 175}


def test_classes_and_identity(con):
    classes = dict(
        con.execute("SELECT perturbation_class, count(*) FROM samples GROUP BY 1").fetchall()
    )
    assert classes == {
        "baseline": 281,
        "acute_drug": 202,
        "chronic_resistance": 136,
        "transgene_induction": 60,
    }
    status = dict(
        con.execute("SELECT identity_status, count(*) FROM samples GROUP BY 1").fetchall()
    )
    assert status == {"unverified": 599, "confirmed": 60, "reassigned": 12, "suspect": 8}
    assert one(con, "SELECT count(*) FROM samples WHERE identity_mismatch") == 20


def test_derived_columns(con):
    assert one(con, "SELECT count(DISTINCT construct) FROM samples") == 5
    assert one(con, "SELECT count(*) FROM samples WHERE chronic_state = 'parental'") == 10
    assert one(con, "SELECT count(*) FROM samples WHERE 'dose_suspect' = ANY(data_flags)") == 4


def test_genes(con):
    assert one(con, "SELECT gene_symbol FROM genes WHERE gene_id = 'ENSG00000171094'") == "ALK"
    assert (
        one(con, "SELECT count(DISTINCT gene_symbol) FROM genes WHERE symbol_is_ambiguous") == 480
    )


def test_no_derivative_mixes_cell_lines(con):
    """Each derivative_id holds one molecular identity (somalier-split derivations become two)."""
    mixed = one(
        con,
        "SELECT count(*) FROM (SELECT derivative_id FROM samples WHERE derivative_id IS NOT NULL "
        "GROUP BY 1 HAVING count(DISTINCT effective_cell_line) > 1)",
    )
    assert mixed == 0
    assert one(con, "SELECT count(DISTINCT derivative_id) FROM samples") == 20


DATA_DIR = os.environ.get("JTARI_DATA_DIR")


@pytest.mark.skipif(not DATA_DIR, reason="set JTARI_DATA_DIR to the raw data folder")
def test_get_expression_matches_raw_matrices():
    """Ground truth read straight from the RSEM files with the csv module,
    never through our SQL: every ALK and DUSP6 TPM the tool returns for E11834
    and LOCAL must equal the raw value (to the tool's 4 significant figures)."""
    import csv

    from jtari_mcp import tools
    from jtari_mcp.build_db import DATASETS
    from jtari_mcp.db import Database

    db = Database(Path(DB))
    wanted = {"ENSG00000171094": "ALK", "ENSG00000139318": "DUSP6"}
    for ds in ("E11834", "LOCAL"):
        raw = {}
        path = Path(DATA_DIR) / DATASETS[ds]["dir"] / "rsem.merged.gene_tpm.tsv"
        with open(path) as f:
            reader = csv.reader(f, delimiter="\t")
            header = next(reader)
            for row in reader:
                if row[0] in wanted:
                    for msid, v in zip(header[2:], row[2:], strict=True):
                        raw[(wanted[row[0]], f"{ds}::{msid}")] = float(v)
        result = tools.get_expression(db, ["ALK", "DUSP6"], {"dataset": ds}, limit=2000)
        rows = list(csv.DictReader(result["table_csv"].splitlines()))
        assert result["complete"] and len(rows) > 100
        for r in rows:
            for sym in ("ALK", "DUSP6"):
                assert float(r[sym]) == tools.round_sig(raw[(sym, r["sample_id"])])


def test_contrasts_are_within_one_dataset_and_line(con):
    assert one(con, "SELECT count(*) FROM contrasts") == 115
    bad = one(
        con,
        """SELECT count(*) FROM (
             SELECT c.contrast_id FROM contrasts c
             JOIN contrast_members m USING (contrast_id) JOIN samples s USING (sample_idx)
             GROUP BY c.contrast_id, c.dataset, c.cell_line
             HAVING count(DISTINCT s.dataset) > 1 OR count(DISTINCT s.effective_cell_line) > 1
                 OR any_value(s.dataset) <> c.dataset
                 OR any_value(s.effective_cell_line) <> c.cell_line)""",
    )
    assert bad == 0


@pytest.mark.skipif(not DATA_DIR, reason="set JTARI_DATA_DIR to the raw data folder")
def test_contrast_matches_independent_computation():
    """log2FC recomputed from the raw RSEM files and the raw metadata TSV, choosing
    samples by their names/labels, never through the build's pairing code."""
    import csv
    import math
    import statistics

    from jtari_mcp import tools
    from jtari_mcp.db import Database

    def raw_tpm(subdir, gene_id):
        with open(Path(DATA_DIR) / subdir / "rsem.merged.gene_tpm.tsv") as f:
            reader = csv.reader(f, delimiter="\t")
            header = next(reader)
            for row in reader:
                if row[0] == gene_id:
                    return dict(zip(header[2:], map(float, row[2:]), strict=True))
        raise KeyError(gene_id)

    def lfc(test, ref):
        def mean_log(vals):
            return statistics.fmean(math.log2(v + 1) for v in vals)

        return mean_log(test) - mean_log(ref)

    db = Database(Path(DB))
    dusp6 = "ENSG00000139318"

    # E11834 CUTO8 lorlatinib 6 h vs the 8 untimed controls, chosen by matrix name
    tpm = raw_tpm("e-mtab-11834_results", dusp6)
    test = [v for k, v in tpm.items() if k.startswith("CUTO8-lorla-6hr-")]
    ref = [v for k, v in tpm.items() if k.startswith("CUTO8-control-")]
    assert (len(test), len(ref)) == (4, 8)
    got = tools.contrast(db, [dusp6], ["acute:E11834:CUTO8:lorlatinib:NA:6h"])
    row = next(csv.DictReader(got["table_csv"].splitlines()))
    assert float(row["log2fc"]) == tools.round_sig(lfc(test, ref))

    # H3122 Cmax BR1 off drug vs parental (both 0 nM), chosen by metadata label
    with open(Path(DATA_DIR) / "sample_metadata.tsv", newline="") as f:
        meta = list(csv.DictReader(f, delimiter="\t"))
    labels = {m["label"]: m["matrix_sample_id"] for m in meta if m["dataset"] == "LOCAL"}
    tpm = raw_tpm("soellner_13807-nm", dusp6)
    test = [tpm[v] for k, v in labels.items() if k.startswith("H3122-LOCAL-Cmax_0nM_BR1-")]
    ref = [tpm[v] for k, v in labels.items() if k.startswith("H3122-LOCAL-baseline_0nM-")]
    assert (len(test), len(ref)) == (4, 4)
    got = tools.contrast(db, [dusp6], ["chronic_resistance:LOCAL:H3122:Cmax:BR1"])
    row = next(csv.DictReader(got["table_csv"].splitlines()))
    assert float(row["log2fc"]) == tools.round_sig(lfc(test, ref))
