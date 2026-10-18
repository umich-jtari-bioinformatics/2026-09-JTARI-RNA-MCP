"""Phase 3 tests: the contrast space and its statistics.

Statistics are checked against scipy's own Welch t-test and t interval (an
independent implementation); contrast values against the fixture formula."""

import csv
import io
import math

import pytest
from conftest import SAMPLES, tpm_value
from scipy import stats

from jtari_mcp import contrasts as cx
from jtari_mcp import tools
from jtari_mcp.db import Database


@pytest.fixture
def db(built):
    return Database(built["out"])


def table(result: dict) -> list[dict]:
    return list(csv.DictReader(io.StringIO(result["table_csv"])))


def fixture_log2(gene_i: int, sample_id: str) -> float:
    ds, msid = sample_id.split("::")
    msids = [s["matrix_sample_id"] for s in SAMPLES if s["dataset"] == ds]
    return math.log2(tpm_value(gene_i, msids.index(msid)) + 1)


# ---------------------------------------------------------------- statistics


def test_welch_matches_scipy():
    a, b = [5.1, 5.6, 4.9, 5.3], [3.2, 3.9, 3.4]
    got = cx.welch_difference([(1, cx.summarise(a)), (-1, cx.summarise(b))])
    ref = stats.ttest_ind(a, b, equal_var=False)
    ci = ref.confidence_interval(0.95)
    assert got["estimate"] == pytest.approx(sum(a) / 4 - sum(b) / 3)
    assert got["ci_low"] == pytest.approx(ci.low)
    assert got["ci_high"] == pytest.approx(ci.high)
    assert got["df"] == pytest.approx(ref.df)


def test_difference_in_differences_estimate():
    groups = [[4.0, 4.2], [2.0, 2.2], [1.1, 1.3], [1.0, 1.2]]
    signs = [1, -1, -1, 1]
    got = cx.welch_difference([(s, cx.summarise(g)) for s, g in zip(signs, groups, strict=True)])
    means = [sum(g) / 2 for g in groups]
    assert got["estimate"] == pytest.approx((means[0] - means[1]) - (means[2] - means[3]))
    assert got["se"] == pytest.approx(math.sqrt(sum(0.02 / 2 for _ in groups)))


def test_small_n_gives_estimate_without_ci():
    got = cx.welch_difference([(1, cx.summarise([3.0])), (-1, cx.summarise([1.0, 2.0]))])
    assert got["estimate"] == pytest.approx(1.5) and got["ci_low"] is None
    empty = cx.welch_difference([(1, cx.summarise([])), (-1, cx.summarise([1.0]))])
    assert empty["estimate"] is None


def test_across_replicates_matches_scipy_interval():
    br = [-4.8, -4.5, -5.4]
    got = cx.across_replicates(br)
    ci = stats.t.interval(0.95, 2, loc=sum(br) / 3, scale=stats.sem(br))
    assert (got["ci_low"], got["ci_high"]) == pytest.approx(ci)


# ---------------------------------------------------------------- the contrast space


def test_expected_contrasts_exist(db):
    ids = {c["contrast_id"] for c in db.contrasts}
    assert {
        "acute:E11342:CUTO29:lorlatinib:NA:24h",
        "acute:E11834:CUTO29:lorlatinib:NA:24h",
        "acute:LOCAL:CUTO46:alectinib:300000nM:NA",
        "chronic_resistance:LOCAL:H2228:Cmax:BR1",
        "chronic_drug_dependence:LOCAL:H2228:Cmax:BR1",
        "chronic_resistance:LOCAL:DFCI032:Cmax:BR3",
        "chronic_resistance:LOCAL:H2228:Cmax:BR3",
        "transgene:E11304:NL20:EML4-ALK_V1",
        "transgene:E11304:NL20:EV",
        "transgene_dox_corrected:E11304:NL20:EML4-ALK_V1",
    } <= ids
    assert not any(":EV" in i for i in ids if i.startswith("transgene_dox_corrected"))


def test_contrasts_never_cross_dataset_or_cell_line(db):
    by_idx = {s["sample_idx"]: s for s in db.samples}
    for c in db.contrasts:
        members = [
            by_idx[i] for idxs in db.contrast_members[c["contrast_id"]].values() for i in idxs
        ]
        assert {m["dataset"] for m in members} == {c["dataset"]}
        assert {m["effective_cell_line"] for m in members} == {c["cell_line"]}


def test_reference_rules(db):
    flags = {c["contrast_id"]: c["flags"] for c in db.contrasts}
    members = db.contrast_members
    by_idx = {s["sample_idx"]: s["sample_id"] for s in db.samples}
    # E11342 has time-matched controls; E11834 controls are untimed
    assert "untimed_control" not in flags["acute:E11342:CUTO29:lorlatinib:NA:24h"]
    assert "untimed_control" in flags["acute:E11834:CUTO29:lorlatinib:NA:24h"]
    assert [by_idx[i] for i in members["acute:E11834:CUTO29:lorlatinib:NA:24h"]["ref"]] == [
        "E11834::CUTO29-ctrl-rep1"
    ]
    # resistance is measured off drug against the parental, both at 0 nM
    res = members["chronic_resistance:LOCAL:H2228:Cmax:BR1"]
    assert [by_idx[i] for i in res["test"]] == ["LOCAL::13807-NM-2"]
    assert [by_idx[i] for i in res["ref"]] == ["LOCAL::13807-NM-1"]
    dep = members["chronic_drug_dependence:LOCAL:H2228:Cmax:BR1"]
    assert [by_idx[i] for i in dep["test"]] == ["LOCAL::13807-NM-3"]
    # the reassigned derivative pairs with the DFCI032 parental
    dfci = members["chronic_resistance:LOCAL:DFCI032:Cmax:BR3"]
    assert [by_idx[i] for i in dfci["ref"]] == ["LOCAL::13807-NM-6"]
    assert "identity_reassigned" in flags["chronic_resistance:LOCAL:DFCI032:Cmax:BR3"]
    # CUTO46 has no untreated sample in the fixture
    assert "no_reference" in flags["acute:LOCAL:CUTO46:alectinib:300000nM:NA"]
    assert "dose_suspect" in flags["acute:LOCAL:CUTO46:alectinib:300000nM:NA"]


# ---------------------------------------------------------------- the contrast tool


def test_contrast_value_equals_difference_of_log2_means(db):
    rows = table(tools.contrast(db, ["ALK"], ["acute:E11342:CUTO29:lorlatinib:NA:24h"]))
    expected = fixture_log2(0, "E11342::CUTO29-lorla-24hr-rep1") - fixture_log2(
        0, "E11342::CUTO29-ctrl-24hr-rep1"
    )
    assert float(rows[0]["log2fc"]) == tools.round_sig(expected)
    assert rows[0]["n_test"] == "1" and "n_lt_2_no_ci" in rows[0]["flags"]


def test_dox_corrected_subtracts_the_ev_effect(db):
    rows = {
        r["contrast_id"]: r
        for r in table(
            tools.contrast(db, ["ALK"], filters={"kind": ["transgene", "transgene_dox_corrected"]})
        )
    }
    v1 = fixture_log2(0, "E11304::NL20-V1-dox-rep1") - fixture_log2(0, "E11304::NL20-V1-nodox-rep1")
    ev = fixture_log2(0, "E11304::NL20-EV-dox-rep1") - fixture_log2(0, "E11304::NL20-EV-nodox-rep1")
    did = rows["transgene_dox_corrected:E11304:NL20:EML4-ALK_V1"]
    assert float(did["log2fc"]) == tools.round_sig(v1 - ev)
    assert float(did["null_log2fc"]) == tools.round_sig(ev)


def test_suspect_samples_are_excluded_and_flagged(db):
    rows = table(tools.contrast(db, ["ALK"], ["chronic_resistance:LOCAL:H2228:Cmax:BR3"]))
    assert rows[0]["n_test"] == "0" and rows[0]["log2fc"] == ""
    assert "no_samples_after_exclusion" in rows[0]["flags"]
    assert "identity_excluded:1" in rows[0]["flags"]
    kept = table(
        tools.contrast(
            db, ["ALK"], ["chronic_resistance:LOCAL:H2228:Cmax:BR3"], exclude_identity_status=[]
        )
    )
    assert kept[0]["n_test"] == "1"


def test_protocol_level_summarises_br(db):
    rows = table(
        tools.contrast(
            db,
            ["ALK"],
            filters={"kind": "chronic_resistance", "cell_line": "H2228"},
            level="protocol",
        )
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["contrast_id"] == "chronic_resistance:LOCAL:H2228:Cmax:all_BR"
    assert row["n_br"] == "1" and "BR_unusable:BR3" in row["flags"]  # BR3 only had a suspect sample


def test_contrast_selection_errors(db):
    with pytest.raises(tools.QueryError, match="Give contrast_ids or filters"):
        tools.contrast(db, ["ALK"])
    with pytest.raises(tools.QueryError, match="Unknown contrast_id"):
        tools.contrast(db, ["ALK"], ["acute:E11834:CUTO29:lorlatinib:NA:6h"])
    with pytest.raises(tools.QueryError, match="Closest: kind"):
        tools.list_contrasts(db, {"knd": "acute"})


def test_list_contrasts(db):
    rows = table(tools.list_contrasts(db, {"kind": "transgene"}))
    assert [r["construct"] for r in rows] == ["EML4-ALK_V1", "EV"]
    assert rows[1]["flags"] == "ev_is_dox_null"


def test_group_by_flags_pooled_conditions(db):
    result = tools.get_expression(db, ["ALK"], {"dataset": "LOCAL"}, group_by=["cell_line"])
    h2228 = next(r for r in table(result) if r["effective_cell_line"] == "H2228")
    assert int(h2228["n_conditions"]) > 1
    assert any("pool different conditions" in n for n in result["notes"])


# ---------------------------------------------------------------- review fixes (Phase 3)


def test_protocol_ci_includes_shared_parental_variance():
    """Two BRs that agree closely must not give a tighter interval than the
    parental mean's own uncertainty (the bug the reviewer found)."""
    br = [-2.372, -2.36]
    parental = {"n": 4, "sd": 0.197}
    naive = cx.across_replicates(br)
    fixed = cx.across_replicates(br, parental)
    se_expected = math.sqrt(stats.tvar(br) / 2 + 0.197**2 / 4)
    assert fixed["se"] == pytest.approx(se_expected)
    assert fixed["se"] > 0.197 / 2  # at least the parental SE
    assert (fixed["ci_high"] - fixed["ci_low"]) > (naive["ci_high"] - naive["ci_low"])
    assert cx.across_replicates(br, {"n": 1, "sd": None})["ci_low"] is None


def test_did_df_is_satterthwaite():
    groups = [[4.0, 4.4, 4.1], [2.0, 2.6], [1.1, 1.3, 1.0, 1.5], [1.0, 1.2]]
    signs = [1, -1, -1, 1]
    got = cx.welch_difference([(s, cx.summarise(g)) for s, g in zip(signs, groups, strict=True)])
    terms = [stats.tvar(g) / len(g) for g in groups]
    df = sum(terms) ** 2 / sum(t**2 / (len(g) - 1) for t, g in zip(terms, groups, strict=True))
    assert got["df"] == pytest.approx(df)


def test_derivation_split_is_flagged(db):
    flags = {c["contrast_id"]: c["flags"] for c in db.contrasts}
    # fixture H2228 Cmax BR3: one library reassigned to DFCI032, one suspect H2228
    assert "derivation_split:DFCI032" in flags["chronic_resistance:LOCAL:H2228:Cmax:BR3"]
    assert "derivation_split:H2228" in flags["chronic_resistance:LOCAL:DFCI032:Cmax:BR3"]
    assert not any(
        f.startswith("derivation_split") for f in flags["chronic_resistance:LOCAL:H2228:Cmax:BR1"]
    )


def test_included_suspect_samples_are_flagged(db):
    cid = "chronic_resistance:LOCAL:H2228:Cmax:BR3"
    default = table(tools.contrast(db, ["ALK"], [cid]))[0]
    assert "identity_suspect" not in default["flags"]
    included = table(tools.contrast(db, ["ALK"], [cid], exclude_identity_status=[]))[0]
    assert "identity_suspect" in included["flags"]


def test_unknown_flag_filter_is_an_error(db):
    with pytest.raises(tools.QueryError, match="Known values"):
        tools.list_contrasts(db, {"flags": "bogus"})
    rows = table(tools.list_contrasts(db, {"flags": "untimed_control"}))
    assert rows and all("untimed_control" in r["flags"] for r in rows)
