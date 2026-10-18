"""Phase 1 tests: the build script, on synthetic inputs (see conftest.py)."""

import hashlib

import duckdb
import pytest
from conftest import GENES, SAMPLES, tpm_value

from jtari_mcp import build_db


def query(path, sql, params=None):
    with duckdb.connect(str(path), read_only=True) as con:
        return con.execute(sql, params or []).fetchall()


def test_row_counts(built):
    n_s, n_g = len(SAMPLES), len(GENES)
    assert query(built["out"], "SELECT count(*) FROM samples") == [(n_s,)]
    assert query(built["out"], "SELECT count(*) FROM genes") == [(n_g,)]
    assert query(built["out"], "SELECT count(*) FROM expr") == [(n_s * n_g,)]


def test_every_value_round_trips_exactly(built):
    """Each stored TPM and count equals the number written into the matrix file."""
    rows = query(
        built["out"],
        """SELECT g.gene_id, s.dataset, s.matrix_sample_id, e.tpm, e.counts
           FROM expr e JOIN genes g USING (gene_idx) JOIN samples s USING (sample_idx)""",
    )
    gene_pos = {gid: i for i, (gid, _, _) in enumerate(GENES)}
    for gene_id, ds, msid, tpm, counts in rows:
        msids = [s["matrix_sample_id"] for s in SAMPLES if s["dataset"] == ds]
        expected = tpm_value(gene_pos[gene_id], msids.index(msid))
        assert tpm == expected
        assert counts == expected * 3


def test_expr_is_sorted_by_gene(built):
    rows = query(built["out"], "SELECT gene_idx, sample_idx FROM expr")
    assert rows == sorted(rows)


def test_empty_means_null_and_zero_stays_zero(built):
    doses = dict(query(built["out"], "SELECT sample_id, dose_nM FROM samples"))
    assert doses["LOCAL::13807-NM-1"] == 0
    assert doses["E11342::CUTO29-lorla-24hr-rep1"] is None
    tps = dict(query(built["out"], "SELECT sample_id, timepoint_h FROM samples"))
    assert tps["E11342::CUTO29-lorla-24hr-rep1"] == 24
    assert tps["E11834::CUTO29-ctrl-rep1"] is None


def test_multiline_notes_survive(built):
    notes = query(built["out"], "SELECT notes FROM samples WHERE sample_id = 'FL3C::A427_rep1'")
    assert notes == [("subtype=LUAD; TP53=p.R273H\nsecond line of notes",)]


def test_shared_matrix_sample_id_gives_two_samples(built):
    rows = query(
        built["out"],
        "SELECT sample_id FROM samples WHERE matrix_sample_id = 'CUTO29-lorla-24hr-rep1' ORDER BY 1",
    )
    assert rows == [("E11342::CUTO29-lorla-24hr-rep1",), ("E11834::CUTO29-lorla-24hr-rep1",)]


def test_identity_columns(built):
    rows = {
        r[0]: r[1:]
        for r in query(
            built["out"],
            "SELECT sample_id, cell_line, effective_cell_line, identity_mismatch FROM samples",
        )
    }
    # reassigned: cell_line kept as recorded, molecular identity in effective_cell_line
    assert rows["LOCAL::13807-NM-4"] == ("H2228", "DFCI032", True)
    # suspect: flagged, but not regrouped
    assert rows["LOCAL::13807-NM-5"] == ("H2228", "H2228", True)
    assert rows["LOCAL::13807-NM-2"] == ("H2228", "H2228", False)
    assert rows["FL3C::A427_rep1"] == ("A427", "A427", False)


def test_chronic_state_and_derivative_id(built):
    rows = {
        r[0]: r[1:]
        for r in query(built["out"], "SELECT sample_id, chronic_state, derivative_id FROM samples")
    }
    assert rows["LOCAL::13807-NM-1"] == ("parental", None)
    assert rows["LOCAL::13807-NM-2"] == ("resistant_off_drug", "H2228:Cmax:BR1")
    assert rows["LOCAL::13807-NM-3"] == ("resistant_on_drug", "H2228:Cmax:BR1")
    # DFCI032 parental counts as a parental because a reassigned derivative is DFCI032
    assert rows["LOCAL::13807-NM-6"] == ("parental", None)
    # one derivation split by somalier: reassigned libraries form a DFCI032 derivative,
    # the suspect one stays under H2228, so no group ever mixes two cell lines
    assert rows["LOCAL::13807-NM-4"] == ("resistant_off_drug", "DFCI032:Cmax:BR3")
    assert rows["LOCAL::13807-NM-5"] == ("resistant_off_drug", "H2228:Cmax:BR3")
    # acute arm and public samples have no chronic state
    assert rows["LOCAL::13807-NM-7"] == (None, None)
    assert rows["E11834::CUTO29-ctrl-rep1"] == (None, None)


def test_construct_and_flags(built):
    constructs = dict(query(built["out"], "SELECT sample_id, construct FROM samples"))
    assert constructs["E11304::NL20-V1-nodox-rep1"] == "EML4-ALK_V1"
    assert constructs["E11304::NL20-EV-dox-rep1"] == "EV"
    assert constructs["LOCAL::13807-NM-1"] is None
    flags = dict(query(built["out"], "SELECT sample_id, data_flags FROM samples"))
    assert flags["LOCAL::13807-NM-7"] == ["dose_suspect"]
    assert flags["LOCAL::13807-NM-1"] == []


def test_genes_table(built):
    rows = {
        r[0]: r[1:]
        for r in query(
            built["out"],
            "SELECT gene_id, gene_symbol, gene_biotype, entrez_id, symbol_is_ambiguous FROM genes",
        )
    }
    assert rows["ENSG00000171094"] == ("ALK", "protein_coding", "1000", False)
    assert rows["ENSG00000200001"][3] is True and rows["ENSG00000200002"][3] is True
    assert rows["ENSG00000300003"] == (None, "lncRNA", "1004", False)


def test_docs_and_provenance(built):
    assert query(built["out"], "SELECT name FROM docs") == [("METADATA_SCHEMA.md",)]
    prov = dict(query(built["out"], "SELECT key, value FROM provenance_build"))
    assert prov["schema_version"] == str(build_db.SCHEMA_VERSION)
    assert prov["ensembl_release"] == "113"
    inputs = query(built["out"], "SELECT role, rel_path, sha256 FROM provenance_inputs")
    meta = next(r for r in inputs if r[0] == "sample_metadata")
    expected = hashlib.sha256((built["data_dir"] / "sample_metadata.tsv").read_bytes()).hexdigest()
    assert meta == ("sample_metadata", "sample_metadata.tsv", expected)
    # 1 metadata + 5 datasets x 2 units + annot + gtf + schema doc
    assert len(inputs) == 14
    assert not any(r[1].startswith("/") for r in inputs)


def test_exclude_local(inputs):
    build_db.build(
        inputs["data_dir"],
        inputs["out"],
        inputs["gtf"],
        exclude=("LOCAL",),
        expected_gtf_sha256=None,
    )
    datasets = query(inputs["out"], "SELECT DISTINCT dataset FROM samples ORDER BY 1")
    assert ("LOCAL",) not in datasets and len(datasets) == 4


def test_matrix_column_not_in_metadata_fails(inputs):
    path = inputs["data_dir"] / "fl3c" / "rsem.merged.gene_tpm.tsv"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0] + "\tEXTRA"] + [ln + "\t1.0" for ln in lines[1:]]) + "\n")
    with pytest.raises(build_db.BuildError, match="columns differ from metadata"):
        build_db.build(inputs["data_dir"], inputs["out"], inputs["gtf"], expected_gtf_sha256=None)


def test_gene_order_mismatch_fails(inputs):
    path = inputs["data_dir"] / "fl3c" / "rsem.merged.gene_counts.tsv"
    header, *rows = path.read_text().splitlines()
    path.write_text("\n".join([header, *reversed(rows)]) + "\n")
    with pytest.raises(build_db.BuildError, match="gene_id rows differ"):
        build_db.build(inputs["data_dir"], inputs["out"], inputs["gtf"], expected_gtf_sha256=None)


def test_failed_build_keeps_previous_database(built):
    before = built["out"].read_bytes()
    (built["data_dir"] / "fl3c" / "rsem.merged.gene_tpm.tsv").unlink()
    with pytest.raises(build_db.BuildError, match="missing matrix"):
        build_db.build(built["data_dir"], built["out"], built["gtf"], expected_gtf_sha256=None)
    assert built["out"].read_bytes() == before


def test_unpinned_gtf_is_refused(inputs):
    with pytest.raises(build_db.BuildError, match="not the pinned Ensembl r113 GTF"):
        build_db.build(inputs["data_dir"], inputs["out"], inputs["gtf"])
    assert not inputs["out"].exists()
    assert not inputs["out"].with_name(inputs["out"].name + ".building").exists()


def test_duplicate_gene_in_annotation_fails(inputs):
    annot = inputs["data_dir"].joinpath(*build_db.ANNOT_FILE)
    lines = annot.read_text().splitlines()
    annot.write_text("\n".join([*lines, lines[1]]) + "\n")
    with pytest.raises(build_db.BuildError, match="genes has"):
        build_db.build(inputs["data_dir"], inputs["out"], inputs["gtf"], expected_gtf_sha256=None)


def test_provenance_shape(built):
    rows = query(
        built["out"],
        "SELECT role, dataset, n_rows, n_cols FROM provenance_inputs WHERE role = 'tpm' "
        "AND dataset = 'LOCAL'",
    )
    assert rows == [("tpm", "LOCAL", len(GENES), 7)]
    prov = dict(query(built["out"], "SELECT key, value FROM provenance_build"))
    assert prov["gtf_verified"] == "False"
