"""The contrast space: which comparisons are valid, and the statistics for them.

`define_contrasts` runs at build time and enumerates every valid comparison into
the `contrasts` and `contrast_members` tables. The server only ever serves
comparisons from that list, so no tool call can pair samples across datasets or
cell lines (contrast_space_tutorial.md: a difference cancels nuisance only when
both groups share line, dataset and batch).

Pairing rules (all within one dataset and one effective_cell_line):
  acute                    drug x dose x timepoint vs untreated ('none') controls;
                           time-matched controls when they exist, else untimed ones
  chronic_resistance       derivative at 0 nM vs parental at 0 nM (matched dose)
  chronic_drug_dependence  derivative on drug vs the same derivative at 0 nM
  transgene                construct +dox vs the same construct -dox
  transgene_dox_corrected  (fusion +dox - fusion -dox) - (EV +dox - EV -dox)

"Resistant on drug vs parental at 0 nM" is deliberately absent: it mixes the
resistance effect with the acute drug effect.
"""

import math
import statistics
from collections import defaultdict

from scipy import stats

KINDS = (
    "acute",
    "chronic_resistance",
    "chronic_drug_dependence",
    "transgene",
    "transgene_dox_corrected",
)
CHRONIC_KINDS = ("chronic_resistance", "chronic_drug_dependence")
# Datasets whose 'none' controls may be untreated or vehicle (the SDRFs don't say).
VEHICLE_UNKNOWN = {"E11342", "E11834"}

CONTRAST_COLUMNS = [
    ("contrast_id", "VARCHAR PRIMARY KEY"),
    ("kind", "VARCHAR"),
    ("dataset", "VARCHAR"),
    ("cell_line", "VARCHAR"),  # effective_cell_line (molecular identity)
    ("compound", "VARCHAR"),
    ("dose_nM", "INTEGER"),
    ("timepoint_h", "INTEGER"),
    ("resistance_protocol", "VARCHAR"),
    ("derivation_replicate", "VARCHAR"),
    ("derivative_id", "VARCHAR"),
    ("construct", "VARCHAR"),
    ("test_desc", "VARCHAR"),
    ("ref_desc", "VARCHAR"),
    ("flags", "VARCHAR[]"),
]


def _fmt(value, unit: str) -> str:
    return "NA" if value is None else f"{value}{unit}"


def _contrast(kind, ds, line, members, flags, test_desc, ref_desc, **fields) -> dict:
    parts = [kind, ds, line]
    if kind == "acute":
        parts += [
            fields["compound"],
            _fmt(fields["dose_nM"], "nM"),
            _fmt(fields["timepoint_h"], "h"),
        ]
    elif kind in CHRONIC_KINDS:
        parts += [fields["resistance_protocol"], fields["derivation_replicate"]]
    else:
        parts += [fields["construct"]]
    row = {c: None for c, _ in CONTRAST_COLUMNS}
    row.update(fields)
    row.update(
        contrast_id=":".join(str(p) for p in parts),
        kind=kind,
        dataset=ds,
        cell_line=line,
        test_desc=test_desc,
        ref_desc=ref_desc,
        flags=sorted(set(flags)),
        members=members,  # {role: [sample_idx]}, stored in contrast_members
    )
    return row


def define_contrasts(samples: list[dict]) -> list[dict]:
    """Every valid comparison. `samples` are build rows with a sample_idx key."""
    out = []

    def idxs(rows):
        return [r["sample_idx"] for r in rows]

    def common_flags(rows):
        flags = []
        if any("dose_suspect" in (r["data_flags"] or []) for r in rows):
            flags.append("dose_suspect")
        if any(r["identity_status"] == "reassigned" for r in rows):
            flags.append("identity_reassigned")
        return flags

    # --- acute: drug arm vs untreated controls of the same line in the same dataset
    arms = defaultdict(list)
    for s in samples:
        if s["perturbation_class"] == "acute_drug":
            key = (
                s["dataset"],
                s["effective_cell_line"],
                s["compound"],
                s["dose_nM"],
                s["timepoint_h"],
            )
            arms[key].append(s)
    for (ds, line, compound, dose, tp), test in sorted(arms.items(), key=lambda kv: str(kv[0])):
        controls = [
            s
            for s in samples
            if s["dataset"] == ds
            and s["effective_cell_line"] == line
            and s["perturbation_class"] == "baseline"
            and s["compound"] == "none"
            and s["dose_nM"] in (0, None)
        ]
        flags = common_flags(test + controls)
        timed = [c for c in controls if tp is not None and c["timepoint_h"] == tp]
        if timed:
            ref = timed
        else:
            ref = [c for c in controls if c["timepoint_h"] is None]
            if tp is not None and ref:
                flags.append("untimed_control")
        if tp is None:
            flags.append("timepoint_unrecorded")
        if dose is None:
            flags.append("dose_unrecorded")
        if ds in VEHICLE_UNKNOWN:
            flags.append("vehicle_unknown")
        if not ref:
            flags.append("no_reference")
        out.append(
            _contrast(
                "acute", ds, line, {"test": idxs(test), "ref": idxs(ref)}, flags,
                f"{compound}, dose {_fmt(dose, ' nM')}, time {_fmt(tp, ' h')}",
                "untreated (compound none)" + (", time-matched" if timed else ", untimed"),
                compound=compound, dose_nM=dose, timepoint_h=tp,
            )
        )  # fmt: skip

    # --- chronic: per resistant derivative (keyed on molecular identity)
    derivs = defaultdict(list)
    for s in samples:
        if s["derivative_id"]:
            derivs[s["derivative_id"]].append(s)
    # A lab derivation (recorded line x protocol x BR) whose libraries somalier assigns
    # to more than one line is split into two derivatives; both halves are flagged,
    # because the culture itself was mixed.
    identities = defaultdict(set)
    for s in samples:
        if s["derivative_id"]:
            lab_key = (s["cell_line"], s["resistance_protocol"], s["derivation_replicate"])
            identities[lab_key].add(s["effective_cell_line"])
    for deriv_id, rows in sorted(derivs.items()):
        first = rows[0]
        ds, line = first["dataset"], first["effective_cell_line"]
        protocol, br = first["resistance_protocol"], first["derivation_replicate"]
        off = [r for r in rows if r["chronic_state"] == "resistant_off_drug"]
        on = [r for r in rows if r["chronic_state"] == "resistant_on_drug"]
        parental = [
            s
            for s in samples
            if s["dataset"] == ds
            and s["chronic_state"] == "parental"
            and s["effective_cell_line"] == line
        ]
        fields = {
            "resistance_protocol": protocol, "derivation_replicate": br, "derivative_id": deriv_id,
        }  # fmt: skip
        # compound/dose describe the TEST arm: none for resistance (both arms at 0 nM),
        # the maintenance drug and dose for drug dependence.
        on_drug = {"compound": on[0]["compound"], "dose_nM": on[0]["dose_nM"]} if on else {}
        lab_key = (first["cell_line"], protocol, br)
        # Name the other line(s), so Claude can tell the user e.g. "the rest is DFCI032".
        others = sorted(identities[lab_key] - {line})
        split = [f"derivation_split:{'+'.join(others)}"] if others else []
        flags = common_flags(off + parental) + split + ([] if parental else ["no_reference"])
        out.append(
            _contrast(
                "chronic_resistance", ds, line, {"test": idxs(off), "ref": idxs(parental)},
                flags, f"{deriv_id} resistant, off drug (0 nM)", "parental (0 nM)",
                compound="none", dose_nM=0, **fields,
            )
        )  # fmt: skip
        flags = common_flags(on + off) + split + ([] if off else ["no_reference"])
        out.append(
            _contrast(
                "chronic_drug_dependence", ds, line, {"test": idxs(on), "ref": idxs(off)},
                flags, f"{deriv_id} on drug ({_fmt(on_drug.get('dose_nM'), ' nM')})",
                f"{deriv_id} off drug (0 nM)", **on_drug, **fields,
            )
        )  # fmt: skip

    # --- transgene: +dox vs -dox within construct; dox-corrected against EV
    by_construct = defaultdict(lambda: {"dox": [], "nodox": []})
    for s in samples:
        if s["construct"]:
            arm = "dox" if s["compound"] == "doxycycline" else "nodox"
            by_construct[(s["dataset"], s["effective_cell_line"], s["construct"])][arm].append(s)
    for (ds, line, construct), arms_ in sorted(by_construct.items()):
        flags = ["ev_is_dox_null"] if construct == "EV" else []
        out.append(
            _contrast(
                "transgene", ds, line, {"test": idxs(arms_["dox"]), "ref": idxs(arms_["nodox"])},
                flags, f"{construct} +dox", f"{construct} -dox", construct=construct,
            )
        )  # fmt: skip
        ev = by_construct.get((ds, line, "EV"))
        if construct != "EV" and ev:
            members = {
                "test": idxs(arms_["dox"]),
                "ref": idxs(arms_["nodox"]),
                "null_test": idxs(ev["dox"]),
                "null_ref": idxs(ev["nodox"]),
            }
            out.append(
                _contrast(
                    "transgene_dox_corrected", ds, line, members, ["did_se_inflated"],
                    f"{construct} (+dox - -dox)", "EV (+dox - -dox)", construct=construct,
                )
            )  # fmt: skip
    return out


# ---------------------------------------------------------------- statistics


def log2p1(x: float) -> float:
    """log2(TPM + 1): the scale effects are measured on (additive nuisance cancels)."""
    return math.log2(x + 1.0)


def summarise(values: list[float]) -> dict:
    n = len(values)
    return {
        "n": n,
        "mean": statistics.fmean(values) if n else None,
        "sd": statistics.stdev(values) if n > 1 else None,
    }


def welch_difference(groups: list[tuple[int, dict]]) -> dict:
    """Linear combination sum(sign * mean) of independent groups, with a Welch
    standard error and a Satterthwaite-df 95% CI. groups: [(+1/-1, summary)].
    Two groups give the usual Welch difference; four give a difference-in-differences."""
    if any(g["n"] == 0 for _, g in groups):
        return {"estimate": None, "se": None, "df": None, "ci_low": None, "ci_high": None}
    estimate = sum(sign * g["mean"] for sign, g in groups)
    if any(g["n"] < 2 for _, g in groups):
        return {"estimate": estimate, "se": None, "df": None, "ci_low": None, "ci_high": None}
    terms = [g["sd"] ** 2 / g["n"] for _, g in groups]
    se = math.sqrt(sum(terms))
    if se == 0:
        return {
            "estimate": estimate,
            "se": 0.0,
            "df": None,
            "ci_low": estimate,
            "ci_high": estimate,
        }
    df = sum(terms) ** 2 / sum(t**2 / (g["n"] - 1) for t, (_, g) in zip(terms, groups, strict=True))
    half = float(stats.t.ppf(0.975, df)) * se  # plain float, not numpy
    return {
        "estimate": estimate,
        "se": se,
        "df": df,
        "ci_low": estimate - half,
        "ci_high": estimate + half,
    }


def across_replicates(estimates: list[float], shared_ref: dict | None = None) -> dict:
    """Mean of per-derivation effects with a 95% CI: the evolutionary replicate
    (BR) is the unit, not the library.

    When every BR is compared with the same reference group (chronic_resistance:
    one parental for all BRs), error in that reference mean shifts all BR effects
    together and never shows in their spread. Its variance is then added back:
        se^2 = sd_BR^2 / k + sd_ref^2 / n_ref
    with Satterthwaite df. Without a shared reference, df = k - 1."""
    k = len(estimates)
    out = {"estimate": None, "sd": None, "se": None, "ci_low": None, "ci_high": None, "n": k}
    if k == 0:
        return out
    out["estimate"] = mean = statistics.fmean(estimates)
    if k < 2:
        return out
    out["sd"] = sd = statistics.stdev(estimates)
    between = sd**2 / k
    if shared_ref is None:
        se, df = math.sqrt(between), k - 1
    else:
        if shared_ref["n"] < 2 or shared_ref["sd"] is None:
            return out  # reference variance unknown: no honest interval
        ref = shared_ref["sd"] ** 2 / shared_ref["n"]
        se = math.sqrt(between + ref)
        denom = between**2 / (k - 1) + ref**2 / (shared_ref["n"] - 1)
        df = (between + ref) ** 2 / denom if denom > 0 else k - 1
    out["se"] = se
    half = float(stats.t.ppf(0.975, df)) * se
    out["ci_low"], out["ci_high"] = mean - half, mean + half
    return out
