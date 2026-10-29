from proofloop.personas.models import Persona, Traits
from proofloop.web.calibration.profiles import NoveltyParams
from proofloop.web.judge.novelty import classify
from proofloop.web.judge.repeat import recurrence


def sessions():
    out = []
    for p in ("per_a", "per_b"):
        for r in range(3):
            out.append({"id": f"{p}_{r}", "persona_id": p, "task_id": "t", "repeat_index": r, "visit_index": 0,
                        "outcome": "completed"})
    return out


def issue(session, cat="hidden_control", screen="/plans", desc="'Continue' at 1.4:1 contrast"):
    return {"session_id": session, "persona_id": session.rsplit("_", 1)[0], "task_id": "t", "category": cat,
            "screen_id": screen, "description": desc}


def test_issue_must_recur_to_be_promoted():
    groups = recurrence([issue("per_a_0"), issue("per_a_2"), issue("per_b_1", cat="dead_click")], sessions())
    hidden = groups[("t", "hidden_control", "/plans")]
    once = groups[("t", "dead_click", "/plans")]
    assert hidden.promoted(2) and not once.promoted(2)
    assert hidden.sessions_with == 2 and hidden.sessions_total == 6
    assert "2/3 runs" in hidden.describe()


def test_convention_violations_are_never_novelty():
    personas = {"per_a": Persona(id="per_a", label="", narrative="", traits=Traits(familiarity=1))}
    s = {x["id"]: x for x in sessions()}
    novel, reason = classify("unexplained_error", [issue("per_a_0", cat="unexplained_error")], personas, s, NoveltyParams())
    assert not novel and "convention" in reason
    novel, _ = classify("hidden_control", [issue("per_a_0")], personas, s, NoveltyParams())
    assert not novel  # low contrast breaks WCAG


def test_unfamiliar_but_learnable_is_novelty():
    personas = {"per_a": Persona(id="per_a", label="", narrative="", traits=Traits(familiarity=1)),
                "per_b": Persona(id="per_b", label="", narrative="", traits=Traits(familiarity=5))}
    s = {x["id"]: x for x in sessions()}
    gesture = [issue("per_a_0", cat="elicited", screen="/feed", desc="swipe menu took a moment to find"),
               issue("per_a_1", cat="elicited", screen="/feed", desc="swipe menu took a moment to find")]
    novel, reason = classify("elicited", gesture, personas, s, NoveltyParams())
    assert novel and "new to this kind of product" in reason
    with_experts = gesture + [issue("per_b_0", cat="elicited", screen="/feed", desc="swipe menu")]
    assert not classify("elicited", with_experts, personas, s, NoveltyParams())[0]
