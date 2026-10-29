"""Ordinary website patterns: pop-ups, cookie banners, menus, buried pages."""

from proofloop.web.engines.heuristic import goal_keywords, page_heading, target_keywords
from proofloop.web.runner.memory import SessionMemory
from proofloop.web.runner.session import navigation_issues
from proofloop.web.runner.types import Action, Signals, StepRecord, ThinkAloud
from proofloop.web.study import TaskSpec

from .test_heuristic_actor import ctx, decide, screen, tb

POPUP = [tb("Join our newsletter", 400, 300, h=30), tb("Get 10% off your first order.", 400, 350),
         tb("No thanks", 400, 420, fill=True)]
COOKIES = [tb("We use cookies to improve your visit.", 40, 760), tb("Accept all", 600, 760, fill=True)]
FIND = TaskSpec(id="returns", type="find", goal="Find out how many days you have to return an item")


def test_newsletter_popup_is_closed_before_the_cookie_banner_under_it():
    d = decide(ctx(screen(POPUP + COOKIES)))
    assert d.action.target == "No thanks"


def test_popup_that_does_not_close_is_tried_again_then_left_alone():
    mem = SessionMemory(popup_pending="pop-up")
    again = decide(ctx(screen(POPUP + COOKIES), mem=mem, last=Signals(no_change=True)))
    assert again.action.target == "No thanks" and not mem.popup_stuck   # the first click may have missed
    d = decide(ctx(screen(POPUP + COOKIES), mem=mem, last=Signals(no_change=True)))
    assert d.action.target == "Accept all" and "pop-up" in mem.popup_stuck


def test_popup_coming_back_on_a_new_page_is_reported():
    mem = SessionMemory(dismissed_popups={"pop-up"})
    d = decide(ctx(screen(POPUP), mem=mem))
    obs = [o for o in d.observations if o.kind == "popup_interruption"]
    assert obs and "'No thanks'" in obs[0].detail


def test_closing_once_is_not_an_interruption():
    mem = SessionMemory(popup_pending="pop-up")
    d = decide(ctx(screen([tb("Help centre", 40, 120, h=34), tb("Orders & delivery", 40, 200)]), mem=mem,
                   task=FIND, last=Signals(progress=True)))
    assert "pop-up" in mem.dismissed_popups
    assert not [o for o in d.observations if o.kind == "popup_interruption"]


def test_find_task_follows_the_most_specific_link_not_the_menu_or_heading():
    scr = screen([tb("Help", 300, 20, fill=True), tb("Contact", 400, 20, fill=True),
                  tb("Orders & delivery", 40, 110, h=34), tb("Tracking an order", 40, 170),
                  tb("Returns and refunds", 40, 210)])
    d = decide(ctx(scr, task=FIND))
    assert d.action.target == "Returns and refunds"


def test_synonym_inside_a_sentence_is_not_a_link():
    scr = screen([tb("Furniture that lasts", 40, 110, h=34), tb("Solid wood, made to order.", 40, 170),
                  tb("Help", 300, 20, fill=True)])
    assert decide(ctx(scr, task=FIND)).action.target == "Help"


def test_task_keywords_replace_goal_synonyms():
    task = TaskSpec(id="p", goal="Choose the Plus membership plan", keywords=["continue"])
    assert target_keywords(task) == (["continue"], [])
    direct, synonyms = target_keywords(FIND)
    assert "return" in direct and "help" in synonyms
    assert "contact" in goal_keywords("Find the customer support email address")


def test_page_heading_is_the_clearly_largest_body_text():
    items = [tb("Shop", 300, 20, fill=True), tb("Help centre", 40, 110, h=34), tb("Payments", 40, 200),
             tb("Your account", 40, 240)]
    assert page_heading(items).text == "Help centre"
    assert page_heading([tb("a", 40, 110), tb("b", 40, 200)]) is None


def test_a_click_that_does_nothing_is_tried_once_more_before_giving_up_on_it():
    mem = SessionMemory()
    scr = screen([tb("Welcome", 100, 100, h=30), tb("Continue", 300, 400, fill=True)])
    first = decide(ctx(scr, mem=mem))
    again = decide(ctx(scr, mem=mem, last=Signals(no_change=True)))
    assert first.action.target == again.action.target == "Continue"
    third = decide(ctx(scr, mem=mem, last=Signals(no_change=True)))
    assert third.action.target != "Continue"


def _step(idx, page, after):
    return StepRecord(idx=idx, screenshot_path="s.jpg", screen_hash=0, screen_id=page, url="", think=ThinkAloud("", "", "", ""),
                      intended=Action("click", x=1, y=1, target="x"), executed=None, signals=Signals(), frustration=0,
                      sim_seconds=0, observations=[], screen_id_after=after)


def test_navigation_issue_for_buried_information_only():
    deep = [_step(0, "/", "/help"), _step(1, "/help", "/help/orders"), _step(2, "/help/orders", "/help/orders/returns")]
    issue = navigation_issues(deep, "completed")
    assert issue and issue[0].category == "deep_navigation" and issue[0].severity == 2
    assert issue[0].evidence["path"] == ["/", "/help", "/help/orders", "/help/orders/returns"]
    assert navigation_issues(deep[:1], "completed") == []
    assert navigation_issues(deep, "quit")[0].severity == 3


def _with_burger(scr):
    import io

    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (scr.width, scr.height), "white")
    d = ImageDraw.Draw(img)
    for k in range(3):
        d.rectangle([1230, 20 + k * 8, 1254, 22 + k * 8], fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    scr.png = buf.getvalue()
    scr.lum = np.asarray(img.convert("L"), dtype=np.float32) / 255.0
    return scr


def test_find_task_opens_the_three_line_menu_when_nothing_on_the_page_matches():
    from proofloop.web.runner.perception import menu_icons

    scr = _with_burger(screen([tb("Furniture that lasts", 40, 110, h=34), tb("Solid wood.", 40, 170)]))
    assert menu_icons(scr) == [(1242, 29)]
    mem = SessionMemory(screen_sig=scr.signature(), scrolls=1, last_action_kind="scroll")
    d = decide(ctx(scr, task=FIND, mem=mem))
    assert d.action.kind == "click" and d.action.target == "menu" and (d.action.x, d.action.y) == (1242, 29)


def _search_box(placeholder="Search help"):
    from proofloop.web.runner.perception import InputBox

    box = InputBox(x0=40, y0=170, x1=360, y1=214)
    box.contents = [tb(placeholder, 50, 182, contrast=3.0)]
    return box


def test_search_box_is_not_a_form_and_is_used_for_find_tasks():
    texts = [tb("Help centre", 40, 110, h=34), tb("Search", 380, 182, fill=True), tb("Payments", 40, 300)]
    scr = screen(texts, inputs=[_search_box()])
    mem = SessionMemory(screen_sig=scr.signature(), scrolls=1, last_action_kind="scroll")
    task = TaskSpec(id="hours", type="find", goal="Find the warranty length")
    d = decide(ctx(scr, task=task, mem=mem))
    assert d.action.kind == "fill" and d.action.text == "warranty length" and mem.pending_enter
    assert not [o for o in d.observations if o.kind == "ambiguous_label"]
    nxt = decide(ctx(scr, task=task, mem=mem))
    assert nxt.action.kind == "press" and nxt.action.key == "Enter"


def test_a_form_i_typed_nothing_into_is_not_submitted():
    from proofloop.web.runner.perception import InputBox

    box = InputBox(x0=40, y0=170, x1=360, y1=214)   # unlabelled field I have nothing for
    scr = screen([tb("Newsletter", 40, 110, h=34), tb("Pay now", 40, 260, fill=True)], inputs=[box])
    d = decide(ctx(scr, task=TaskSpec(id="t", goal="Read the article", keywords=["read more"])))
    assert d.action.target != "Pay now"
