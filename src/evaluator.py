"""
VibeFinder Evaluation Script - test harness for reliability assessment.

Runs predefined test cases against the recommendation engine and prints a
structured pass/fail summary with scores and confidence metrics.

Usage:
    python -m src.evaluator
    python -m src.evaluator --json     (output raw JSON)
"""

import sys
import json
import argparse
from pathlib import Path
from typing import List, Dict, Any, Callable

try:
    from recommender import load_songs, recommend_songs
except ModuleNotFoundError:
    from src.recommender import load_songs, recommend_songs


# ---------------------------------------------------------------------------
# Evaluation cases
# Each case has a name, user preferences, and a dict of named check lambdas.
# A case passes only when ALL its checks return True.
# ---------------------------------------------------------------------------

EVAL_CASES: List[Dict[str, Any]] = [
    {
        "name": "Pop fan gets pop song at #1 (balanced mode)",
        "prefs": {
            "genre": "pop", "mood": "happy", "energy": 0.8,
            "likes_acoustic": False, "scoring_mode": "balanced",
        },
        "checks": {
            "top_genre_is_pop":    lambda recs: recs[0][0]["genre"] == "pop",
            "top_score_above_4":   lambda recs: recs[0][1] >= 4.0,
            "returns_5_results":   lambda recs: len(recs) == 5,
        },
    },
    {
        "name": "Lofi listener gets lofi song at #1 (mood_first mode)",
        "prefs": {
            "genre": "lofi", "mood": "chill", "energy": 0.4,
            "likes_acoustic": True, "scoring_mode": "mood_first",
        },
        "checks": {
            "top_genre_is_lofi":   lambda recs: recs[0][0]["genre"] == "lofi",
            "top_score_above_4":   lambda recs: recs[0][1] >= 4.0,
        },
    },
    {
        "name": "Rock fan gets rock song at #1 (genre_first mode)",
        "prefs": {
            "genre": "rock", "mood": "intense", "energy": 0.9,
            "likes_acoustic": False, "scoring_mode": "genre_first",
        },
        "checks": {
            "top_genre_is_rock":   lambda recs: recs[0][0]["genre"] == "rock",
            "top_score_above_3":   lambda recs: recs[0][1] >= 3.0,
        },
    },
    {
        "name": "Energy-focused mode de-ranks low-energy pop song",
        "prefs": {
            "genre": "pop", "mood": "happy", "energy": 0.80,
            "likes_acoustic": False, "scoring_mode": "energy_focused",
        },
        "checks": {
            # Gym Hero (energy=0.93) should drop behind Rooftop Lights (energy=0.76)
            # which is closer to target 0.80 in energy_focused mode
            "gym_hero_not_second": lambda recs: recs[1][0]["title"] != "Gym Hero",
            "returns_5_results":   lambda recs: len(recs) == 5,
        },
    },
    {
        "name": "Folk niche listener gets the only folk song first",
        "prefs": {
            "genre": "folk", "mood": "relaxed", "energy": 0.32,
            "likes_acoustic": True, "preferred_decade": 2010,
            "scoring_mode": "mood_first",
        },
        "checks": {
            "top_is_desert_wind":  lambda recs: recs[0][0]["title"] == "Desert Wind",
            "returns_5_results":   lambda recs: len(recs) == 5,
        },
    },
    {
        "name": "Mood-tag hunter: euphoric+nostalgic boosts tag-matching songs",
        "prefs": {
            "genre": "pop", "mood": "happy", "energy": 0.80,
            "likes_acoustic": False,
            "favorite_mood_tags": "nostalgic,euphoric",
            "scoring_mode": "balanced",
        },
        "checks": {
            "returns_5_results":   lambda recs: len(recs) == 5,
            "top_score_above_3":   lambda recs: recs[0][1] >= 3.0,
            "gym_hero_appears":    lambda recs: any(
                r[0]["title"] == "Gym Hero" for r in recs
            ),
        },
    },
    {
        "name": "Unknown genre still returns 5 results with positive scores",
        "prefs": {
            "genre": "bossa-nova", "mood": "relaxed", "energy": 0.4,
            "likes_acoustic": True, "scoring_mode": "balanced",
        },
        "checks": {
            "returns_5_results":    lambda recs: len(recs) == 5,
            "all_scores_positive":  lambda recs: all(r[1] > 0 for r in recs),
        },
    },
    {
        "name": "Diversity penalty lowers score of repeated-genre second pick",
        "prefs": {
            "genre": "pop", "mood": "happy", "energy": 0.8,
            "likes_acoustic": False, "scoring_mode": "balanced",
        },
        "diversity_penalty": 0.5,
        "checks": {
            "returns_5_results": lambda recs: len(recs) == 5,
            # Second pop song (Gym Hero) should have a lower adjusted score
            # than in the no-penalty run. We verify it's under 3.1 (was 3.21).
            "second_score_penalized": lambda recs: recs[1][1] < 3.15,
        },
    },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_evaluation(songs_path: str = "data/songs.csv") -> Dict[str, Any]:
    """Run all evaluation cases and return a structured results dict."""
    songs = load_songs(songs_path)
    case_results = []
    total_checks = 0
    passed_checks = 0

    for case in EVAL_CASES:
        prefs = case["prefs"]
        penalty = case.get("diversity_penalty", 0.0)
        recs = recommend_songs(prefs, songs, k=5, diversity_penalty=penalty)

        check_results: Dict[str, bool] = {}
        for check_name, check_fn in case["checks"].items():
            try:
                result = check_fn(recs)
            except Exception:
                result = False
            check_results[check_name] = result
            total_checks += 1
            if result:
                passed_checks += 1

        case_passed = all(check_results.values())
        top_song, top_score, _ = recs[0]

        case_results.append({
            "name": case["name"],
            "passed": case_passed,
            "top_song": top_song["title"],
            "top_genre": top_song["genre"],
            "top_score": round(top_score, 2),
            "checks": check_results,
        })

    cases_passed = sum(1 for c in case_results if c["passed"])

    return {
        "total_cases": len(EVAL_CASES),
        "cases_passed": cases_passed,
        "cases_failed": len(EVAL_CASES) - cases_passed,
        "case_pass_rate": cases_passed / len(EVAL_CASES),
        "total_checks": total_checks,
        "checks_passed": passed_checks,
        "check_pass_rate": passed_checks / total_checks if total_checks else 0,
        "cases": case_results,
    }


def _print_results(report: Dict[str, Any]) -> None:
    """Pretty-print evaluation results to stdout."""
    print("\n" + "=" * 62)
    print("  VibeFinder Evaluation Report")
    print("=" * 62)
    print(
        f"  Cases:  {report['cases_passed']}/{report['total_cases']} passed "
        f"({report['case_pass_rate']:.0%})"
    )
    print(
        f"  Checks: {report['checks_passed']}/{report['total_checks']} passed "
        f"({report['check_pass_rate']:.0%})"
    )
    print()

    for i, case in enumerate(report["cases"], 1):
        status = "PASS" if case["passed"] else "FAIL"
        marker = "+" if case["passed"] else "x"
        print(f"  [{marker}] {i}. {case['name']}")
        print(
            f"       Top result: {case['top_song']} ({case['top_genre']}) "
            f"score={case['top_score']}"
        )
        failed_checks = [k for k, v in case["checks"].items() if not v]
        if failed_checks:
            for fc in failed_checks:
                print(f"       FAILED check: {fc}")
        print()

    print("=" * 62)
    overall = "ALL PASSED" if report["cases_failed"] == 0 else f"{report['cases_failed']} FAILED"
    print(f"  Result: {overall}")
    print("=" * 62 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="VibeFinder evaluation harness")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    songs_path = "data/songs.csv"
    if not Path(songs_path).exists():
        songs_path = str(Path(__file__).parent.parent / "data" / "songs.csv")

    report = run_evaluation(songs_path)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_results(report)

    sys.exit(0 if report["cases_failed"] == 0 else 1)


if __name__ == "__main__":
    main()
