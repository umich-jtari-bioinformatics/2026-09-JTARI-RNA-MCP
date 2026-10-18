"""Phase 2 tests for tools.py, on the synthetic fixture database (see conftest.py).
Expected values come from the fixture's formula, not from the code under test."""

import csv
import io

import pytest
from conftest import SAMPLES, tpm_value

from jtari_mcp import tools
from jtari_mcp.db import Database


@pytest.fixture
def db(built):
    return Database(built["out"])


def table(result: dict) -> list[dict]:
    return list(csv.DictReader(io.StringIO(result["table_csv"])))


def expected_tpm(gene_i: int, sample_id: str) -> float:
    ds = sample_id.split("::")[0]
    msids = [s["matrix_sample_id"] for s in SAMPLES if s["dataset"] == ds]
    return tools.round_sig(tpm_value(gene_i, msids.index(sample_id.split("::")[1])))


# ---------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (1234567.891, 1234568),
        (12.3456, 12.35),
        (0.000123456, 0.0001235),
        (0.0, 0),
        (float("nan"), None),
        (None, None),
    ],
)
def test_round_sig(x, expected):
    assert tools.round_sig(x) == expected


# ---------------------------------------------------------------- filters


def test_filters_are_case_insensitive_and_canonicalised(db):
    norm = tools.normalise_filters(db, {"compound": "LORLATINIB", "dose_nM": "0"})
    assert norm == {"compound": ["lorlatinib"], "dose_nM": [0]}


def test_unknown_column_and_value_suggest_fixes(db):
    with pytest.raises(tools.QueryError, match="Closest: compound"):
        tools.normalise_filters(db, {"compund": "none"})
    with pytest.raises(tools.QueryError, match="Closest values: lorlatinib"):
        tools.normalise_filters(db, {"compound": "lorlatnib"})


def test_null_filter_matches_empty(db):
    rows = table(tools.find_samples(db, {"dataset": "E11834", "timepoint_h": None}))
    assert [r["sample_id"] for r in rows] == ["E11834::CUTO29-ctrl-rep1"]


def test_notes_substring_filter(db):
    rows = table(tools.find_samples(db, {"notes": "tp53=p.r273h"}))
    assert [r["sample_id"] for r in rows] == ["FL3C::A427_rep1"]


# ---------------------------------------------------------------- find_samples


def test_suspect_excluded_by_default(db):
    result = tools.find_samples(db, {"dataset": "LOCAL"})
    ids = {r["sample_id"] for r in table(result)}
    assert "LOCAL::13807-NM-5" not in ids
    assert result["n_excluded_by_identity"] == 1
    all_ids = {
        r["sample_id"] for r in table(tools.find_samples(db, {"dataset": "LOCAL"}, None, []))
    }
    assert "LOCAL::13807-NM-5" in all_ids


def test_default_order_puts_untreated_first(db):
    rows = table(tools.find_samples(db, {"dataset": "E11342"}))
    assert [r["compound"] for r in rows] == ["none", "lorlatinib"]


def test_order_by_desc_is_stable(db):
    rows = table(
        tools.find_samples(db, {"dataset": "LOCAL"}, ["dose_nM"], [], ["dose_nM desc", "sample_id"])
    )
    doses = [r["dose_nM"] for r in rows]
    assert doses[0] == "300000"
    zeros = [r["sample_id"] for r in rows if r["dose_nM"] == "0"]
    assert zeros == sorted(zeros)


def test_paging(db):
    first = tools.find_samples(db, limit=5)
    assert first["returned"] == 5 and not first["complete"] and first["next_offset"] == 5
    rest = tools.find_samples(db, limit=100, offset=5)
    assert rest["next_offset"] is None
    assert first["n_rows_total"] == rest["n_rows_total"] == len(SAMPLES) - 1  # minus suspect


# ---------------------------------------------------------------- resolve_genes


def test_resolve_genes_tiers(db):
    out = tools.resolve_genes(db, ["alk", "ENSG00000141510.7", "TP", "Y_RNA", "NOPE1"])["results"]
    by_query = {}
    for r in out:
        by_query.setdefault(r["query"], []).append(r)
    assert by_query["alk"][0]["gene_id"] == "ENSG00000171094"
    assert by_query["ENSG00000141510.7"][0]["match"] == "gene_id"
    assert by_query["TP"][0]["gene_symbol"] == "TP53"
    assert {r["is_ambiguous"] for r in by_query["Y_RNA"]} == {True}
    assert len(by_query["Y_RNA"]) == 2
    assert by_query["NOPE1"][0]["match"] == "none"


def test_resolve_genes_caps_exact_matches(db):
    out = tools.resolve_genes(db, ["Y_RNA"], limit_per_query=1)["results"]
    assert [r["match"] for r in out] == ["symbol", "truncated"]


# ---------------------------------------------------------------- get_expression


def test_values_match_the_matrix(db):
    rows = table(tools.get_expression(db, ["ALK", "ENSG00000141510"]))
    assert len(rows) == len(SAMPLES) - 1
    for r in rows:
        assert float(r["ALK"]) == expected_tpm(0, r["sample_id"])
        assert float(r["TP53"]) == expected_tpm(1, r["sample_id"])


def test_counts_unit(db):
    rows = table(tools.get_expression(db, ["ALK"], {"sample_id": "FL3C::A427_rep1"}, "counts"))
    assert float(rows[0]["ALK"]) == tools.round_sig(tpm_value(0, 0) * 3)


def test_every_row_carries_dataset_and_batch(db):
    result = tools.get_expression(db, ["ALK"], {"dataset": "FL3C"})
    header = result["table_csv"].splitlines()[0].split(",")
    assert {"dataset", "batch", "identity_status"} <= set(header)
    # columns empty for every FL3C sample are dropped
    assert "compound" in header and "chronic_state" not in header
    assert any("not comparable across datasets" in n for n in result["notes"])


def test_ambiguous_symbol_needs_gene_id(db):
    with pytest.raises(tools.QueryError, match="matches 2 Ensembl genes"):
        tools.get_expression(db, ["Y_RNA"])
    rows = table(tools.get_expression(db, ["ENSG00000200001"], {"dataset": "FL3C"}))
    assert "ENSG00000200001" in rows[0]  # column named by gene_id, not the ambiguous symbol


def test_unknown_gene_and_empty_selection(db):
    with pytest.raises(tools.QueryError, match="No gene named 'ALKK'"):
        tools.get_expression(db, ["ALKK"])
    with pytest.raises(tools.QueryError, match="No samples match"):
        tools.get_expression(db, ["ALK"], {"sample_id": "LOCAL::13807-NM-5"})


def test_group_by_never_spans_datasets(db):
    result = tools.get_expression(db, ["ALK"], {"cell_line": "CUTO29"}, group_by=["compound"])
    rows = table(result)
    assert [(r["dataset"], r["compound"], r["n"]) for r in rows] == [
        ("E11342", "none", "1"),
        ("E11342", "lorlatinib", "1"),
        ("E11834", "none", "1"),
        ("E11834", "lorlatinib", "1"),
    ]
    sid = "E11834::CUTO29-lorla-24hr-rep1"
    lorla_e11834 = rows[3]
    assert float(lorla_e11834["ALK_mean"]) == expected_tpm(0, sid)
    assert lorla_e11834["ALK_sd"] == ""  # n=1 has no SD


def test_character_budget_pages(db, monkeypatch):
    monkeypatch.setattr(tools, "MAX_TABLE_CHARS", 400)
    result = tools.get_expression(db, ["ALK"])
    assert len(result["table_csv"]) <= 400
    assert not result["complete"] and result["next_offset"] == result["returned"]


# ---------------------------------------------------------------- metadata


def test_describe_metadata_counts_and_nulls(db):
    out = tools.describe_metadata(db, ["dose_nM", "notes"], "LOCAL")
    dose = out["columns"][0]
    assert dose["levels"] == {"0": 5, "1300": 1, "300000": 1}
    assert out["columns"][1]["n_empty"] == 7  # free text: counted, not enumerated
    with pytest.raises(tools.QueryError, match="Unknown dataset"):
        tools.describe_metadata(db, dataset="NOPE")


def test_dataset_summary(db):
    out = tools.dataset_summary(db)
    by_ds = {d["dataset"]: d for d in out["datasets"]}
    assert set(by_ds) == {"LOCAL", "E11304", "E11342", "E11834", "FL3C"}
    assert by_ds["LOCAL"]["public"] is False
    assert by_ds["E11834"]["timepoint_h"] == {"(empty)": 1, "24": 1}


# ---------------------------------------------------------------- review fixes (Phase 2)


def test_group_by_cell_line_uses_molecular_identity(db):
    """Reassigned DFCI032 libraries must never pool into an H2228 group."""
    result = tools.get_expression(
        db, ["ALK"], {"dataset": "LOCAL"}, group_by=["cell_line", "chronic_state"]
    )
    rows = {(r["effective_cell_line"], r["chronic_state"]): r for r in table(result)}
    assert rows[("H2228", "resistant_off_drug")]["n"] == "1"  # NM-2 only
    assert rows[("DFCI032", "resistant_off_drug")]["n"] == "1"  # NM-4, reassigned
    assert rows[("DFCI032", "resistant_off_drug")]["n_identity_mismatch"] == "1"
    assert any("effective_cell_line" in n for n in result["notes"])


def test_ranking_by_value_stays_within_dataset(db):
    result = tools.get_expression(db, ["ALK"], order_by=["ALK desc"])
    datasets = [r["dataset"] for r in table(result)]
    assert datasets == sorted(datasets)  # grouped by dataset, not interleaved
    assert any("ranked within each dataset" in n for n in result["notes"])
    one = tools.get_expression(db, ["ALK"], {"dataset": "LOCAL"}, order_by=["ALK desc"])
    values = [float(r["ALK"]) for r in table(one)]
    assert values == sorted(values, reverse=True)
    grouped = tools.get_expression(db, ["ALK"], group_by=["compound"], order_by=["ALK_mean desc"])
    assert [r["dataset"] for r in table(grouped)] == sorted(r["dataset"] for r in table(grouped))


def test_integer_filters_must_exist(db):
    with pytest.raises(tools.QueryError, match="Values present"):
        tools.normalise_filters(db, {"dose_nM": 999})
    with pytest.raises(tools.QueryError, match="takes integers"):
        tools.normalise_filters(db, {"dose_nM": True})


def test_null_notes_and_flags(db):
    no_notes = {r["sample_id"] for r in table(tools.find_samples(db, {"notes": None}, limit=100))}
    assert "FL3C::A427_rep1" not in no_notes and "LOCAL::13807-NM-1" in no_notes
    unflagged = table(tools.find_samples(db, {"dataset": "LOCAL", "data_flags": None}))
    assert "LOCAL::13807-NM-7" not in {r["sample_id"] for r in unflagged}


def test_requested_columns_are_kept_even_if_empty(db):
    result = tools.get_expression(db, ["ALK"], {"dataset": "FL3C"}, columns=["resistance_protocol"])
    assert "resistance_protocol" in result["table_csv"].splitlines()[0]


def test_blank_gene_query_is_rejected(db):
    with pytest.raises(tools.QueryError, match="blank"):
        tools.resolve_genes(db, ["  "])
