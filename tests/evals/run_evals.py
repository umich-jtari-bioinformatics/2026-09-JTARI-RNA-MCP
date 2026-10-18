"""Does Claude answer JTARI questions correctly through the MCP server?

The tool tests prove the server returns the right DATA. This proves the next layer:
given that data, Claude picks the right tools, reports the numbers correctly and
follows the rules in the server instructions. Adapted from the DSS3 eval runner.

How a case runs:
    1. Claude Code runs headless (`claude -p`) in an EMPTY temp folder with ONLY this
       MCP server attached and a neutral system prompt, so every rule it follows must
       come from the server's instructions and tool descriptions.
    2. Every tool call (name + arguments) and the final answer are recorded.
    3. Automatic checks: tools called, arguments, expected numbers present (computed
       from the raw files in cases.py), expected names present.
    4. Judge: a second Claude call grades one behaviour rule PASS/FAIL with a reason.
    A run passes only if every check passes. --runs N asks each question N times.

Usage (from the repo root, jtari-mcp env):
    uv run python tests/evals/run_evals.py                 # all cases, 1 run each
    uv run python tests/evals/run_evals.py --runs 2
    uv run python tests/evals/run_evals.py --cases flagship,dox-corrected
    uv run python tests/evals/run_evals.py --list

Reports (full answers, which contain LOCAL data) go to results/evals/ (gitignored).
One summary line per run is appended to docs/EVAL_LOG.md (pass rates only).
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import build_cases

RESULTS = ROOT / "results" / "evals"
LOG = ROOT / "docs" / "EVAL_LOG.md"
TOOL_PREFIX = "mcp__jtari__"
UV = shutil.which("uv") or str(Path.home() / "miniforge3/envs/jtari-mcp/bin/uv")

# Neutral on purpose: no hints about JTARI rules. Those must come from the server.
ANSWER_SYSTEM = (
    "You are a research assistant for a cancer biology lab. Answer the user's question "
    "using the tools available to you."
)
BLOCKED_BUILTINS = "Bash Read Write Edit Glob Grep WebFetch WebSearch NotebookEdit Task Agent"

JUDGE_SYSTEM = """You grade an AI assistant's answer against ONE rule.
You get the question, the rule, what the data tools returned, and the answer.
Judge only the rule, strictly but fairly: wording can differ, meaning must match.
Reply with ONLY a JSON object, no other text:
{"verdict": "PASS" or "FAIL", "reason": "<one sentence>"}"""


def mcp_config(workdir: Path) -> None:
    cfg = {
        "mcpServers": {
            "jtari": {
                "type": "stdio",
                "command": UV,
                "args": ["run", "--directory", str(ROOT), "python", "-m", "jtari_mcp.server"],
            }
        }
    }
    (workdir / "mcp.json").write_text(json.dumps(cfg))


def ask_claude(question: str, workdir: Path, model: str | None, timeout: int = 400) -> dict:
    cmd = [
        "claude", "-p", question, "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(workdir / "mcp.json"), "--strict-mcp-config",
        "--allowedTools", f"{TOOL_PREFIX}*", "--disallowedTools", BLOCKED_BUILTINS,
        "--system-prompt", ANSWER_SYSTEM, "--max-turns", "15", "--no-session-persistence",
    ]  # fmt: skip
    if model:
        cmd += ["--model", model]
    out = {"answer": "", "tool_calls": [], "tool_results": [], "cost_usd": 0.0, "error": None}
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        out["error"] = f"timed out after {timeout}s"
        return out
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for c in ev["message"].get("content", []):
                if c.get("type") == "tool_use" and c["name"].startswith(TOOL_PREFIX):
                    out["tool_calls"].append(
                        {"tool": c["name"].removeprefix(TOOL_PREFIX), "args": c.get("input", {})}
                    )
        elif ev.get("type") == "user":
            for c in ev["message"].get("content", []):
                if c.get("type") == "tool_result":
                    b = c.get("content")
                    text = " ".join(x.get("text", "") for x in b) if isinstance(b, list) else str(b)
                    out["tool_results"].append(text)
        elif ev.get("type") == "result":
            out["answer"] = ev.get("result", "") or ""
            out["cost_usd"] = ev.get("total_cost_usd", 0.0) or 0.0
            if ev.get("is_error"):
                out["error"] = ev.get("subtype", "error")
    if not out["answer"] and not out["error"]:
        out["error"] = (proc.stderr or "no answer")[-300:]
    return out


def judge(case: dict, run: dict, model: str, workdir: Path) -> dict:
    tool_out = "\n---\n".join(run["tool_results"])[:12000] or "(no tool output)"
    prompt = (
        f"QUESTION:\n{case['question']}\n\nRULE:\n{case['judge']}\n\n"
        f"WHAT THE DATA TOOLS RETURNED:\n{tool_out}\n\nANSWER TO GRADE:\n{run['answer']}"
    )
    cmd = [
        "claude", "-p", prompt, "--output-format", "json", "--system-prompt", JUDGE_SYSTEM,
        "--model", model, "--strict-mcp-config", "--disallowedTools", BLOCKED_BUILTINS,
        "--max-turns", "1", "--no-session-persistence",
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
        )
        res = json.loads(proc.stdout)
        text = res.get("result", "")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        verdict = (
            json.loads(m.group(0))
            if m
            else {"verdict": "FAIL", "reason": f"unparseable judge reply: {text[:120]}"}
        )
        verdict["cost_usd"] = res.get("total_cost_usd", 0.0) or 0.0
        return verdict
    except Exception as e:  # noqa: BLE001 (any judge failure is a FAIL with the reason)
        return {"verdict": "FAIL", "reason": f"judge error: {e}", "cost_usd": 0.0}


_NUM = re.compile(r"(?<![\w.])[-−–]?\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    vals = []
    for s in _NUM.findall(text):
        try:
            vals.append(float(s.replace("−", "-").replace("–", "-").replace(",", "")))
        except ValueError:
            pass
    return vals


def number_present(text: str, spec: dict) -> bool:
    target, rel = float(spec["value"]), spec.get("rel", 0.01)
    return any(abs(v - target) <= rel * abs(target) for v in numbers_in(text))


def args_match(actual, want) -> bool:
    if isinstance(want, dict):
        return isinstance(actual, dict) and all(
            args_match(actual.get(k), v) for k, v in want.items()
        )
    return actual == want


def auto_checks(case: dict, run: dict) -> list[dict]:
    ans, calls, checks = run["answer"], run["tool_calls"], []
    called = {c["tool"] for c in calls}
    for tool in case.get("expect_tools", []):
        checks.append({"check": f"called {tool}", "passed": tool in called})
    for tool, want in case.get("expect_args", {}).items():
        hit = any(c["tool"] == tool and args_match(c["args"], want) for c in calls)
        checks.append({"check": f"called {tool} with {want}", "passed": hit})
    for spec in case.get("numbers", []):
        checks.append(
            {
                "check": f"answer contains {spec['label']} ≈ {spec['value']:.4g}",
                "passed": number_present(ans, spec),
            }
        )
    for alts in case.get("contains", []):
        hit = any(a.lower() in ans.lower() for a in alts)
        checks.append({"check": f"answer mentions {alts[0]}", "passed": hit})
    return checks


def run_one(case: dict, run_no: int, args) -> dict:
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="jtari-eval-") as tmp:
        workdir = Path(tmp)
        mcp_config(workdir)
        run = ask_claude(case["question"], workdir, args.model)
        checks = [
            {"check": "got an answer", "passed": not run["error"], "detail": run["error"] or ""}
        ]
        if not run["error"]:
            checks += auto_checks(case, run)
            if case.get("judge"):
                v = judge(case, run, args.judge_model, workdir)
                run["cost_usd"] += v.get("cost_usd", 0.0)
                checks.append(
                    {
                        "check": "judge: " + case["judge"][:80] + "…",
                        "passed": v.get("verdict") == "PASS",
                        "detail": v.get("reason", ""),
                    }
                )
    passed = all(c["passed"] for c in checks)
    failed = "; ".join(c["check"] for c in checks if not c["passed"])
    print(
        f"{'PASS' if passed else 'FAIL'}  {case['id']} (run {run_no})  {time.time() - t0:.0f}s"
        + ("" if passed else f"  — {failed}"),
        flush=True,
    )
    return {
        "case": case["id"],
        "run": run_no,
        "passed": passed,
        "checks": checks,
        "answer": run["answer"],
        "tool_calls": run["tool_calls"],
        "cost_usd": round(run["cost_usd"], 4),
        "seconds": round(time.time() - t0, 1),
    }


def write_markdown(path: Path, cases: list[dict], runs: list[dict], summary: dict) -> None:
    lines = [f"# Eval run {summary['when']}", "", f"Pass rate: **{summary['pass_rate']}**  ", ""]
    lines += [f"Cost: ${summary['cost_usd']:.2f}; model: {summary['model']}", ""]
    for case in cases:
        lines += [f"## {case['id']}", "", f"> {case['question']}", ""]
        for r in [r for r in runs if r["case"] == case["id"]]:
            lines.append(f"### run {r['run']}: {'PASS' if r['passed'] else 'FAIL'}")
            lines += [
                f"- {'✓' if c['passed'] else '✗'} {c['check']} {c.get('detail', '')}"
                for c in r["checks"]
            ]
            calls = ", ".join(
                f"{c['tool']}({json.dumps(c['args'])[:120]})" for c in r["tool_calls"]
            )
            lines += [
                "",
                f"Tools: {calls}",
                "",
                "<details><summary>answer</summary>",
                "",
                r["answer"],
                "",
                "</details>",
                "",
            ]
    path.write_text("\n".join(lines))


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--cases", help="comma-separated case ids")
    p.add_argument("--model", default=None, help="answering model (default: Claude Code's)")
    p.add_argument("--judge-model", default="sonnet")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--list", action="store_true")
    args = p.parse_args()

    cases = build_cases()
    if args.list:
        for c in cases:
            print(f"{c['id']:22s} {c['question']}")
        return 0
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    jobs = [(c, n) for c in cases for n in range(1, args.runs + 1)]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        runs = list(pool.map(lambda j: run_one(j[0], j[1], args), jobs))

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    n_pass = sum(r["passed"] for r in runs)
    summary = {
        "when": stamp,
        "commit": git_commit(),
        "runs": args.runs,
        "model": args.model or "claude default",
        "pass_rate": f"{n_pass}/{len(runs)}",
        "cost_usd": round(sum(r["cost_usd"] for r in runs), 2),
        "per_case": {
            c["id"]: f"{sum(r['passed'] for r in runs if r['case'] == c['id'])}/{args.runs}"
            for c in cases
        },
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"evals_{stamp}.json").write_text(
        json.dumps({"summary": summary, "runs": runs}, indent=1)
    )
    write_markdown(RESULTS / f"evals_{stamp}.md", cases, runs, summary)
    if not LOG.exists():
        LOG.write_text(
            "# Eval log\n\nOne line per run of tests/evals/run_evals.py (pass rates only).\n\n"
        )
    with open(LOG, "a") as f:
        per_case = ", ".join(f"{k} {v}" for k, v in summary["per_case"].items())
        f.write(
            f"- {stamp} @ {summary['commit']}: **{summary['pass_rate']}** "
            f"(runs={args.runs}, ${summary['cost_usd']:.2f}) — {per_case}\n"
        )
    print(
        f"\n{summary['pass_rate']} passed, ${summary['cost_usd']:.2f}. Report: {RESULTS}/evals_{stamp}.md"
    )
    return 0 if n_pass == len(runs) else 1


if __name__ == "__main__":
    sys.exit(main())
