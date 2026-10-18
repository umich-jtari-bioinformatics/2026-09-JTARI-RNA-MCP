"""Eval cases: questions a lab member might ask, and what a correct answer must do.

Expected numbers are computed HERE from the raw files (the RSEM TPM matrices and
sample_metadata.tsv, read with the csv module, samples chosen by name or label),
never through the server's code, so a pass means "Claude's answer agrees with the
source data", not "the server agrees with itself".

Each case can carry these checks (all present must pass):
    expect_tools   tools Claude must call at least once
    expect_args    {tool: {arg: value}}: some call to that tool used these args
                   (for dict args, the given keys must match)
    numbers        values that must appear in the answer, within a relative tolerance
    contains       groups of alternatives; each group needs one match (case-insensitive)
    judge          one behaviour rule that a second Claude call grades PASS/FAIL
"""

import csv
import math
import os
import statistics
from functools import cache
from pathlib import Path

# The raw data folder (sample_metadata.tsv + the five result folders); not in the repo.
DATA_DIR = Path(os.environ.get("JTARI_DATA_DIR", "data/raw"))
DIRS = {
    "LOCAL": "soellner_13807-nm",
    "E11304": "e-mtab-11304_results",
    "E11342": "e-mtab-11342_results",
    "E11834": "e-mtab-11834_results",
    "FL3C": "fl3c",
}
GENE = {
    "ALK": "ENSG00000171094",
    "DUSP6": "ENSG00000139318",
    "SPRY4": "ENSG00000187678",
    "EGFR": "ENSG00000146648",
}


@cache
def tpm(dataset: str, symbol: str) -> dict[str, float]:
    """matrix_sample_id -> TPM for one gene, straight from the RSEM file."""
    with open(DATA_DIR / DIRS[dataset] / "rsem.merged.gene_tpm.tsv") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader)
        for row in reader:
            if row[0] == GENE[symbol]:
                return dict(zip(header[2:], map(float, row[2:]), strict=True))
    raise KeyError(symbol)


@cache
def metadata() -> list[dict]:
    with open(DATA_DIR / "sample_metadata.tsv", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def log_mean(values) -> float:
    return statistics.fmean(math.log2(v + 1) for v in values)


def by_name(dataset, symbol, prefix):
    return [v for k, v in tpm(dataset, symbol).items() if k.startswith(prefix)]


def by_label(symbol, prefix, exclude_suspect=True):
    rows = [
        m
        for m in metadata()
        if m["dataset"] == "LOCAL"
        and m["label"].startswith(prefix)
        and not (exclude_suspect and m["identity_status"] == "suspect")
    ]
    values = tpm("LOCAL", symbol)
    return [values[m["matrix_sample_id"]] for m in rows]


def lfc(test, ref) -> float:
    return log_mean(test) - log_mean(ref)


def protocol_mean(symbol, line, protocol) -> float:
    parental = by_label(symbol, f"{line}-LOCAL-baseline_0nM-")
    effects = [
        lfc(by_label(symbol, f"{line}-LOCAL-{protocol}_0nM_{br}-"), parental)
        for br in ("BR1", "BR2", "BR3")
        if by_label(symbol, f"{line}-LOCAL-{protocol}_0nM_{br}-")
    ]
    return statistics.fmean(effects)


def build_cases() -> list[dict]:
    e11834_cuto8_6h = lfc(
        by_name("E11834", "DUSP6", "CUTO8-lorla-6hr-"), by_name("E11834", "DUSP6", "CUTO8-control-")
    )
    v3 = lfc(
        by_name("E11304", "DUSP6", "NL20-V3-dox-"), by_name("E11304", "DUSP6", "NL20-V3-nodox-")
    )
    ev = lfc(
        by_name("E11304", "DUSP6", "NL20-EV-dox-"), by_name("E11304", "DUSP6", "NL20-EV-nodox-")
    )
    h3122_tram = {
        tp: lfc(
            by_name("E11834", "DUSP6", f"H3122-trame-{tp}hr-"),
            by_name("E11834", "DUSP6", "H3122-control-"),
        )
        for tp in (6, 24)
    }
    kras_g12c = sorted(
        {
            m["cell_line"]
            for m in metadata()
            if m["dataset"] == "FL3C" and "KRAS=p.G12C" in m["notes"]
        }
    )
    cuto9_ctrl = statistics.fmean(by_name("E11834", "ALK", "CUTO9-control-"))

    return [
        {
            "id": "alk-e11834-ordered",
            "question": "Show me ALK expression across all cell-line samples in E11834, ordered by "
            "treatment and timepoint, and tell me which cell line expresses ALK most there.",
            "expect_tools": ["get_expression"],
            "numbers": [{"label": "CUTO9 control mean TPM", "value": cuto9_ctrl, "rel": 0.02}],
            "contains": [["CUTO9"]],
            "judge": "The answer keeps all numbers within E11834 (no other dataset's values are "
            "ranked against them) and reports how many samples each group has.",
        },
        {
            "id": "flagship",
            "question": "What is the log2 fold change of DUSP6 under acute lorlatinib in each CUTO line "
            "across both acute cohorts, next to Cmax-resistant vs parental in H3122 and H2228?",
            "expect_tools": ["contrast"],
            "numbers": [
                {"label": "E11834 CUTO8 6h log2FC", "value": e11834_cuto8_6h, "rel": 0.02},
                {
                    "label": "H3122 Cmax mean over BRs",
                    "value": protocol_mean("DUSP6", "H3122", "Cmax"),
                    "rel": 0.02,
                },
            ],
            "judge": "Across datasets the answer compares only the direction of effects, not their "
            "size, and it describes the resistance comparison as resistant cells at 0 nM (off drug) "
            "versus parental, not as a drug effect.",
        },
        {
            "id": "dataset-ranking",
            "question": "Which dataset has the highest ALK expression?",
            "expect_tools": ["get_expression"],
            "contains": [["E11304", "NL20"]],
            "judge": "The server's rule (the answer may quote it): ranking raw TPM across datasets "
            "is allowed only when one dataset's highest group mean is 10-fold or more above the next "
            "dataset's highest group mean, a design reason is stated, and the gene's measurement does "
            "not depend on library preparation; otherwise datasets are shown side by side. PASS if "
            "the answer warns that TPM is not directly comparable across datasets, explains that "
            "E11304's high ALK comes from transgene overexpression, ranks only where that rule is "
            "met, and never averages TPM across datasets. Values within one dataset labelled as "
            "pooled over conditions are acceptable; a cautious word like 'likely' about a design "
            "reason is acceptable.",
        },
        {
            "id": "resistance-off-drug",
            "question": "In H3122, does Cmax resistance change SPRY4 compared with the parental line?",
            "expect_tools": ["contrast"],
            "numbers": [
                {
                    "label": "H3122 Cmax SPRY4 mean over BRs",
                    "value": protocol_mean("SPRY4", "H3122", "Cmax"),
                    "rel": 0.03,
                }
            ],
            "judge": "The answer states that the resistant samples compared were off drug (0 nM) "
            "against parental at 0 nM, and reports results per derivation (BR) or across the "
            "independent derivations, not as a single pooled group of libraries.",
        },
        {
            "id": "identity-split",
            "question": "How does DUSP6 change in H2228 StartIC50 resistant cells compared with parental H2228?",
            "expect_tools": ["contrast"],
            "contains": [["DFCI032", "DFCI-032"]],
            "judge": "The answer tells the user that some libraries recorded as H2228 StartIC50 are "
            "genetically DFCI032 and are not counted in the H2228 results. (Facts for grading: in "
            "BR2 and BR3 some libraries were reassigned to DFCI032; the H2228 libraries left in BR2 "
            "have suspect identity and are excluded by default, so attributing BR2's exclusion to "
            "suspect identity is correct.)",
        },
        {
            "id": "dox-corrected",
            "question": "What does EML4-ALK V3 do to DUSP6 in the NL20 cells, once you correct for "
            "the effect of doxycycline itself?",
            "expect_tools": ["contrast"],
            "numbers": [{"label": "V3 dox-corrected log2FC", "value": v3 - ev, "rel": 0.03}],
            "judge": "The answer explains that the empty-vector +/- dox difference was subtracted "
            "(difference in differences) to remove the doxycycline effect.",
        },
        {
            "id": "ambiguous-gene",
            "question": "What is the expression of Y_RNA in H3122 in the LOCAL dataset?",
            "expect_tools": ["resolve_genes"],
            "judge": "The answer makes clear that Y_RNA names many different Ensembl genes and either "
            "asks which one is meant or explicitly reports which gene(s) it used; it does not present "
            "one gene's values as 'Y_RNA' without saying so.",
        },
        {
            "id": "invalid-comparison",
            "question": "Give me the log2 fold change of DUSP6 in H3122 Cmax cells on alectinib "
            "compared with parental H3122.",
            "judge": "The answer explains that 'resistant on drug vs parental' mixes the resistance "
            "effect with the acute drug effect, and offers or reports the valid comparisons "
            "(resistant at 0 nM vs parental, and/or on drug vs off drug) instead of computing the "
            "invalid one itself from raw expression values.",
        },
        {
            "id": "untimed-control",
            "question": "In E11834, what is the effect of trametinib on DUSP6 in H3122 at 6 h and at 24 h?",
            "expect_tools": ["contrast"],
            "numbers": [
                {"label": "6h log2FC", "value": h3122_tram[6], "rel": 0.02},
                {"label": "24h log2FC", "value": h3122_tram[24], "rel": 0.02},
            ],
            "judge": "The answer mentions that the E11834 controls have no timepoint, so the same "
            "controls serve as the reference for both 6 h and 24 h.",
        },
        {
            "id": "fl3c-kras",
            "question": "Which FL3C cell lines carry KRAS G12C, and what is their EGFR expression?",
            "expect_tools": ["get_expression"],
            "contains": [[kras_g12c[0]], [kras_g12c[-1]]],
            "judge": "The answer lists the KRAS G12C lines from the FL3C metadata and gives EGFR values "
            "for them from FL3C only.",
        },
        {
            "id": "missing-arm",
            "question": "What does alectinib do to DUSP6 in the E11834 dataset?",
            "judge": "The answer says E11834 has no alectinib treatment (it has lorlatinib and "
            "trametinib) and does not present results from another dataset as if they were E11834; "
            "pointing the user to where alectinib data exists is fine.",
        },
    ]
