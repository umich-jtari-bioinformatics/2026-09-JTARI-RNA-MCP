"""Synthetic inputs in the same layout and formats as the real data directory.

Values are invented (tpm = 10*gene + sample + 0.25, counts = 3*tpm), so tests can
check every number exactly and no real (unpublished) data ever enters git or CI.
The sample rows cover the cases the build must handle: multi-line notes, empty vs
0 doses, reassigned and suspect identities, chronic derivatives on and off drug,
E11304 constructs, a matrix_sample_id shared by two datasets, a suspect dose.
"""

import csv
import gzip
from pathlib import Path

import pytest

from jtari_mcp import build_db

METADATA_COLUMNS = [
    "sample_id", "matrix_sample_id", "label", "dataset", "cell_line", "cell_line_raw",
    "parent_line", "lineage", "fusion", "fusion_source", "perturbation_class", "compound",
    "dose_nM", "timepoint_h", "resistance_protocol", "derivation_replicate", "replicate",
    "batch", "source_lab", "identity_status", "identity_call", "identity_confidence",
    "identity_basis", "identity_qc_status", "notes",
]  # fmt: skip

# (gene_id, gene_name or None, biotype); two genes share the symbol Y_RNA.
GENES = [
    ("ENSG00000171094", "ALK", "protein_coding"),
    ("ENSG00000141510", "TP53", "protein_coding"),
    ("ENSG00000200001", "Y_RNA", "misc_RNA"),
    ("ENSG00000200002", "Y_RNA", "misc_RNA"),
    ("ENSG00000300003", None, "lncRNA"),
]


def sample(dataset, msid, cell_line, pclass, **kw):
    row = dict.fromkeys(METADATA_COLUMNS, "")
    row.update(
        sample_id=f"{dataset}::{msid}",
        matrix_sample_id=msid,
        label=f"{cell_line}-{dataset}-{msid}",
        dataset=dataset,
        cell_line=cell_line,
        cell_line_raw=cell_line,
        perturbation_class=pclass,
        compound="none",
        replicate="1",
        identity_status="unverified",
    )
    row.update({k: str(v) for k, v in kw.items()})
    return row


SAMPLES = [
    # LOCAL: parentals, a derivative on/off drug, a reassigned and a suspect derivative sample
    sample("LOCAL", "13807-NM-1", "H2228", "baseline", dose_nM=0, batch=1,
           identity_status="confirmed", identity_call="H2228"),
    sample("LOCAL", "13807-NM-2", "H2228", "chronic_resistance", dose_nM=0, batch=1,
           resistance_protocol="Cmax", derivation_replicate="BR1",
           identity_status="confirmed", identity_call="H2228"),
    sample("LOCAL", "13807-NM-3", "H2228", "chronic_resistance", compound="alectinib",
           dose_nM=1300, batch=2, resistance_protocol="Cmax", derivation_replicate="BR1",
           identity_status="confirmed", identity_call="H2228"),
    sample("LOCAL", "13807-NM-4", "H2228", "chronic_resistance", dose_nM=0, batch=1,
           resistance_protocol="Cmax", derivation_replicate="BR3",
           identity_status="reassigned", identity_call="DFCI032"),
    sample("LOCAL", "13807-NM-5", "H2228", "chronic_resistance", dose_nM=0, batch=2,
           resistance_protocol="Cmax", derivation_replicate="BR3",
           identity_status="suspect", identity_call="unresolved"),
    sample("LOCAL", "13807-NM-6", "DFCI032", "baseline", dose_nM=0, batch=1,
           identity_status="confirmed", identity_call="DFCI032"),
    sample("LOCAL", "13807-NM-7", "CUTO46", "acute_drug", compound="alectinib",
           dose_nM=300000, batch=1),
    # E11304: constructs come from notes
    sample("E11304", "NL20-V1-dox-rep1", "NL20", "transgene_induction",
           compound="doxycycline", notes="construct=V1; +dox"),
    sample("E11304", "NL20-V1-nodox-rep1", "NL20", "transgene_induction",
           notes="construct=V1; -dox"),
    sample("E11304", "NL20-EV-dox-rep1", "NL20", "transgene_induction",
           compound="doxycycline", notes="construct=EV; +dox; empty-vector control"),
    sample("E11304", "NL20-EV-nodox-rep1", "NL20", "transgene_induction",
           notes="construct=EV; -dox; empty-vector control"),
    # E11342 and E11834 share the matrix_sample_id CUTO29-lorla-24hr-rep1
    sample("E11342", "CUTO29-lorla-24hr-rep1", "CUTO29", "acute_drug",
           compound="lorlatinib", timepoint_h=24),
    sample("E11342", "CUTO29-ctrl-24hr-rep1", "CUTO29", "baseline", timepoint_h=24),
    sample("E11834", "CUTO29-lorla-24hr-rep1", "CUTO29", "acute_drug",
           compound="lorlatinib", timepoint_h=24),
    sample("E11834", "CUTO29-ctrl-rep1", "CUTO29", "baseline"),
    # FL3C: a quoted multi-line note
    sample("FL3C", "A427_rep1", "A427", "baseline",
           notes="subtype=LUAD; TP53=p.R273H\nsecond line of notes"),
]  # fmt: skip

DIRS = {ds: spec["dir"] for ds, spec in build_db.DATASETS.items()}


def tpm_value(gene_i: int, sample_j: int) -> float:
    return 10 * gene_i + sample_j + 0.25


def write_matrix(path: Path, msids: list[str], unit: str, genes=GENES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["gene_id", "transcript_id(s)", *msids])
        for i, (gid, _, _) in enumerate(genes):
            vals = [tpm_value(i, j) * (3 if unit == "counts" else 1) for j in range(len(msids))]
            w.writerow([gid, f"ENST{i:011d}", *vals])


def write_inputs(root: Path) -> dict:
    data = root / "data_dir"
    data.mkdir(parents=True)
    with open(data / "sample_metadata.tsv", "w", newline="") as f:
        w = csv.DictWriter(f, METADATA_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(SAMPLES)
    for ds, d in DIRS.items():
        msids = [s["matrix_sample_id"] for s in SAMPLES if s["dataset"] == ds]
        for unit, fname in build_db.MATRIX_FILES.items():
            write_matrix(data / d / fname, msids, unit)
    # the stray fragment that must be ignored
    (data / DIRS["E11304"] / "rsem.merged.gene_tpm.2.tsv").write_text("gene_id\tjunk\n")
    with open(data.joinpath(*build_db.ANNOT_FILE), "w") as f:
        f.write("gene_id\tentrezgene_id\texternal_gene_name\tdescription\ttranscript_id(s)\n")
        f.writelines(
            f"{gid}\t{1000 + i}\t{name or '.'}\tdesc {i}\tENST{i:011d}\n"
            for i, (gid, name, _) in enumerate(GENES)
        )
    gtf = root / "mini.gtf.gz"
    with gzip.open(gtf, "wt") as f:
        f.write("#!genome-build GRCh38\n")
        for gid, name, biotype in GENES:
            attrs = f'gene_id "{gid}"; gene_version "1"; '
            if name:
                attrs += f'gene_name "{name}"; '
            attrs += f'gene_source "ensembl"; gene_biotype "{biotype}";'
            f.write(f"1\tensembl\tgene\t1\t100\t.\t+\t.\t{attrs}\n")
    schema = root / "METADATA_SCHEMA.md"
    schema.write_text("# Unified sample metadata\n")
    return {"data_dir": data, "gtf": gtf, "schema_doc": schema, "out": root / "out.duckdb"}


@pytest.fixture
def inputs(tmp_path) -> dict:
    return write_inputs(tmp_path)


@pytest.fixture
def built(inputs) -> dict:
    """Inputs plus a database built from them."""
    build_db.build(
        inputs["data_dir"],
        inputs["out"],
        inputs["gtf"],
        inputs["schema_doc"],
        expected_gtf_sha256=None,  # the synthetic GTF is not the pinned Ensembl file
    )
    return inputs


@pytest.fixture
def anyio_backend():
    return "asyncio"
