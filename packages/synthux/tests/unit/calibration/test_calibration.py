import asyncio
from pathlib import Path

import pytest

from synthux.calibration.compare import compare, history
from synthux.calibration.importer import ImportError_, import_real_study, load_real_results, real_study
from synthux.calibration.match import infer_category, match_issues
from synthux.calibration.metrics import completion_metrics, quit_metrics, spearman
from synthux.calibration.profiles import CalibrationProfile, apply_changes, load_profile
from synthux.calibration.tune import apply_suggestions, suggest
from synthux.core.db import utcnow

EX = Path(__file__).resolve().parents[3] / "examples" / "real_users"


def test_json_and_csv_imports_are_equivalent(db):
    a = load_real_results(EX / "demo_study.json")
    b = load_real_results(tasks_csv=EX / "tasks.csv", issues_csv=EX / "issues.csv")
    assert [(t["task_id"], t["completed"]) for t in a["tasks"]] == [(t["task_id"], t["completed"]) for t in b["tasks"]]
    assert [i["title"] for i in a["issues"]] == [i["title"] for i in b["issues"]]
    sid = import_real_study(db, "x", a)
    stored = real_study(db, sid)
    assert len(stored["issues"]) == 7 and stored["quits"]["signup"][0]["count"] == 5


def test_import_validation(tmp_path):
    bad = tmp_path / "t.csv"
    bad.write_text("task_id,participants,completed\nsignup,3,5\n", encoding="utf-8")
    with pytest.raises(ImportError_):
        load_real_results(tasks_csv=bad)
    with pytest.raises(ImportError_):
        load_real_results()


def test_real_data_is_anonymized(db):
    data = {"tasks": [], "issues": [{"title": "Maria Lopez could not log in", "description": "call 555-123-4567"}]}
    sid = import_real_study(db, "x", data)
    issue = real_study(db, sid)["issues"][0]
    assert "Maria" not in issue["title"] and "555" not in issue["description"]


def test_category_inference_and_matching():
    assert infer_category("Shipping page slow, looked frozen") in ("slow_response", "no_loading_feedback")
    assert infer_category("Form wiped all fields, had to retype") == "data_lost_on_error"
    real = [{"title": "Continue button nearly invisible", "screen": "/plans", "task_id": "p", "severity": 4},
            {"title": "Prices unclear", "screen": "/plans", "task_id": "p", "severity": 2}]
    syn = [{"title": "Control needed to continue is hard to find", "category": "hidden_control", "screen_id": "/plans",
            "severity": 3, "detail_json": {"tasks": ["p"], "description": "'Continue' 1.4:1 contrast"}},
           {"title": "Slow response", "category": "slow_response", "screen_id": "/x", "severity": 2,
            "detail_json": {"tasks": ["q"], "description": ""}}]
    both, real_only, syn_only = match_issues(real, syn)
    assert [(m.real["title"], m.synthetic["category"]) for m in both] == [("Continue button nearly invisible", "hidden_control")]
    assert [r["title"] for r in real_only] == ["Prices unclear"] and [s["category"] for s in syn_only] == ["slow_response"]


def test_metrics():
    assert spearman([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0 and spearman([1, 2], [2, 1]) is None
    sessions = [{"task_id": "a", "visit_index": 0, "outcome": o} for o in ("completed", "completed", "quit", "quit")]
    m = completion_metrics([{"task_id": "a", "participants": 4, "completed": 1}], sessions)
    assert m["per_task"]["a"] == {"real": 0.25, "synthetic": 0.5, "delta": 0.25} and m["bias"] == 0.25
    from collections import Counter
    q = quit_metrics({"a": [{"screen": "/x", "count": 3}]}, {"a": Counter({"/x": 2, "/y": 1})})
    assert q["top_agreement"] == 1.0 and q["per_task"]["a"]["jaccard"] == 0.5


def test_suggestions_follow_the_disagreement():
    profile = CalibrationProfile(frustration={"weights": {"cannot_find": 7, "no_change": 6, "slow_load": 8}})
    too_persistent = suggest(profile, {"bias": 0.3}, [], [], [])
    t3 = next(s for s in too_persistent if s["path"] == "frustration.threshold_by_patience.3")
    assert t3["to"] < t3["from"]
    missed = suggest(profile, {"bias": 0.0}, [], [{"title": "Could not see the grey Continue button", "screen": "/p"}], [])
    paths = {s["path"]: s for s in missed}
    assert paths["perception.glance_contrast.normal"]["to"] > 2.2
    assert paths["frustration.weights.cannot_find"]["to"] == 9
    assert any(s["kind"] == "note" for s in missed)
    over = suggest(profile, {"bias": None}, [], [], [{"category": "dead_click"}])
    assert over[0]["path"] == "frustration.weights.no_change" and over[0]["to"] < 6
    assert "may have missed" in over[0]["reason"]


def test_apply_creates_new_version_with_history(db, settings, tmp_path):
    base = load_profile(settings, db)
    suggestions = suggest(base, {"bias": 0.25}, [], [{"title": "Error message meaningless", "screen": "/c"}], [])
    (tmp_path / "calibration").mkdir()
    new = apply_suggestions(db, base, suggestions, tmp_path, only=[1, 2])
    assert new.version == "default-cal1"
    row = db.one("SELECT * FROM calibration_profiles WHERE version = 'default-cal1'")
    assert row["parent"] == "default"
    assert (tmp_path / "calibration" / "default-cal1.yaml").read_text(encoding="utf-8").startswith("# Calibrated from default")
    again = apply_suggestions(db, new, suggestions, None)
    assert again.version == "default-cal2"
    assert load_profile(settings, db, "default-cal2").version == "default-cal2"   # loads from the database


def test_apply_changes_on_nested_paths():
    p = apply_changes(CalibrationProfile(), [{"path": "frustration.threshold_by_patience.1", "value": 20},
                                            {"path": "prompt_notes", "value": "watch for X"}], "v2")
    assert p.frustration.threshold_by_patience[1] == 20 and p.prompt_notes == ["watch for X"] and p.version == "v2"


def test_compare_end_to_end_on_stored_rows(db, settings):
    db.insert("runs", {"id": "run_1", "created_at": utcnow(), "study": "s", "target_url": "http://x", "status": "done",
                       "config_json": {"tasks": ["plan"], "repeats": 2}, "calibration_version": "default"})
    load_profile(settings, db)
    for n, outcome in enumerate(["completed", "completed", "completed", "completed"]):
        db.insert("sessions", {"id": f"s{n}", "run_id": "run_1", "persona_id": "p", "task_id": "plan",
                               "repeat_index": n, "seed": n, "conditions_json": {}, "outcome": outcome})
    db.insert("issue_clusters", {"id": "c1", "run_id": "run_1", "title": "Control needed to continue is hard to find",
                                 "category": "hidden_control", "screen_id": "/plans", "severity": 3, "runs_seen": 2,
                                 "runs_total": 2, "personas_json": ["p"], "score": 1, "confidence": "high", "promoted": 1,
                                 "member_ids_json": [], "detail_json": {"tasks": ["plan"], "description": "grey Continue"}})
    sid = import_real_study(db, "real", {"tasks": [{"task_id": "plan", "participants": 4, "completed": 1,
                                                    "quits": [{"screen": "/plans", "reason": "lost", "count": 3}]}],
                                         "issues": [{"task_id": "plan", "screen": "/plans",
                                                     "title": "Continue button invisible", "severity": 4}]})
    result = asyncio.run(compare(settings, db, "run_1", sid))
    assert result["metrics"]["issues"]["f1"] == 1.0
    assert result["metrics"]["completion"]["bias"] == 0.75        # personas far too persistent here
    assert any(s["path"].startswith("frustration.threshold_by_patience") and s["to"] < s["from"]
               for s in result["suggestions"])
    assert history(db)[0]["agreement"] == result["metrics"]["agreement"]


def test_search_scales_only_the_chosen_knobs():
    from synthux.calibration.profiles import CalibrationProfile
    from synthux.calibration.search import scaled_profile

    base = CalibrationProfile()
    p = scaled_profile(base, {"frustration": 0.5, "patience": 1.0, "perception": 2.0}, "default-search1")
    assert p.version == "default-search1"
    assert p.frustration.threshold_by_patience[3] == base.frustration.threshold_by_patience[3] * 0.5
    assert p.patience == base.patience
    assert p.perception.glance_contrast["normal"] == round(base.perception.glance_contrast["normal"] * 2, 2)
    tiny = scaled_profile(base, {"patience": 0.01}, "x")
    assert tiny.patience.base_steps == base.patience.min_steps          # never below the floor


def test_search_score_rewards_matching_completion_and_quit_points(db):
    from synthux.calibration.search import score_run

    db.insert("runs", {"id": "run_a", "created_at": "t", "study": "s", "target_url": "u", "status": "done",
                       "calibration_version": "default", "config_json": {"tasks": ["t1"]}})
    for i, outcome in enumerate(["completed", "quit", "completed", "completed"]):
        db.insert("sessions", {"id": f"ses_{i}", "run_id": "run_a", "persona_id": "p", "task_id": "t1",
                               "repeat_index": i, "visit_index": 0, "seed": i, "conditions_json": {},
                               "outcome": outcome, "steps_count": 1, "sim_seconds": 1, "wall_seconds": 1,
                               "final_frustration": 0, "memory_json": {}, "started_at": "t", "ended_at": "t"})
    real = {"tasks": [{"task_id": "t1", "participants": 4, "completed": 3}], "quits": {}}
    score, mae, quits = score_run(db, "run_a", real, ["t1"])
    assert mae == 0.0 and score == 1.0
    worse = {"tasks": [{"task_id": "t1", "participants": 4, "completed": 1}], "quits": {}}
    assert score_run(db, "run_a", worse, ["t1"])[0] < score
