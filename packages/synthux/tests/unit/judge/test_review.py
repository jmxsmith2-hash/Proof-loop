import asyncio

from synthux.core.llm import FakeLLM, LLMGateway
from synthux.judge.review import ModelJudge, RuleJudge, revise_issue

from .conftest import make_issue, make_step


def review(issue, steps):
    return asyncio.run(RuleJudge(3.0).review(issue, steps))


def test_supported_specific_issue_is_accepted(shot):
    v = review(make_issue(shot), [make_step(0, error_seen=True), make_step(1)])
    assert v.verdict == "accept" and "supported by behaviour" in v.reasons[0]


def test_unsupported_issue_is_rejected(shot):
    v = review(make_issue(shot), [make_step(0), make_step(1)])
    assert v.verdict == "reject" and "unsupported" in v.reasons[0]


def test_flattering_issue_is_rejected(shot):
    v = review(make_issue(shot, description="Great, intuitive checkout page with a nice layout overall"), [])
    assert v.verdict == "reject" and "flattering" in v.reasons[0]


def test_vague_issue_is_sent_back_then_made_specific(shot):
    vague = make_issue(shot, description="Something about this screen could be better", region_json=None)
    v = review(vague, [make_step(1, error_seen=True)])
    assert v.verdict == "revise" and v.feedback
    # quoted text is found on the persona's own screenshot when the location is missing
    located = make_issue(shot, region_json=None)
    revised = revise_issue(located, "give its position")
    assert revised and revised["region_json"] and "middle left" in revised["description"] or "top left" in revised["description"]


def test_revision_without_anything_to_point_at_fails(shot):
    assert revise_issue(make_issue(shot, description="It felt wrong", region_json=None), "x") is None


def test_missing_evidence_is_rejected(shot):
    v = review(make_issue(shot, evidence_json={"steps": []}), [make_step(1, error_seen=True)])
    assert v.verdict == "reject" and "evidence" in v.reasons[0]


def test_elicited_complaint_needs_behaviour_on_that_element(shot):
    complaint = make_issue(shot, category="elicited", source="elicited", step_idx=0,
                           description="The text 'Continue' is faint and I nearly missed it at the bottom",
                           region_json={"x": 590, "y": 390, "w": 140, "h": 40})
    unrelated = [make_step(0, no_change=True, action={"kind": "click", "x": 100, "y": 50})]
    assert review(complaint, unrelated).verdict == "reject"
    own_slip = [make_step(0, no_change=True, noise_applied=True, action={"kind": "click", "x": 650, "y": 410})]
    assert review(complaint, own_slip).verdict == "reject"
    related = [make_step(0, scroll_search=True, no_change=True, action={"kind": "click", "x": 650, "y": 410})]
    assert review(complaint, related).verdict == "accept"


def test_agent_limitations_are_not_judged_as_ux(shot):
    v = review(make_issue(shot, failure_class="AGENT_LIMITATION", category="captcha"), [])
    assert v.verdict == "accept" and "agent limitation" in v.reasons[0]


def test_model_judge_runs_rules_first(settings, shot):
    settings.engine.roles = {"judge": "fake"}
    fake = FakeLLM(lambda r: {"verdict": "reject", "reasons": ["only the tester's mistake"], "feedback": ""})
    judge = ModelJudge(LLMGateway(settings, clients={"fake": fake}), 3.0)
    unsupported = asyncio.run(judge.review(make_issue(shot), [make_step(1)]))
    assert unsupported.verdict == "reject" and not fake.calls          # rules rejected it; no model call
    supported = asyncio.run(judge.review(make_issue(shot), [make_step(1, error_seen=True)]))
    assert supported.verdict == "reject" and len(fake.calls) == 1 and fake.calls[0].images
