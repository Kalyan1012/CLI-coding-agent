"""Benchmark the agent: inject known bugs, let it fix them, grade with real assertions.

    uv run benchmark.py              # all cases
    uv run benchmark.py --cases 3    # first 3 only
    uv run benchmark.py --verbose    # show the agent's full step-by-step output

Needs GEMINI_API_KEY (makes live API calls). The agent itself never executes code; this
harness runs plain Python assertions only to GRADE the result afterwards.
Restore python_project/main.py to its original, bug-free state before running.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from google import genai

import agent
import tools

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "python_project" / "main.py"
REAL_WORKSPACE = tools.WORKSPACE

# Each case swaps ONE exact line (`old` -> `new`) and gives a plain-English symptom.
# `check` is a boolean expression over the module `m`; it must fail on the buggy code.
CASES = [
    {"id": "add-wrong-operator", "old": "    return a + b", "new": "    return a - b",
     "description": "add(2, 3) returns -1 but it should return 5.",
     "check": "m.add(2, 3) == 5 and m.add(10, 5) == 15"},
    {"id": "factorial-off-by-one", "old": "    for i in range(2, n + 1):", "new": "    for i in range(2, n):",
     "description": "factorial(5) returns 24 but it should return 120.",
     "check": "m.factorial(5) == 120 and m.factorial(3) == 6 and m.factorial(0) == 1"},
    {"id": "factorial-bad-start", "old": "    result = 1", "new": "    result = 0",
     "description": "factorial always returns 0, no matter what number I give it.",
     "check": "m.factorial(4) == 24"},
    {"id": "is-prime-loop-bound", "old": "    while i * i <= n:", "new": "    while i * i < n:",
     "description": "is_prime(9) says True but 9 is not prime.",
     "check": "m.is_prime(9) is False and m.is_prime(4) is False and m.is_prime(17) is True"},
    {"id": "is-prime-base-case", "old": "    if n < 2:", "new": "    if n < 1:",
     "description": "is_prime(1) says True but 1 is not a prime number.",
     "check": "m.is_prime(1) is False and m.is_prime(2) is True"},
    {"id": "find-max-wrong-compare", "old": "        if value > max_value:", "new": "        if value < max_value:",
     "description": "find_max([3, 41, 7]) returns 3 but the biggest number is 41.",
     "check": "m.find_max([3, 41, 7]) == 41 and m.find_max([5]) == 5"},
    {"id": "find-max-skips-item", "old": "    for value in values[1:]:", "new": "    for value in values[2:]:",
     "description": "find_max([1, 9, 2]) returns 2 instead of 9. It seems to ignore the second item.",
     "check": "m.find_max([1, 9, 2]) == 9"},
    {"id": "average-wrong-divisor", "old": "    return total / len(values)", "new": "    return total / (len(values) + 1)",
     "description": "average([2, 4, 6]) returns 3.0 but it should be 4.0.",
     "check": "m.average([2, 4, 6]) == 4"},
    {"id": "average-floor-division", "old": "    return total / len(values)", "new": "    return total // len(values)",
     "description": "average([1, 2]) returns 1 but it should return 1.5.",
     "check": "m.average([1, 2]) == 1.5"},
    {"id": "reverse-index-error", "old": "    right = len(chars) - 1", "new": "    right = len(chars)",
     "description": "reverse_string('hello') crashes with an IndexError.",
     "check": "m.reverse_string('hello') == 'olleh' and m.reverse_string('') == ''"},
    {"id": "vowels-missing-letter", "old": '        if ch in "aeiou":', "new": '        if ch in "aeio":',
     "description": "count_vowels('Education') returns 4 but it has 5 vowels.",
     "check": "m.count_vowels('Education') == 5"},
    {"id": "vowels-case-sensitive", "old": "    for ch in text.lower():", "new": "    for ch in text:",
     "description": "count_vowels('AEIOU') returns 0 but it should return 5.",
     "check": "m.count_vowels('AEIOU') == 5 and m.count_vowels('Education') == 5"},
]


def _script(checks: list[str]) -> str:
    return "import main as m\n" + "\n".join(f"assert {c}" for c in checks)


FULL_CHECK = _script([c["check"] for c in CASES])


def _passes(workspace: Path, script: str) -> bool:
    """Run a grading script against workspace/main.py in a separate process.

    -B stops Python writing .pyc files: a same-size edit made within the same second as an
    earlier run would otherwise be graded against stale cached bytecode.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=workspace, capture_output=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def run_case(client, case: dict, clean_source: str, verbose: bool) -> dict:
    if clean_source.count(case["old"]) != 1:
        raise RuntimeError(f"{case['id']}: line to mutate must occur exactly once in main.py")
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "python_project"
        workspace.mkdir()
        (workspace / "main.py").write_text(clean_source.replace(case["old"], case["new"], 1))
        if _passes(workspace, FULL_CHECK):
            raise RuntimeError(f"{case['id']}: the injected bug does not break the checks")

        tools.WORKSPACE = workspace
        started = time.time()
        error = None
        claimed, attempts = False, 0
        try:
            quiet = contextlib.nullcontext() if verbose else contextlib.redirect_stdout(io.StringIO())
            with quiet:
                run = agent.run_agent(client, case["description"])
            claimed, attempts = run.success, run.attempts
        except agent.AgentError as exc:
            error = str(exc)
        finally:
            tools.WORKSPACE = REAL_WORKSPACE
        solved = claimed and _passes(workspace, FULL_CHECK)  # graded by assertions, not by the model
        return {
            "id": case["id"], "solved": solved, "errored": error is not None,
            "agent_claimed_fixed": claimed, "attempts": attempts,
            "seconds": round(time.time() - started, 1), "error": error,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the bug-fixing agent.")
    parser.add_argument("--cases", type=int, default=len(CASES), help="run only the first N cases")
    parser.add_argument("--verbose", action="store_true", help="show the agent's full output")
    parser.add_argument("--delay", type=float, default=10.0, help="seconds to pause between cases")
    parser.add_argument("--only", help="comma-separated case ids to run (e.g. to re-run errored ones)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
        return 1

    clean_source = SOURCE.read_text()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.py").write_text(clean_source)
        if not _passes(Path(tmp), FULL_CHECK):
            print("python_project/main.py is not in its original bug-free state. Restore it and retry.")
            return 1

    client = genai.Client()
    model = os.getenv("GEMINI_MODEL") or agent.DEFAULT_MODEL
    cases = CASES[: args.cases]
    if args.only:
        wanted = {name.strip() for name in args.only.split(",")}
        unknown = wanted - {c["id"] for c in CASES}
        if unknown:
            print(f"Unknown case ids: {', '.join(sorted(unknown))}")
            return 1
        cases = [c for c in CASES if c["id"] in wanted]
    print(f"Benchmarking {model} on {len(cases)} injected bugs ...\n")

    results = []
    for number, case in enumerate(cases, start=1):
        outcome = run_case(client, case, clean_source, args.verbose)
        results.append(outcome)
        verdict = "ERROR" if outcome["errored"] else ("PASS" if outcome["solved"] else "FAIL")
        note = f"  ({outcome['error'][:90]}...)" if outcome["error"] else ""
        print(f"[{number:>2}/{len(cases)}] {verdict:<5} {case['id']:<26} "
              f"attempts={outcome['attempts']}  {outcome['seconds']}s{note}")
        time.sleep(args.delay)

    # Cases that died on an API error (rate limit, outage) say nothing about the agent: not scored.
    completed = [r for r in results if not r["errored"]]
    errored = len(results) - len(completed)
    total = len(completed)
    solved = [r for r in completed if r["solved"]]
    first_try = [r for r in solved if r["attempts"] == 1]
    false_claims = [r for r in completed if r["agent_claimed_fixed"] and not r["solved"]]
    avg_attempts = sum(r["attempts"] for r in solved) / len(solved) if solved else 0.0
    avg_seconds = sum(r["seconds"] for r in completed) / total if total else 0.0

    print("\n=== Summary ===")
    print(f"Model:                          {model}")
    print(f"Solved (verified by asserts):   {len(solved)}/{total} ({100 * len(solved) // max(total, 1)}%)")
    print(f"Solved on first attempt:        {len(first_try)}/{total}")
    print(f"Avg attempts (solved cases):    {avg_attempts:.2f}")
    print(f"Avg time per case:              {avg_seconds:.1f}s")
    print(f"Agent said fixed but was wrong: {len(false_claims)}")
    if errored:
        failed_ids = ",".join(r["id"] for r in results if r["errored"])
        print(f"API errors (NOT scored):        {errored}")
        print(f"Re-run them with:  uv run benchmark.py --only {failed_ids}")

    report = {
        "model": model, "date": date.today().isoformat(), "total": total, "api_errors": errored,
        "solved": len(solved), "solved_first_attempt": len(first_try),
        "false_claims": len(false_claims), "cases": results,
    }
    (ROOT / "benchmark_results.json").write_text(json.dumps(report, indent=2))
    print("Saved benchmark_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
