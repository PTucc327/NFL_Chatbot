"""
Answer-quality evals: run real fan questions through the real pipeline
(Gemini + live data) and check each answer against the data it was given.

    python -m evals.run                    # all cases
    python -m evals.run --only tnf,trade   # some cases
    python -m evals.run --list             # show cases

Run it before and after changing prompts or models and compare the reports
in evals/results/. Uses ~2 Gemini requests per case (free-tier quota), so it
is not part of CI.
"""
import argparse
import datetime
import json
import logging
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.WARNING)
# Streamlit warns about running outside `streamlit run`; the pipeline
# functions used here don't need a session.
logging.getLogger("streamlit").setLevel(logging.ERROR)

from src import chatbot  # noqa: E402
from evals.cases import CASES  # noqa: E402
from evals.checks import GLOBAL_CHECKS  # noqa: E402


class _ModelLog(logging.Handler):
    """Records which Gemini models served each request (from httpx logs)."""
    def __init__(self):
        super().__init__()
        self.calls = []

    def emit(self, record):
        m = re.search(r"models/([\w.\-]+):(\w+).*HTTP/1.1 (\d+)", record.getMessage())
        if m:
            self.calls.append(f"{m.group(1)}:{'stream' if 'stream' in m.group(2).lower() else 'call'}"
                              f"({m.group(3)})")


def _data_text(results: dict) -> str:
    parts = []
    for value in results.values():
        if isinstance(value, dict):
            value = value.get("_text") or json.dumps(value)
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def _match(expected, actual) -> bool:
    if isinstance(expected, set):
        return expected <= set(actual or [])
    if isinstance(expected, str):
        return bool(actual) and (expected.lower() in str(actual).lower()
                                 or str(actual).lower() in expected.lower())
    return expected == actual


def run_case(case, model_log: _ModelLog) -> dict:
    model_log.calls.clear()
    t0 = time.time()
    context = {"last_player": None, "last_team": None, "conv_state": {}, **(case.context or {})}
    parsed = chatbot._extract_intent(case.question, context)
    t_understand = time.time() - t0

    results, _ = chatbot._dispatch(parsed)
    if any(isinstance(v, dict) and v.get("type") == "selection_required" for v in results.values()):
        answer = "(asked the user to choose between players)"
    else:
        answer = "".join(chatbot.stream_response(case.question, results, [], {}))
    elapsed = time.time() - t0
    data = _data_text(results)

    understanding = {k: (v, parsed.get(k)) for k, v in case.expect.items()}
    understanding_ok = {k: _match(v, a) for k, (v, a) in understanding.items()}

    checks = []
    for check in (*GLOBAL_CHECKS, *case.checks):
        try:
            ok, msg = check(answer, data, parsed)
        except Exception as e:  # a broken check is a failure, not a crash
            ok, msg = False, f"check error: {e!r}"
        checks.append({"check": getattr(check, "__name__", "check"), "ok": ok, "msg": msg})

    passed = all(understanding_ok.values()) and all(c["ok"] for c in checks)
    return {
        "id": case.id, "category": case.category, "question": case.question,
        "passed": passed,
        "understanding": {k: {"expected": sorted(v) if isinstance(v, set) else v,
                              "got": a, "ok": understanding_ok[k]}
                          for k, (v, a) in understanding.items()},
        "checks": checks, "answer": answer, "models": list(model_log.calls),
        "seconds": round(elapsed, 1), "understand_seconds": round(t_understand, 1),
    }


def write_report(rows: list, out_dir: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stamp}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

    passed = sum(r["passed"] for r in rows)
    lines = [f"# Eval report — {stamp}", "",
             f"**{passed}/{len(rows)} passed** · median {sorted(r['seconds'] for r in rows)[len(rows) // 2]}s per answer",
             "", "| Case | Category | Result | Time | Models | Problems |", "|---|---|---|---|---|---|"]
    for r in rows:
        problems = [f"understood {k}={v['got']!r} (expected {v['expected']!r})"
                    for k, v in r["understanding"].items() if not v["ok"]]
        problems += [f"{c['check']}: {c['msg']}" for c in r["checks"] if not c["ok"]]
        lines.append(f"| `{r['id']}` | {r['category']} | {'✅' if r['passed'] else '❌'} | "
                     f"{r['seconds']}s | {', '.join(r['models']) or '-'} | "
                     f"{'<br>'.join(problems) or ''} |")
    lines += ["", "## Answers", ""]
    for r in rows:
        lines += [f"### `{r['id']}` — {r['question']}", "", r["answer"].strip(), ""]
    path = out_dir / f"{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--list", action="store_true", help="list cases and exit")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between cases (rate limits)")
    args = ap.parse_args()

    cases = CASES
    if args.list:
        for c in cases:
            print(f"{c.id:18} {c.category:12} {c.question}")
        return
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in CASES if c.id in wanted]

    model_log = _ModelLog()
    logging.getLogger("httpx").addHandler(model_log)
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpx").propagate = False

    rows = []
    for i, case in enumerate(cases, 1):
        row = run_case(case, model_log)
        rows.append(row)
        status = "PASS" if row["passed"] else "FAIL"
        print(f"[{i:2}/{len(cases)}] {status}  {case.id:18} {row['seconds']:5.1f}s")
        for k, v in row["understanding"].items():
            if not v["ok"]:
                print(f"         understood {k}={v['got']!r}, expected {v['expected']!r}")
        for c in row["checks"]:
            if not c["ok"]:
                print(f"         {c['check']}: {c['msg']}")
        time.sleep(args.pause)

    report = write_report(rows, ROOT / "evals" / "results")
    print(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} passed — report: {report.relative_to(ROOT)}")
    sys.exit(0 if all(r["passed"] for r in rows) else 1)


if __name__ == "__main__":
    main()
