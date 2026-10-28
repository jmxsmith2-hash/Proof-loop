import asyncio
from pathlib import Path

from PIL import Image, ImageDraw

from tests.fonts import font

from synthux.core.llm import FakeLLM, LLMGateway
from synthux.elicitation.questions import (Complaint, HeuristicElicitor, ModelElicitor, load_scales,
                                           vague_complaint)
from synthux.personas.models import Persona, Traits

REPO = Path(__file__).resolve().parents[3]


def _shot(tmp_path, name, text):
    img = Image.new("RGB", (800, 400), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 100), text, fill="black", font=font(30))
    d.text((40, 250), "Free returns within 30 days", fill=(150, 150, 150), font=font(16))
    p = tmp_path / name
    img.save(p, "JPEG")
    return str(p)


def _data(tmp_path):
    a, b = _shot(tmp_path, "a.jpg", "Your cart"), _shot(tmp_path, "b.jpg", "Shipping options")
    steps = [
        {"idx": 0, "screen_id": "/cart", "frustration": 6, "screenshot_path": a,
         "intended_json": {"kind": "click", "target": "Go to shipping", "x": 100, "y": 120},
         "signals_json": {"no_change": True, "loading": True}},
        {"idx": 1, "screen_id": "/cart", "frustration": 27, "screenshot_path": a,
         "intended_json": {"kind": "wait"}, "signals_json": {"slow_load": True, "load_seconds": 6.2, "load_trigger": "Go to shipping"}},
        {"idx": 2, "screen_id": "/shipping", "frustration": 23, "screenshot_path": b,
         "intended_json": {"kind": "click", "target": "Continue"}, "signals_json": {}},
    ]
    session = {"id": "ses_1", "outcome": "completed", "final_frustration": 23, "seed": 5}
    return session, steps


def test_heuristic_questionnaire_is_behavioural_and_located(tmp_path):
    scales = load_scales(REPO / "config")
    session, steps = _data(tmp_path)
    persona = Persona(id="p", label="", narrative="", traits=Traits(budget="tight", budget_usd=40))
    q = HeuristicElicitor(scales).answer(persona, session, steps, [113.0, 20.0], "neutral", [])
    assert {c.screen_id for c in q.complaints} == {"/cart", "/shipping"}       # one per screen at least
    cart = next(c for c in q.complaints if c.screen_id == "/cart")
    assert cart.category == "slow_response" and "Go to shipping" in cart.what  # restates measured behaviour
    shipping = next(c for c in q.complaints if c.screen_id == "/shipping")
    assert shipping.category == "elicited" and shipping.region and "'" in shipping.what
    assert q.screen_ranking[0]["screen_id"] == "/cart"                         # worst first
    assert q.quit_step["screen_id"] == "/cart"
    ratings = {r["screen_id"]: r for r in q.ratings}
    assert ratings["/cart"]["ease"] < ratings["/shipping"]["ease"]
    assert ratings["/cart"]["ease_anchor"] == scales["ease"]["points"][ratings["/cart"]["ease"]]
    prices = {p["price"]: p["choice"] for p in q.price_choices}
    assert prices[113.0] == "never buy" and prices[20.0] == "buy now"         # over a tight budget vs within it


def test_vague_complaint_detection():
    ok = Complaint("/a", "a", "Continue", {"x": 1, "y": 1, "w": 5, "h": 5}, "The Continue button is grey on white and easy to miss", "", 2, 0)
    assert vague_complaint(ok) is None
    assert vague_complaint(Complaint("/a", "a", "", None, "could be better", "", 1, 0))
    assert "praises" in vague_complaint(Complaint("/a", "a", "page", {"x": 1, "y": 1, "w": 5, "h": 5},
                                                  "Great clean design, love it overall", "", 1, 0))


def test_model_questionnaire_sends_back_vague_answers(settings, tmp_path):
    settings.engine.roles = {"persona": "fake"}
    session, steps = _data(tmp_path)

    def answer(vague):
        complaint = {"screen_index": 0, "element": "" if vague else "Go to shipping", "x": 10, "y": 10, "w": 50,
                     "h": 20, "what": "meh" if vague else "Clicking 'Go to shipping' froze the page for six seconds",
                     "why": "slow", "severity": 2}
        second = dict(complaint, screen_index=1, element="Continue", what="The 'Continue' link sits far from the options")
        return {"price_choices": [{"price": 113, "choice": "come back later", "reason": "slow"}],
                "quit_step": {"screen_index": 0, "reason": "frozen"}, "screen_ranking": [0, 1],
                "ratings": [{"screen_index": 0, "ease": 2, "clarity": 3}], "complaints": [complaint, second]}

    replies = iter([answer(True), answer(False)])
    fake = FakeLLM(lambda r: next(replies))
    q = asyncio.run(ModelElicitor(LLMGateway(settings, clients={"fake": fake}), load_scales(REPO / "config"))
                    .answer(Persona(id="p", label="", narrative="x", traits=Traits()), session, steps, [113], "competitor", "BrandX"))
    assert len(fake.calls) == 2 and "not specific enough" in fake.calls[1].prompt
    assert "BrandX" in fake.calls[0].prompt and "what do you think" not in fake.calls[0].prompt.lower()
    assert q.rounds == 2 and all(vague_complaint(c) is None for c in q.complaints)
    assert len(fake.calls[0].images) == 2
