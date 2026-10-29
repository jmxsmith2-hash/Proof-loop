from proofloop.personas.models import Persona, Traits
from proofloop.web.aggregate.dedupe import build_clusters, cluster_issues
from proofloop.web.aggregate.guardrails import (
    REPLACEMENT,
    contains_demand_prediction,
    strip_demand_predictions,
)
from proofloop.web.aggregate.rank import score_clusters
from proofloop.web.report.charts import bar_chart, line_chart


def issue(n, cat, screen, persona="per_a", session="s_a0", desc=None, source="behavior", replay="CONFIRMED", sev=3):
    return {"id": f"i{n}", "category": cat, "screen_id": screen, "task_id": "t", "persona_id": persona,
            "session_id": session, "title": cat.replace("_", " "), "severity": sev, "source": source,
            "description": desc or f"{cat} problem on {screen} with the 'Continue' control",
            "replay_status": replay, "novelty": 0, "judge_json": {}, "region_json": None, "evidence_json": {}}


def sessions(outcome="completed"):
    out = {}
    for p in ("per_a", "per_b"):
        for r in range(3):
            out[f"s_{p[-1]}{r}"] = {"id": f"s_{p[-1]}{r}", "persona_id": p, "task_id": "t", "repeat_index": r,
                                    "outcome": outcome, "visit_index": 0}
    return out


PERSONAS = {"per_a": Persona(id="per_a", label="a", narrative="", traits=Traits()),
            "per_b": Persona(id="per_b", label="b", narrative="", traits=Traits(eyesight="low_vision"))}


def test_same_category_and_screen_merge_but_screens_never_merge():
    issues = [issue(1, "unexplained_error", "/a"), issue(2, "unexplained_error", "/a", session="s_a1"),
              issue(3, "unexplained_error", "/b"), issue(4, "slow_response", "/s"), issue(5, "no_loading_feedback", "/s")]
    groups = cluster_issues(issues)
    as_ids = sorted(sorted(issues[i]["id"] for i in g) for g in groups)
    assert as_ids == [["i1", "i2"], ["i3"], ["i4", "i5"]]


def test_promotion_needs_recurrence_replay_and_behaviour():
    issues = [
        issue(1, "hidden_control", "/p", session="s_a0"), issue(2, "hidden_control", "/p", session="s_a1"),
        issue(3, "dead_click", "/q", session="s_a0"),                                   # once
        issue(4, "data_lost_on_error", "/r", session="s_a0", replay="NOT_REPRODUCED"),
        issue(5, "data_lost_on_error", "/r", session="s_a2", replay="NOT_REPRODUCED"),  # recurs, not reproduced
        issue(6, "elicited", "/z", session="s_a0", source="elicited", desc="the 'Help' link is tiny"),
        issue(7, "elicited", "/z", persona="per_b", session="s_b0", source="elicited", desc="the 'Help' link is tiny"),
        issue(8, "elicited", "/z", session="s_a1", source="elicited", desc="the 'Help' link is tiny"),
    ]
    clusters = score_clusters(build_clusters(issues, cluster_issues(issues)), sessions(), PERSONAS, 2)
    by_screen = {c.screen_id: c.detail for c in clusters}
    assert by_screen["/p"]["promoted"] and by_screen["/p"]["frequency"] == round(2 / 6, 3)
    assert not by_screen["/q"]["promoted"] and "at most 1 of 3" in by_screen["/q"]["why_not_promoted"]
    assert not by_screen["/r"]["promoted"] and "replay" in by_screen["/r"]["why_not_promoted"]
    assert not by_screen["/z"]["promoted"] and by_screen["/z"]["stated_only"]  # opinion only, never promoted
    assert clusters[0].screen_id == "/p"


def test_ranking_weights_quits_and_confidence():
    base = [issue(1, "hidden_control", "/p", session="s_a0"), issue(2, "hidden_control", "/p", session="s_a1"),
            issue(3, "hidden_control", "/p", persona="per_b", session="s_b0")]
    calm = score_clusters(build_clusters(base, cluster_issues(base)), sessions(), PERSONAS, 2)[0].detail
    quit_ = score_clusters(build_clusters(base, cluster_issues(base)), sessions("quit"), PERSONAS, 2)[0].detail
    assert quit_["score"] > calm["score"] and calm["confidence"] == "high"
    only_b = [issue(1, "hidden_control", "/p", persona="per_b", session="s_b0"),
              issue(2, "hidden_control", "/p", persona="per_b", session="s_b1")]
    d = score_clusters(build_clusters(only_b, cluster_issues(only_b)), sessions(), PERSONAS, 2)[0].detail
    assert d["only_low_confidence"] and d["confidence"] == "low"


def test_demand_predictions_are_removed():
    text = "The form is slow. 40% of users would buy at $20. Expected revenue is high. The error is vague."
    out = strip_demand_predictions(text)
    assert REPLACEMENT in out and "40%" not in out and "revenue" not in out
    assert "The form is slow." in out and "The error is vague." in out
    assert contains_demand_prediction("market size is large") and not contains_demand_prediction("price is $20")


def test_charts_render_svg_with_tooltips():
    svg = bar_chart([("a", 0.5, "a: 1 of 2"), ("b", 1.0, "b: 2 of 2")])
    assert svg.startswith("<svg") and "<title>a: 1 of 2</title>" in svg and "50%" in svg
    lines = line_chart([{"name": "x", "values": [0, 10, 30], "slot": 0, "threshold": 50},
                        {"name": "y", "values": [0, 10, 30], "slot": 1}])
    assert lines.count("<polyline") == 2 and "quits at 50" in lines
    assert lines.count('class="direct-label"') == 2
