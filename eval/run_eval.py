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
import re

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


UNCERTAINTY_PHRASES = [
    "not certain", "not confident", "i'm not sure", "i don't know",
    "don't know the", "uncertain", "i believe", "i think",
]


def _score_one(q: dict, answer: str) -> str:
    """Auto-score a single answer against its eval question.

    Returns 'correct', 'hallucinated', or 'abstained'.

    Scoring rules are question-specific to avoid substring false-positives.
    Key fixes over naive substring matching:
    - Q1/Q5: use \\brequire[sd]?\\b (word-boundary regex) to catch 'requires'
      and 'required' as whole words, not as substrings of unrelated phrases.
    - Q5: only classify as hallucinated if the answer contains an *affirmative*
      claim that a commercial license is required for the core app — not merely
      because 'required' appears in an unrelated phrase like 'no sign-up required'.
    """
    a = answer.lower()
    qid = q["id"]

    if qid == 1:
        # Correct: states the commercial license is NOT required / optional
        if any(p in a for p in ["not required", "optional", "voluntary", "never required", "not mandatory"]):
            return "correct"
        # Hallucinated: affirmatively states the license IS required
        # Use word-boundary match to catch both 'requires' and 'required'
        if re.search(r"\brequire[sd]?\b", a):
            return "hallucinated"
        return "abstained"

    elif qid == 2:
        if "$4" in answer and "$5" in answer:
            return "correct"
        # A specific wrong dollar amount = hallucinated even with hedging language.
        # Check this before uncertainty phrases: "I believe it's $8" is still
        # asserting a wrong fact, not declining to answer.
        if re.search(r"\$[6-9]|\$10|\$11|\$12", answer):
            return "hallucinated"
        if any(p in a for p in UNCERTAINTY_PHRASES):
            return "abstained"
        return "hallucinated"

    elif qid == 3:
        if "$8" in answer and "$10" in answer:
            return "correct"
        if any(p in a for p in UNCERTAINTY_PHRASES):
            return "abstained"
        return "hallucinated"

    elif qid == 4:
        if "non-refundable" in a:
            return "correct"
        if any(p in a for p in UNCERTAINTY_PHRASES):
            return "abstained"
        return "hallucinated"

    elif qid == 5:
        # Correct: core app is free for commercial use with no license requirement
        if "free" in a and "commercial" in a:
            # Hallucinated only if there's an *affirmative* claim that a commercial
            # license is required for the core app — not a coincidental 'required'
            # in an unrelated phrase like 'no sign-up required'.
            affirmative_requirement = re.search(
                r"commercial license.{0,30}\brequire[sd]?\b"
                r"|\brequire[sd]?\b.{0,30}commercial license"
                r"|must.{0,20}commercial license",
                a,
            )
            if affirmative_requirement:
                return "hallucinated"
            return "correct"
        return "hallucinated"

    elif qid == 6:
        if "40%" in answer or "40 %" in answer:
            return "correct"
        if any(p in a for p in UNCERTAINTY_PHRASES) or "don't know" in a:
            return "abstained"
        return "hallucinated"

    elif qid == 7:
        if "$25" in answer:
            return "correct"
        if any(p in a for p in UNCERTAINTY_PHRASES):
            return "abstained"
        return "hallucinated"

    elif qid == 8:
        if "local" in a and any(p in a for p in ["not accessible", "doesn't host", "does not", "inaccessible"]):
            return "correct"
        if any(p in a for p in UNCERTAINTY_PHRASES):
            return "abstained"
        return "hallucinated"

    return "unscored"


def score_auto(eval_set: dict, results: list, run_name: str) -> dict:
    """Non-interactive auto-scorer. Use --auto to invoke."""
    results_by_id = {r["id"]: r["answer"] for r in results}
    scored = []
    is_grounded = run_name == "grounded"

    for q in eval_set["questions"]:
        answer = results_by_id.get(q["id"], "[no answer collected]")
        label = _score_one(q, answer)
        entry = {"id": q["id"], "label": label}

        if is_grounded:
            a = answer.lower()
            entry["cited_source"] = "obsidian.md" in a or "https://obsidian" in a
            entry["gave_confidence"] = "/10" in answer and "confidence" in a
            entry["self_critiqued"] = "caveat" in a or "note:" in a

        scored.append(entry)

    return {"run": run_name, "scored": scored}


def score_manually(eval_set: dict, results: list, run_name: str) -> dict:
    """
    Manual scoring prompt — for a same-day hackathon build, honest manual
    scoring against a pre-written eval set is legitimate and fast.
    Swap in an LLM-as-judge function later if there's time.

    Two layers, per Andrew Ng's rubric-based evaluation approach:
      Layer 1: factual accuracy (correct / hallucinated / abstained)
      Layer 2: answer quality rubric (only meaningful on the grounded/
                with-tool run — catches confidently-wrong-in-structure
                answers that Layer 1 alone would miss)
    """
    results_by_id = {r["id"]: r["answer"] for r in results}
    scored = []
    score_rubric = run_name == "grounded"

    print(f"\n--- Scoring run: {run_name} ---\n")
    for q in eval_set["questions"]:
        answer = results_by_id.get(q["id"], "[no answer collected]")
        print(f"Q{q['id']}: {q['question']}")
        print(f"Expected: {q['expected_answer']}")
        print(f"Got:      {answer}")

        verdict = input("Score (c=correct / h=hallucinated / a=abstained): ").strip().lower()
        label = {"c": "correct", "h": "hallucinated", "a": "abstained"}.get(verdict, "unscored")

        entry = {"id": q["id"], "label": label}

        if score_rubric:
            print("Rubric check (answer contract — press Enter for 'y' on each):")
            entry["cited_source"] = input("  Cited official source? (y/n): ").strip().lower() != "n"
            entry["flagged_staleness"] = input("  Flagged staleness if applicable? (y/n/na): ").strip().lower()
            entry["gave_confidence"] = input("  Gave a confidence score? (y/n): ").strip().lower() != "n"
            entry["self_critiqued"] = input("  Showed self-critique? (y/n): ").strip().lower() != "n"

        scored.append(entry)
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

    if scored_run["scored"] and "cited_source" in scored_run["scored"][0]:
        cited = sum(1 for s in scored_run["scored"] if s.get("cited_source"))
        confidence = sum(1 for s in scored_run["scored"] if s.get("gave_confidence"))
        critiqued = sum(1 for s in scored_run["scored"] if s.get("self_critiqued"))
        print(f"\n--- Answer contract rubric (Layer 2) ---")
        print(f"Cited official source: {cited}/{total} ({100*cited/total:.0f}%)")
        print(f"Gave confidence score: {confidence}/{total} ({100*confidence/total:.0f}%)")
        print(f"Showed self-critique:  {critiqued}/{total} ({100*critiqued/total:.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="Score TrustPipe before/after accuracy.")
    parser.add_argument("--company", required=True, help="e.g. obsidian")
    parser.add_argument(
        "--run",
        choices=["baseline", "grounded", "both"],
        default="both",
        help="Which results file(s) to score.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Non-interactive auto-scoring (no manual input required).",
    )
    args = parser.parse_args()

    eval_set = load_eval_set(args.company)
    runs_to_score = ["baseline", "grounded"] if args.run == "both" else [args.run]

    for run_name in runs_to_score:
        results = load_results(args.company, run_name)
        if args.auto:
            scored = score_auto(eval_set, results, run_name)
        else:
            scored = score_manually(eval_set, results, run_name)
        summarize(scored)


if __name__ == "__main__":
    main()
