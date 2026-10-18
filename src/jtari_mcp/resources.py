"""Text served as MCP resources. Clients show resources for the user to attach;
Claude rarely reads them on its own, so every rule here is also in the server
instructions or a tool description."""

from jtari_mcp.db import Database

DERIVED_COLUMNS = """
## Columns added by this server's build (addendum to METADATA_SCHEMA.md)

| Column | Meaning |
|---|---|
| `effective_cell_line` | Molecular identity: `identity_call` for `reassigned` samples, otherwise `cell_line`. `cell_line` itself is never changed. |
| `identity_mismatch` | True when `identity_call` is set and differs from `cell_line`. |
| `construct` | E11304 transgene construct (EV, KIF5B-ALK, TFG-ALK, EML4-ALK_V1, EML4-ALK_V3), from `notes`. |
| `chronic_state` | LOCAL resistance series: `parental` (0 nM), `resistant_off_drug` (0 nM), `resistant_on_drug` (1300/1500 nM). |
| `derivative_id` | Resistant derivative `effective_cell_line:protocol:BR`, e.g. `H3122:Cmax:BR2`. |
| `data_flags` | Known data issues, e.g. `dose_suspect` (300000 nM recorded; probably 300 nM). |
"""

INTERPRETATION = """# How to interpret results from this server

**TPM** (transcripts per million) is a within-sample proportion. It answers "is this
gene expressed, and how strongly relative to other genes in this sample".
It is NOT comparable across datasets: library preparation, read length and
composition differ between cohorts. Rank or average TPM only within one dataset.

**Counts** are RSEM expected counts: raw, depth-dependent and fractional. A single
count value means little on its own; counts are inputs to normalising models
(edgeR, DESeq2, limma-voom).

**Across datasets, compare directions, not sizes.** Preparation differences scale
log fold changes as well as shifting them, so the same biology can give different
magnitudes in different cohorts.

**Replicates and n.** `derivation_replicate` (BR1-3) is an independent resistance
derivation, the unit of evolutionary replication. `replicate` is a library
replicate and is unreliable for pairing in LOCAL; `batch` is reliable there.
Always report n.

**Identity.** `suspect` samples (unresolved genotype) are excluded by default.
`reassigned` samples are genetically another line (DFCI032 in place of H2228);
`effective_cell_line` holds the molecular identity. `unverified` means never
tested, not doubtful: only LOCAL was fingerprinted.

**Controls.** E11342 controls are time-matched. E11834 controls have no timepoint
(one group serves 6 h and 24 h). LOCAL acute arms and controls have no timepoint.
In E11342/E11834, `none` may mean untreated or vehicle (the SDRF does not say).

**Doxycycline** itself changes expression: in E11304 the empty-vector (EV) +/- dox
difference is the null for every fusion effect.

**Resistance at matched dose.** In the chronic series the 0 nM derivative samples
are resistant cells off drug, not parental. Resistance = resistant (0 nM) vs
parental (0 nM); on drug vs off drug within a derivative = residual drug dependence.

**Contrasts** report log2 fold change of mean log2(TPM+1), test minus reference,
with a Welch 95% CI. They are descriptive: no p-values. Kinds: acute (drug vs
untreated), chronic_resistance (resistant 0 nM vs parental 0 nM),
chronic_drug_dependence (on drug vs off drug, same derivative), transgene (+dox vs
-dox), transgene_dox_corrected (fusion dox effect minus EV dox effect). At
level='protocol', chronic effects are averaged over derivation replicates (BR), the
independent unit; BRs share one parental reference, so they are not fully independent.

**Cross-dataset ranking** of raw TPM is acceptable only when each dataset's highest
group mean is 10-fold or more above the next dataset's highest group mean, with a
stated design reason (e.g. transgene overexpression) and for a gene whose measurement
does not depend on library preparation; otherwise show values per dataset without
ranking.

**Batch in LOCAL** is balanced 2+2 within each H2228/H3122 group, but all
parental-only lines are batch 1 only.
"""


def schema_doc(db: Database) -> str:
    base = db.docs.get("METADATA_SCHEMA.md", "(METADATA_SCHEMA.md was not included in this build.)")
    return base + "\n" + DERIVED_COLUMNS


def inventory(db: Database) -> str:
    lines = [
        "# Dataset inventory",
        "",
        "| dataset | accession | public | samples | cell lines | design | caveats |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in db.datasets:
        lines.append(
            f"| {d['dataset']} | {d['accession'] or '-'} | {'yes' if d['public'] else 'no'} | "
            f"{d['n_samples']} | {d['n_cell_lines']} | {d['design']} | {d['caveats']} |"
        )
    return "\n".join(lines) + "\n"


def provenance(db: Database) -> str:
    lines = ["# Build provenance", ""]
    lines += [f"- **{k}**: {v}" for k, v in db.provenance_build.items()]
    lines += ["", "| role | dataset | file | sha256 |", "|---|---|---|---|"]
    lines += [
        f"| {p['role']} | {p['dataset'] or '-'} | {p['rel_path']} | `{p['sha256'][:16]}…` |"
        for p in db.provenance_inputs
    ]
    return "\n".join(lines) + "\n"
