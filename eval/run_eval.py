"""
Generic before/after eval runner for TrustPipe.

Usage:
    python run_eval.py --company obsidian

Loads eval/eval_sets/{company}.json and scores answers as:
  correct / hallucinated / abstained

Run once with the MCP tool disconnected (baseline) and once connected
(grounded), and compare the two accuracy numbers. This script scores
answers you've already collected — it does not call the model itself,
since the two runs (with/without MCP) need to happen through whatever
client you're demoing with (Claude Desktop, Claude Code, etc).

Expected input format (results/{company}_baseline.json and
results/{company}_grounded.json), one entry per question id:
[
  { "id": 1, "answer": "the model's actual answer text" },
  ...
]
"""

import argparse
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_eval_set(company: str) -> dict:
    path = os.path.join(BASE_DIR, "eval_sets", f"{company}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No eval set found for '{company}' at {path}. "
            f"Create one following the structure in eval_sets/obsidian.json."
        )
    with open(path, "r") as f:
        return json.load(f)


def load_results(company: str, run_name: str) -> list:
    path = os.path.join(BASE_DIR, "results", f"{company}_{run_name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No results file found at {path}. "
            f"Collect answers first and save them in this format — see module docstring."
        )
    with open(path, "r") as f:
        return json.load(f)


def score_manually(eval_set: dict, results: list, run_name: str) -> dict:
    """
    Manual scoring prompt — for a same-day hackathon build, honest manual
    scoring against a pre-written eval set is legitimate and fast.
    Swap in an LLM-as-judge function later if there's time.
    """
    results_by_id = {r["id"]: r["answer"] for r in results}
    scored = []

    print(f"\n--- Scoring run: {run_name} ---\n")
    for q in eval_set["questions"]:
        answer = results_by_id.get(q["id"], "[no answer collected]")
        print(f"Q{q['id']}: {q['question']}")
        print(f"Expected: {q['expected_answer']}")
        print(f"Got:      {answer}")
        verdict = input("Score (c=correct / h=hallucinated / a=abstained): ").strip().lower()
        label = {"c": "correct", "h": "hallucinated", "a": "abstained"}.get(verdict, "unscored")
        scored.append({"id": q["id"], "label": label})
        print()

    return {"run": run_name, "scored": scored}


def summarize(scored_run: dict) -> None:
    labels = [s["label"] for s in scored_run["scored"]]
    total = len(labels)
    correct = labels.count("correct")
    hallucinated = labels.count("hallucinated")
    abstained = labels.count("abstained")

    print(f"\n=== {scored_run['run']} summary ===")
    print(f"Correct:      {correct}/{total} ({100*correct/total:.0f}%)")
    print(f"Hallucinated: {hallucinated}/{total} ({100*hallucinated/total:.0f}%)")
    print(f"Abstained:    {abstained}/{total} ({100*abstained/total:.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="Score TrustPipe before/after accuracy.")
    parser.add_argument("--company", required=True, help="e.g. obsidian")
    parser.add_argument(
        "--run",
        choices=["baseline", "grounded", "both"],
        default="both",
        help="Which results file(s) to score.",
    )
    args = parser.parse_args()

    eval_set = load_eval_set(args.company)
    runs_to_score = ["baseline", "grounded"] if args.run == "both" else [args.run]

    for run_name in runs_to_score:
        results = load_results(args.company, run_name)
        scored = score_manually(eval_set, results, run_name)
        summarize(scored)


if __name__ == "__main__":
    main()
