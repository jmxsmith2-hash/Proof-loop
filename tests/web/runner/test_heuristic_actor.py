"""Decision logic on synthetic screens (no browser, no OCR)."""

import asyncio
import random

from proofloop.personas.models import Persona, Traits
from proofloop.web.engines.base import ActorContext
from proofloop.web.engines.heuristic import HeuristicActor, date_variants, noticed
from proofloop.web.runner.behavior import PatienceBudget
from proofloop.web.runner.memory import SessionMemory
from proofloop.web.runner.perception import InputBox, Screen, TextBox
from proofloop.web.runner.types import Signals
from proofloop.web.study import TaskSpec


def tb(text, x, y, contrast=12.0, fill=False, h=20):
    return TextBox(text=text, x0=x, y0=y, x1=x + 10 * len(text), y1=y + h, conf=0.9, contrast=contrast, on_fill=fill)


def screen(texts, inputs=()):
    return Screen(png=b"", width=1280, height=800, texts=list(texts), inputs=list(inputs), dhash=0, gray_mean=0.9)


def ctx(scr, traits=None, task=None, mem=None, last=None, busy=False, profile=None):
    traits = traits or Traits()
    return ActorContext(
        persona=Persona(id="p", label="p", traits=traits, narrative=""),
        task=task or TaskSpec(id="t", goal="g", keywords=["continue"]),
        screen=scr, memory=mem or SessionMemory(), budget=PatienceBudget(12, 3, 300, 6), step=0,
        frustration=0, frustration_threshold=65, last_signals=last, last_timing=None, browser_busy=busy,
        interrupted=False, framing="neutral", competitor_label="", prompt_notes=[], rng=random.Random(0), seed=1)


def decide(c):
    return asyncio.run(HeuristicActor().decide(c))


def test_clicks_obvious_button():
    d = decide(ctx(screen([tb("Welcome", 100, 100, h=30), tb("Continue", 300, 400, fill=True)])))
    assert d.action.kind == "click" and d.action.target == "Continue"
    assert d.think.sees and d.think.expects and d.think.does and d.think.feels


def test_low_contrast_button_missed_at_glance_and_found_after_search():
    scr = screen([tb("Plans", 100, 100, h=30), tb("Continue", 900, 700, contrast=1.4)])
    mem = SessionMemory()
    first = decide(ctx(scr, mem=mem))
    assert first.action.kind == "scroll"
    # scrolling hits the end of the page, then the persona looks carefully
    look = decide(ctx(scr, mem=mem, last=Signals(scroll_end=True)))
    assert look.action.kind == "wait" and any(o.kind == "cannot_find" for o in look.observations)
    found = decide(ctx(scr, mem=mem, last=Signals()))
    assert found.action.target == "Continue"
    assert any(o.kind == "hidden_control" for o in found.observations)


def test_low_vision_persona_never_sees_ghost_button():
    scr = screen([tb("Plans", 100, 100, h=30), tb("Continue", 900, 700, contrast=1.4)])
    mem, traits = SessionMemory(), Traits(eyesight="low_vision")
    kinds = []
    last = None
    for _ in range(8):
        d = decide(ctx(scr, traits=traits, mem=mem, last=last))
        kinds.append(d.action.kind)
        last = Signals(scroll_end=True) if d.action.kind == "scroll" else Signals()
        if d.action.kind == "quit":
            break
    assert kinds[-1] == "quit" and "click" not in kinds


def test_form_filling_with_ambiguous_label_and_date_habit():
    labels = [tb("Full name", 100, 100), tb("ID", 100, 190), tb("DOB", 100, 280), tb("Create account", 110, 380, fill=True)]
    boxes = [InputBox(100, 125, 500, 165), InputBox(100, 215, 500, 255), InputBox(100, 305, 500, 345)]
    task = TaskSpec(id="s", goal="g", keywords=["create account"],
                    inputs={"full_name": "Sam", "username": "sam85", "birthdate": "1985-03-14"})
    mem = SessionMemory()
    traits = Traits(tech_skill=2, familiarity=2, date_habit="DMY")
    actions = []
    for _ in range(4):
        d = decide(ctx(screen(labels, boxes), traits=traits, task=task, mem=mem))
        actions.append(d)
        if d.action.kind == "fill":  # the typed text now shows in that box
            box = next(b for b in boxes if b.y0 < d.action.y < b.y1)
            box.contents = [tb(d.action.text, box.x0 + 5, box.y0 + 8)]
    assert [a.action.kind for a in actions] == ["fill", "fill", "fill", "click"]
    assert actions[1].action.text == "sam85" and any(o.kind == "ambiguous_label" for o in actions[1].observations)
    assert actions[2].action.text == "14/03/1985"  # the persona's own date habit first
    assert actions[3].action.target == "Create account"


def test_detects_lost_data_and_varies_date_format():
    labels = [tb("Invalid input.", 100, 60), tb("Full name", 100, 100), tb("DOB", 100, 190),
              tb("Create account", 110, 300, fill=True)]
    boxes = [InputBox(100, 125, 500, 165), InputBox(100, 215, 500, 255)]
    task = TaskSpec(id="s", goal="g", inputs={"full_name": "Sam", "birthdate": "1985-03-14"})
    mem = SessionMemory(typed={"fullname": "Sam", "dob": "03/14/1985"},
                        typed_kinds={"fullname": "full_name", "dob": "birthdate"})
    mem.last_action_kind = "click"
    d = decide(ctx(screen(labels, boxes), traits=Traits(tech_skill=4), task=task, mem=mem))
    kinds = {o.kind for o in d.observations}
    assert {"data_lost_on_error", "unexplained_error"} <= kinds
    d2 = decide(ctx(screen(labels, [InputBox(100, 125, 500, 165, [tb("Sam", 105, 135)]), boxes[1]]),
                    traits=Traits(tech_skill=4), task=task, mem=mem))
    assert d2.action.text == "1985-03-14"


def test_waits_while_browser_loads_unless_impatient():
    scr = screen([tb("Go to shipping", 300, 300, fill=True)])
    mem = SessionMemory(clicks=[{"x": 350, "y": 310, "text": "Go to shipping", "sig": ""}])
    patient = decide(ctx(scr, traits=Traits(patience=4), mem=mem, last=Signals(no_change=True), busy=True))
    assert patient.action.kind == "wait"
    mem2 = SessionMemory(clicks=[{"x": 350, "y": 310, "text": "Go to shipping", "sig": ""}])
    hasty = decide(ctx(scr, traits=Traits(patience=1), mem=mem2, last=Signals(no_change=True), busy=True))
    assert hasty.action.kind == "click"


def test_unexplained_error_without_form_retries_then_quits():
    scr = screen([tb("Error 0x3F", 100, 100), tb("Place order", 100, 200, fill=True)])
    task = TaskSpec(id="o", goal="g", keywords=["place order"])
    mem = SessionMemory()
    d = decide(ctx(scr, task=task, mem=mem, last=Signals(progress=True)))
    assert d.action.target == "Place order" and any(o.kind == "unexplained_error" for o in d.observations)


def test_date_variants():
    assert date_variants("1985-03-14", "MDY", 4)[:2] == ["03/14/1985", "1985-03-14"]
    assert date_variants("1985-03-14", "YMD", 1)[0] == "1985-03-14"


def test_noticed_depends_on_eyesight():
    scr = screen([tb("faint", 0, 0, contrast=2.5), tb("clear", 0, 40, contrast=9)])
    assert len(noticed(scr, Traits(), careful=False)) == 2
    assert [t.text for t in noticed(scr, Traits(eyesight="low_vision"), careful=False)] == ["clear"]
