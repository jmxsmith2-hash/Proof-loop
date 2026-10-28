"""The free, built-in persona engine: rule-based behaviour on top of what OCR can see.

It does not call any model. Each persona's traits shape what it notices
(contrast and size thresholds), how it fills forms (date habits, typos),
how long it searches and when it gives up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from synthux.core.study import TaskSpec
from synthux.engines.base import ActorContext
from synthux.personas.models import Traits
from synthux.runner.observe import explained, visible_errors
from synthux.runner.perception import (InputBox, Screen, TextBox, checkbox_left_of, fuzzy_contains, menu_icons,
                                       normalize)
from synthux.runner.types import Action, Decision, Observation, ThinkAloud

# --------------------------------------------------------------- lexicons

FIELD_LEXICON: dict[str, list[str]] = {
    "full_name": ["full name", "your name", "name"],
    "first_name": ["first name", "given name"],
    "last_name": ["last name", "surname", "family name"],
    "email": ["email address", "e-mail", "email"],
    "username": ["username", "user name", "login name", "screen name"],
    "password": ["password", "passcode"],
    "birthdate": ["date of birth", "birth date", "birthday", "dob", "born on"],
    "phone": ["phone number", "telephone", "mobile number", "phone"],
    "address": ["street address", "address"],
    "city": ["city", "town"],
    "zip": ["postcode", "postal code", "zip code", "zip"],
    "search": ["search"],
}
JARGON_LABELS = {"dob": "birthdate"}           # understood only by experienced personas
AMBIGUOUS_GUESSES = {"id": ["username", "email", "phone"], "dob": ["birthdate"], "number": ["phone"], "code": ["zip"],
                     "reference": ["username"], "handle": ["username"]}
SUBMIT_WORDS = ["create account", "sign up", "register", "submit", "log in", "login", "sign in", "continue",
                "next", "save", "confirm", "place order", "pay", "send", "search", "done", "finish"]
PROGRESS_WORDS = ["continue", "next", "proceed", "confirm", "done"]
CHECKBOX_KEYS = {"agree", "agree_terms", "accept_terms", "terms", "consent", "accept"}
CHECKBOX_WORDS = ("i agree", "i accept", "terms", "conditions", "privacy policy", "consent")
TRUTHY = {"yes", "true", "1", "y", "tick", "check"}
COOKIE_WORDS = ("cookie", "cookies", "consent", "we value your privacy")
COOKIE_ACCEPT = ["accept all", "accept", "allow all", "agree", "i agree", "got it", "ok", "okay", "i understand"]
COOKIE_REJECT = ["reject all", "necessary only", "only necessary", "decline", "reject"]
MODAL_WORDS = ("newsletter", "subscribe", "sign up for", "% off", "join our", "get 10", "get 15", "exclusive offer",
               "don't miss", "special offer")
MODAL_CLOSE = ["no thanks", "no, thanks", "not now", "maybe later", "close", "dismiss", "skip"]
CLOSE_GLYPHS = {"x", "×", "✕", "✖"}
NAV_SYNONYMS = {
    "help": ["help", "support", "faq", "faqs", "customer service", "contact"],
    "return": ["returns", "return policy", "refunds", "help", "faq", "orders", "orders delivery"],
    "refund": ["refunds", "returns", "help"],
    "contact": ["contact", "contact us", "support", "help"],
    "support": ["support", "contact", "contact us", "help", "customer service"],
    "customer": ["customer service", "contact", "help"],
    "email": ["contact", "contact us"],
    "phone": ["contact", "contact us"],
    "hours": ["contact", "opening hours", "stores", "about", "visit"],
    "account": ["account", "my account", "profile", "log in", "sign in"],
    "order": ["orders", "my orders", "account", "track"],
    "price": ["pricing", "plans", "shop"],
    "shipping": ["shipping", "delivery", "help", "faq"],
    "delivery": ["delivery", "shipping", "help", "faq"],
}
GOAL_STOPWORDS = frozenset("""find the a an your you to of for and in on at with from out what where when how is are
    get go see check look page site which this that its it my me""".split())
QUIT_LINES = {
    "cannot_find": "I give up: I looked over the whole page and couldn't find a way to continue.",
    "form": "I give up: I've filled this form in {n} times and it keeps rejecting it without saying why.",
    "error": "I give up: it shows an error I don't understand and retrying doesn't help.",
    "frustration": "That's it, I'm leaving. {cause}",
    "slow": "I'm not waiting any longer; nothing is happening.",
}


@dataclass
class FieldView:
    box: InputBox
    label: TextBox | None
    label_text: str
    kind: str | None          # None = the persona can't tell what this field wants
    ambiguous: bool
    jargon: bool
    placeholder: bool = False  # the only label is grey text inside the box
    select: bool = False       # a dropdown showing its default ("Select a country")

    @property
    def empty(self) -> bool:
        return self.box.empty or self.placeholder or self.select


# ----------------------------------------------------------- perception


DEFAULT_PERCEPTION = {"glance_contrast": {"normal": 2.2, "color_blind": 2.3, "low_vision": 3.6},
                      "careful_contrast": {"normal": 1.3, "color_blind": 1.35, "low_vision": 2.4}}


def glance_threshold(t: Traits, perception: dict | None = None) -> float:
    base = (perception or DEFAULT_PERCEPTION)["glance_contrast"][t.eyesight]
    base += {"scanner": 0.4, "methodical": -0.2, "explorer": 0.0}[t.style]
    if t.time_pressure or t.mood != "calm":
        base += 0.3
    return base


def careful_threshold(t: Traits, perception: dict | None = None) -> float:
    return (perception or DEFAULT_PERCEPTION)["careful_contrast"][t.eyesight]


def noticed(screen: Screen, traits: Traits, careful: bool, perception: dict | None = None) -> list[TextBox]:
    """Text a person with these traits would notice on this screen."""
    min_contrast = careful_threshold(traits, perception) if careful else glance_threshold(traits, perception)
    min_height = 12 if traits.eyesight == "low_vision" else 9
    return [t for t in screen.texts if t.salience >= min_contrast and t.height >= min_height and t.conf >= 0.5]


def classify_label(text: str, traits: Traits) -> tuple[str | None, bool, bool]:
    """Return (field kind, ambiguous, jargon)."""
    norm = normalize(text).rstrip(" *:")
    if norm in JARGON_LABELS:
        knows = traits.tech_skill >= 3 or traits.familiarity >= 3
        return (JARGON_LABELS[norm] if knows else None), not knows, True
    for kind, phrases in FIELD_LEXICON.items():
        for phrase in phrases:
            if norm == phrase or (len(phrase) > 4 and fuzzy_contains(norm, phrase, 0.88)):
                return kind, False, False
    return None, True, False


def label_for(box: InputBox, texts: list[TextBox]) -> TextBox | None:
    above = [t for t in texts if t.y1 <= box.y0 + 2 and box.y0 - t.y1 <= 45
             and t.x0 < box.x1 and t.x1 > box.x0 - 10]
    if above:
        return max(above, key=lambda t: t.y1)
    left = [t for t in texts if t.x1 <= box.x0 + 2 and box.x0 - t.x1 <= 200
            and abs(t.center[1] - box.center[1]) <= 18]
    return max(left, key=lambda t: t.x1) if left else None


SEARCH_WORDS = ("search", "what are you looking for", "find products")
SELECT_PROMPTS = ("select", "choose", "please select", "please choose", "--", "pick")


def _match_input_key(text: str, inputs: dict[str, str]) -> str | None:
    """Task inputs can be named after the label itself (e.g. country: Germany)."""
    norm = normalize(text).rstrip(" *:")
    for key in inputs:
        name = key.replace("_", " ").lower()
        if norm == name or fuzzy_contains(norm, name, 0.88) or fuzzy_contains(name, norm, 0.88):
            return key
    return None


def form_fields(screen: Screen, traits: Traits, inputs: dict[str, str] | None = None) -> list[FieldView]:
    inputs = inputs or {}
    fields = []
    for box in sorted(screen.inputs, key=lambda b: (b.y0, b.x0)):
        label = label_for(box, screen.texts)
        text = label.text if label else ""
        inner = box.text.strip()
        placeholder = select = False
        if inner and normalize(inner).startswith(SELECT_PROMPTS):
            select = True
            text = text or inner
        elif not text and inner and all(t.contrast < 4.0 for t in box.contents):
            placeholder, text, label = True, inner, box.contents[0]    # grey hint text used as the label
        kind, ambiguous, jargon = classify_label(text, traits) if text else (None, True, False)
        if any(w in normalize(t) for t in (text, inner) if t for w in SEARCH_WORDS):
            kind, ambiguous, jargon = "search", False, False   # the site's search box, not part of a form
        if kind is None and text:
            matched = _match_input_key(text, inputs)
            if matched:
                kind, ambiguous = matched, False
        fields.append(FieldView(box, label, text, kind, ambiguous, jargon, placeholder, select))
    return fields


# ---------------------------------------------------------------- values

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def date_variants(iso: str, habit: str, skill: int) -> list[str]:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if not m:
        return [iso]
    y, mo, d = m.groups()
    forms = {"MDY": f"{mo}/{d}/{y}", "DMY": f"{d}/{mo}/{y}", "YMD": f"{y}-{mo}-{d}",
             "LONG": f"{MONTHS[int(mo) - 1]} {int(d)}, {y}", "DOTS": f"{d}.{mo}.{y}"}
    order = [habit]
    order += ["YMD", "DMY", "MDY", "LONG", "DOTS"] if skill >= 3 else ["MDY", "DMY", "LONG", "DOTS", "YMD"]
    seen, out = set(), []
    for key in order:
        if forms[key] not in seen:
            seen.add(forms[key])
            out.append(forms[key])
    return out


def value_options(kind: str, inputs: dict[str, str], traits: Traits) -> list[str]:
    if kind == "birthdate" and "birthdate" in inputs:
        return date_variants(inputs["birthdate"], traits.date_habit, traits.tech_skill)
    if kind == "full_name" and "full_name" not in inputs and {"first_name", "last_name"} <= set(inputs):
        return [f"{inputs['first_name']} {inputs['last_name']}"]
    return [inputs[kind]] if kind in inputs else []


# -------------------------------------------------------------- think-aloud


def _feel(ctx: ActorContext, confusion: float) -> str:
    ratio = ctx.frustration / max(1.0, ctx.frustration_threshold)
    style = ctx.persona.traits.style
    if ratio < 0.2 and confusion < 0.3:
        base = {"methodical": "Fine so far.", "scanner": "OK, moving on.", "explorer": "Seems straightforward."}[style]
    elif ratio < 0.45:
        base = {"methodical": "A little uneasy.", "scanner": "Getting a bit impatient.", "explorer": "Hmm, not obvious."}[style]
    elif ratio < 0.75:
        base = {"methodical": "Annoyed; this should be simpler.", "scanner": "Irritated, this is wasting my time.",
                "explorer": "Frustrated, I keep hitting walls."}[style]
    else:
        base = "Very frustrated and close to giving up."
    if confusion >= 0.5:
        base += " I'm not sure what this screen wants from me."
    if ctx.persona.traits.time_pressure and ratio >= 0.2:
        base += " I don't have time for this."
    return base


def _sees(ctx: ActorContext, items: list[TextBox]) -> str:
    salient = sorted(items, key=lambda t: (-t.height, t.y0))[:6]
    shown = ", ".join(f"'{t.text[:40]}'" for t in sorted(salient, key=lambda t: (t.y0, t.x0)))
    return f"I see {shown}." if shown else "I don't see anything I can use."


def _decision(ctx: ActorContext, items: list[TextBox], action: Action, expects: str, does: str,
              confusion: float = 0.0, observations: list[Observation] | None = None,
              quit_reason: str | None = None) -> Decision:
    think = ThinkAloud(sees=_sees(ctx, items), expects=expects, does=does, feels=_feel(ctx, confusion))
    return Decision(action=action, think=think, confusion=confusion,
                    observations=observations or [], quit_reason=quit_reason)


# ------------------------------------------------------------------- actor


class HeuristicActor:
    """Rule-based persona. Deterministic for a given seed."""

    async def decide(self, ctx: ActorContext) -> Decision:
        mem = ctx.memory
        self._learn_from_last_step(ctx)
        decision = self._decide(ctx)
        mem.last_action_kind = decision.action.kind
        if decision.action.kind == "click" and decision.action.x is not None:
            mem.clicks.append({"x": decision.action.x, "y": decision.action.y, "text": decision.action.target,
                               "sig": mem.screen_sig})
        return decision

    def _learn_from_last_step(self, ctx: ActorContext) -> None:
        mem, screen, last = ctx.memory, ctx.screen, ctx.last_signals
        sig = screen.signature()
        if mem.target_pending:
            # Remember where the way forward was only once clicking it actually moved me on.
            if mem.last_action_kind == "click" and last and not last.no_change:
                mem.commit_target()
            mem.target_pending = None
        if mem.last_action_kind not in ("scroll", "wait") and sig != mem.screen_sig:
            # A form that disappears without an error was accepted: remember what worked.
            if mem.typed and not screen.inputs and not visible_errors(screen):
                for label, kind in mem.typed_kinds.items():
                    if kind != "password":
                        mem.known_formats[kind] = mem.typed[label]
                    if normalize(label) in AMBIGUOUS_GUESSES or normalize(label) in JARGON_LABELS:
                        mem.label_meanings[normalize(label)] = kind
                mem.remember("the form was accepted with " + ", ".join(
                    f"{k}={mem.typed[l]!r}" for l, k in mem.typed_kinds.items() if k != "password"))
                mem.typed.clear()
                mem.typed_kinds.clear()
                mem.form_attempts = 0
            mem.new_screen(sig)
        if last and last.no_change and not ctx.browser_busy and mem.clicks and mem.last_action_kind == "click":
            key = (mem.screen_sig, normalize(mem.clicks[-1]["text"] or ""))
            mem.followed.discard(key)  # it went nowhere, so it was not "followed"
            # People click once more before deciding something is broken (maybe they missed it).
            if key in mem.missed_once or ctx.persona.traits.patience <= 1:
                mem.dead_targets.add(key)
            else:
                mem.missed_once.add(key)

    def _decide(self, ctx: ActorContext) -> Decision:
        screen, mem, t = ctx.screen, ctx.memory, ctx.persona.traits
        items = noticed(screen, t, mem.careful, ctx.perception)
        observations: list[Observation] = []
        if ctx.interrupted:
            observations.append(Observation("interruption", "came back after an interruption"))

        # 1. The screen did not react yet but the browser shows it is loading.
        waiting = self._handle_waiting(ctx, items)
        if waiting:
            return waiting
        # 1b. Cookie banners and pop-ups get dismissed the way most people do.
        popup = self._handle_popups(ctx, items, observations)
        if popup:
            return popup
        # 2. Errors on screen.
        error_decision = self._handle_errors(ctx, items, observations)
        if error_decision:
            return error_decision
        # 3. Forms.
        if mem.pending_enter:
            mem.pending_enter = False
            return _decision(ctx, items, Action("press", key="Enter"), "Enter should run the search.",
                             "I press Enter to search.", 0.1, observations)
        all_fields = form_fields(screen, t, ctx.task.inputs)
        self._search_boxes = [f for f in all_fields if f.kind == "search"]
        fields = [f for f in all_fields if f.kind != "search"]
        if fields:
            form_decision = self._handle_form(ctx, items, fields, observations)
            if form_decision:
                return form_decision
        # 4. Choices the task asks for (e.g. pick the "Plus" plan).
        choice = self._handle_choice(ctx, items, observations)
        if choice:
            return choice
        # 5. A visible control that moves the task forward.
        target = self._best_target(ctx, items, fields)
        if target:
            if not fields:
                mem.followed.add((mem.screen_sig, target.norm))
            return self._click(ctx, items, target, observations)
        # 6. Something remembered from a previous visit.
        remembered = mem.known_targets.get(mem.screen_sig)
        if remembered and (mem.screen_sig, normalize(remembered["text"] or "")) in mem.dead_targets:
            remembered = None  # it doesn't work any more
        if remembered and mem.visits > 0:
            if mem.scrolls < remembered.get("scrolls", 0):
                mem.scrolls += 1
                return _decision(ctx, items, Action("scroll", dy=remembered.get("dy", 500)),
                                 f"Last time '{remembered['text']}' was further down.",
                                 "I scroll down to where I remember it.", 0.1, observations)
            observations.append(Observation("remembered", f"remembered '{remembered['text']}' from last time"))
            return _decision(ctx, items, Action("click", x=remembered["x"], y=remembered["y"],
                                                target=remembered["text"]),
                             f"Last time '{remembered['text']}' was here.",
                             f"I click where '{remembered['text']}' was last time.", 0.1, observations)
        # 7. Search: scroll, look carefully, then give up.
        return self._search(ctx, items, observations)

    # ---------------------------------------------------------------- parts
    def _handle_waiting(self, ctx: ActorContext, items: list[TextBox]) -> Decision | None:
        sig, mem = ctx.last_signals, ctx.memory
        if not (sig and sig.no_change and ctx.browser_busy):
            return None
        mem.waits += 1
        t = ctx.persona.traits
        if mem.waits > {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}[t.patience]:
            return _decision(ctx, items, Action("quit"), "Nothing will happen.", "I leave.",
                             0.4, quit_reason=QUIT_LINES["slow"])
        if t.patience <= 2 and mem.waits == 1 and mem.clicks:
            last = mem.clicks[-1]
            return _decision(ctx, items, Action("click", x=last["x"], y=last["y"], target=last["text"]),
                             "Maybe my click didn't register.", f"I click '{last['text']}' again.", 0.3)
        return _decision(ctx, items, Action("wait"), "The page should load any second.",
                         "I wait; the browser says it is still loading.", 0.2)

    def _handle_popups(self, ctx: ActorContext, items: list[TextBox], observations: list[Observation]) -> Decision | None:
        mem, text = ctx.memory, ctx.screen.norm_text
        goal = normalize(ctx.task.goal + " " + " ".join(ctx.task.keywords))
        present: dict[str, TextBox | None] = {}
        # A centred pop-up sits on top of a cookie banner, so a person deals with it first.
        if any(w in text for w in MODAL_WORDS) and not any(w in goal for w in ("subscribe", "newsletter")):
            present["pop-up"] = self._short_button(items, MODAL_CLOSE) or self._close_glyph(ctx.screen, items)
        if any(w in text for w in COOKIE_WORDS):
            prefer = COOKIE_REJECT + COOKIE_ACCEPT if (ctx.persona.traits.tech_skill >= 4
                                                       and ctx.persona.traits.style == "methodical") else COOKIE_ACCEPT
            present["cookie banner"] = self._short_button(items, prefer)
        # Settle last step's close: gone means dismissed; still there with nothing changed means stuck.
        if mem.popup_pending:
            kind = mem.popup_pending
            mem.popup_pending = ""
            if kind not in present or (ctx.last_signals and not ctx.last_signals.no_change):
                mem.dismissed_popups.add(kind)
            elif ctx.last_signals and ctx.last_signals.no_change:
                mem.popup_stuck.add(kind)
        for kind, button in present.items():
            if button is None or kind in mem.popup_stuck:
                continue
            if kind in mem.dismissed_popups:
                observations.append(Observation(
                    "popup_interruption", f"the {kind} came back after I had already closed it with '{button.text}'",
                    button.region(30), 2))
                mem.dismissed_popups.discard(kind)
            mem.popup_pending = kind
            x, y = button.center
            return _decision(ctx, items, Action("click", x=x, y=y, target=button.text),
                             f"Closing the {kind} gets it out of my way.", f"I click '{button.text}' on the {kind}.",
                             0.15, observations)
        return None

    @staticmethod
    def _short_button(items: list[TextBox], words: list[str]) -> TextBox | None:
        for word in words:
            for t in items:
                if len(t.norm) <= len(word) + 8 and (t.norm == word or fuzzy_contains(t.norm, word, 0.9)):
                    return t
        return None

    @staticmethod
    def _close_glyph(screen: Screen, items: list[TextBox]) -> TextBox | None:
        glyphs = [t for t in screen.texts if t.text.strip().lower() in CLOSE_GLYPHS and t.height <= 40]
        return min(glyphs, key=lambda t: (t.y0, -t.x0)) if glyphs else None

    def _handle_errors(self, ctx: ActorContext, items: list[TextBox], observations: list[Observation]) -> Decision | None:
        errors = [e for e in visible_errors(ctx.screen) if e in items or e.contrast >= 2.0]
        mem = ctx.memory
        if not errors:
            mem.last_error = ""
            return None
        error = errors[0]
        fresh = error.norm != mem.last_error or (ctx.last_signals and ctx.last_signals.progress)
        if not fresh:
            return None
        mem.last_error = error.norm
        is_explained = explained(error, ctx.screen)
        if not is_explained:
            observations.append(Observation(
                "unexplained_error", f"error '{error.text}' gives no cause or next step", error.region(), 3))
        has_form = bool(ctx.screen.inputs)
        if has_form:
            return None  # the form handler decides what to change; observations carry the error
        mem.retries_same += 1
        if mem.retries_same > ctx.budget.max_retries:
            return _decision(ctx, items, Action("quit"), "Retrying won't help.", "I give up.",
                             0.8, observations, QUIT_LINES["error"])
        last = mem.clicks[-1] if mem.clicks else None
        retry_target = self._best_target(ctx, items, [])
        if retry_target is None and last is None:
            return None
        if retry_target is not None:
            x, y = retry_target.center
            label = retry_target.text
        else:
            x, y, label = last["x"], last["y"], last["text"]
        confusion = 0.8 if not is_explained else 0.3
        mem.remember(f"'{label}' failed once with '{error.text}'")
        return _decision(ctx, items, Action("click", x=x, y=y, target=label),
                         "Maybe it was a temporary glitch." if not is_explained else "I'll follow what it says.",
                         f"I try '{label}' again.", confusion, observations)

    def _handle_form(self, ctx: ActorContext, items: list[TextBox], fields: list[FieldView],
                     observations: list[Observation]) -> Decision | None:
        mem, t, inputs = ctx.memory, ctx.persona.traits, ctx.task.inputs
        error_now = any(o.kind == "unexplained_error" for o in observations) or bool(
            visible_errors(ctx.screen) and mem.typed and all(f.empty for f in fields))
        # Data the persona typed is gone after a failed submit.
        labels = {_field_key(f) for f in fields}
        if mem.typed and not set(mem.typed) <= labels:
            mem.typed.clear()  # a different form
            mem.typed_kinds.clear()
        if mem.typed and all(f.empty for f in fields):
            if error_now or mem.form_attempts > 0:
                observations.append(Observation(
                    "data_lost_on_error", f"all {len(mem.typed)} fields I filled are empty again",
                    _union_region([f.box for f in fields]), 3))
                mem.form_attempts += 1
                mem.remember(f"the form rejected my details {mem.form_attempts} time(s)")
                if mem.form_attempts > ctx.budget.max_retries:
                    return _decision(ctx, items, Action("quit"), "It will just reject me again.", "I give up.",
                                     0.8, observations, QUIT_LINES["form"].format(n=mem.form_attempts))
                self._vary_uncertain(mem, fields, t)
            mem.typed.clear()
            mem.typed_kinds.clear()

        typed_values = {normalize(v) for v in mem.typed.values()}
        for f in fields:
            key = _field_key(f)
            if key in mem.typed:
                if f.empty and mem.typed_kinds.get(key) not in ("password", None) and len(fields) > 1:
                    del mem.typed[key]  # what I typed isn't there (missed the field); type it again
                    mem.typed_kinds.pop(key, None)
                    confusion_note = Observation("missed_field", f"my text didn't appear in '{key}'")
                    observations.append(confusion_note)
                else:
                    continue
            if not f.empty and f.kind != "password":
                if normalize(f.box.text) in typed_values:
                    pass  # I typed into the wrong field by mistake; retype the right value here
                else:
                    mem.typed[key] = f.box.text  # already filled (e.g. browser autofill)
                    continue
            kind, confusion = f.kind, 0.0
            if kind is None:
                guesses = AMBIGUOUS_GUESSES.get(normalize(f.label_text), ["username"])
                kind = mem.label_meanings.get(normalize(f.label_text)) or self._guess(guesses, mem, key)
                confusion = 0.6 if t.tech_skill <= 3 else 0.45
                observations.append(Observation(
                    "ambiguous_label", f"I don't know what '{f.label_text or 'this field'}' means; guessing {kind}",
                    (f.label.region() if f.label else f.box.region()), 2))
            options = value_options(kind, inputs, t)
            if not options:
                continue
            idx = min(mem.variant_index.get(key, 0), len(options) - 1)
            if kind in mem.known_formats:
                value = mem.known_formats[kind]
            else:
                value = options[idx]
            if kind == "birthdate" and idx > 0:
                observations.append(Observation(
                    "unclear_input_format", f"trying date format '{value}' because the expected format isn't shown",
                    f.box.region(), 3))
                confusion = max(confusion, 0.5)
            mem.typed[key] = value
            mem.typed_kinds[key] = kind
            x, y = f.box.center
            label = f.label_text or "the next field"
            return _decision(ctx, items, Action("fill", x=x, y=y, text=value, target=label, field_kind=kind),
                             f"Typing in '{label}' should fill it in.",
                             (f"I open '{label}' and pick {value!r}." if f.select else
                              f"I click '{label}' and type {'my password' if kind == 'password' else repr(value)}."),
                             confusion, observations)

        tick = self._checkbox(ctx, items)
        if tick is not None:
            return tick
        if not mem.typed:
            return None  # nothing of mine is in this form, so it isn't mine to send
        submit = self._submit_button(ctx, items, fields)
        if submit is None:
            return None
        mem.remember(f"submitted the form with {', '.join(mem.typed_kinds.values())}")
        x, y = submit.center
        return _decision(ctx, items, Action("click", x=x, y=y, target=submit.text),
                         f"'{submit.text}' should send the form.", f"I click '{submit.text}'.",
                         0.2 if mem.form_attempts else 0.0, observations)

    def _checkbox(self, ctx: ActorContext, items: list[TextBox]) -> Decision | None:
        """Tick 'I agree to the terms' (and similar) when the task says so."""
        wants = [k for k, v in ctx.task.inputs.items() if k in CHECKBOX_KEYS and str(v).lower() in TRUTHY]
        if not wants:
            return None
        mem = ctx.memory
        for t in items:
            if not any(w in t.norm for w in CHECKBOX_WORDS) or t.norm in mem.ticked:
                continue
            box = checkbox_left_of(ctx.screen, t)
            mem.ticked.add(t.norm)
            if box is not None and box[2]:
                continue  # already ticked
            x, y = (box[0], box[1]) if box else (t.x0 + 6, t.center[1])
            return _decision(ctx, items, Action("click", x=x, y=y, target=t.text),
                             "The box has to be ticked before I can continue.", f"I tick '{t.text}'.", 0.1)
        return None

    @staticmethod
    def _guess(guesses: list[str], mem, key: str) -> str:
        idx = mem.variant_index.get(key + "#kind", 0)
        return guesses[min(idx, len(guesses) - 1)]

    @staticmethod
    def _vary_uncertain(mem, fields: list[FieldView], t: Traits) -> None:
        """After a rejected form, change one field the persona is unsure about."""
        # Format-sensitive fields (dates, phone numbers) are the usual suspects; then ambiguous labels.
        ranked = sorted(fields, key=lambda f: (f.kind not in ("birthdate", "phone"), not f.ambiguous))
        uncertain = [_field_key(f) for f in ranked
                     if f.ambiguous or f.kind in ("birthdate", "phone")]
        if not uncertain:
            return
        key = uncertain[mem.vary_cursor % len(uncertain)]
        mem.vary_cursor += 1
        field = next(f for f in fields if (_field_key(f)) == key)
        if field.ambiguous and field.kind is None:
            mem.variant_index[key + "#kind"] = mem.variant_index.get(key + "#kind", 0) + 1
        else:
            mem.variant_index[key] = mem.variant_index.get(key, 0) + 1

    def _submit_button(self, ctx: ActorContext, items: list[TextBox], fields: list[FieldView]) -> TextBox | None:
        bottom = max(f.box.y1 for f in fields)
        words = [normalize(k) for k in ctx.task.keywords] + SUBMIT_WORDS
        candidates = [t for t in items if t.y0 >= bottom - 5 and any(fuzzy_contains(t.norm, w, 0.85) for w in words)]
        if not candidates:
            return None
        return min(candidates, key=lambda t: (not t.on_fill, t.y0))

    def _handle_choice(self, ctx: ActorContext, items: list[TextBox], observations: list[Observation]) -> Decision | None:
        choice = ctx.task.inputs.get("choose")
        mem = ctx.memory
        if not choice or choice in mem.choices_done:
            return None
        hits = [t for t in items if fuzzy_contains(t.norm, normalize(choice), 0.9) and len(t.norm) <= len(choice) + 12]
        if not hits:
            return None
        target = min(hits, key=lambda t: (-t.height, t.y0))
        mem.choices_done.add(choice)
        mem.remember(f"chose {choice}")
        x, y = target.center
        return _decision(ctx, items, Action("click", x=x, y=y, target=target.text),
                         f"Selecting '{choice}' should mark it as my choice.", f"I pick '{target.text}'.",
                         0.0, observations)

    def _best_target(self, ctx: ActorContext, items: list[TextBox], fields: list[FieldView]) -> TextBox | None:
        mem = ctx.memory
        direct, synonyms = target_keywords(ctx.task)
        weighted = [(kw, 1.0 - i * 0.03) for i, kw in enumerate(direct)] + \
                   [(kw, 0.7 - i * 0.01) for i, kw in enumerate(synonyms)]
        heading = page_heading(items)
        scored: list[tuple[float, TextBox]] = []
        for t in items:
            if (mem.screen_sig, t.norm) in mem.dead_targets:
                continue
            if not fields and (mem.screen_sig, t.norm) in mem.followed:
                continue  # followed it from here already (it led back here, or nowhere useful)
            if len(t.norm) > 40 or t is heading:
                continue  # paragraphs and the page's own title are not controls
            header = t.y0 < 70 and t.height < 28
            if header and heading is not None and fuzzy_contains(heading.norm, t.norm, 0.9):
                continue  # the menu link to the page I am already on
            matches = []
            words = len(t.norm.split())
            for kw, weight in weighted:
                if t.norm == kw:
                    matches.append(3.0 * weight)
                elif weight < 0.75 and words > len(kw.split()) + 2:
                    continue  # a loose synonym inside a sentence ("made to order") is not a link to it
                elif fuzzy_contains(t.norm, kw, 0.86):
                    matches.append(2.2 * weight)
            score = max(matches, default=0.0) + 0.15 * (len(matches) - 1 if matches else 0)
            if score == 0.0 and any(t.norm == w or fuzzy_contains(t.norm, w, 0.9) for w in PROGRESS_WORDS):
                score = 1.2
            if score == 0.0:
                continue
            if t.on_fill and not header:
                score += 0.6       # looks like a button
            if header:
                score -= 0.8       # small header/navigation link
            scored.append((score, t))
        if not scored:
            return None
        return max(scored, key=lambda st: (st[0], st[1].y0))[1]

    def _click(self, ctx: ActorContext, items: list[TextBox], target: TextBox,
               observations: list[Observation]) -> Decision:
        mem = ctx.memory
        x, y = target.center
        confusion = 0.0
        if target.contrast < 3.0 and mem.careful:
            observations.append(Observation(
                "hidden_control",
                f"'{target.text}' was very hard to see ({target.contrast}:1 contrast"
                + (", and it doesn't look like a button" if not target.on_fill else "")
                + f"); I only found it after {mem.scrolls} scroll(s) and a careful look",
                target.region(8), 3))
            confusion = 0.5
        mem.target_pending = (mem.screen_sig, {"text": target.text, "x": x, "y": y, "scrolls": mem.scrolls,
                                               "dy": mem.last_scroll_dy})
        expects = f"'{target.text}' should take me to the next step."
        if ctx.framing == "competitor":
            expects = f"On {ctx.competitor_label}, '{target.text}' should take me to the next step."
        return _decision(ctx, items, Action("click", x=x, y=y, target=target.text), expects,
                         f"I click '{target.text}'.", confusion, observations)

    def _explore(self, ctx: ActorContext, items: list[TextBox]) -> Decision | None:
        """No direct way forward here: try a menu or footer link that relates to the goal."""
        mem = ctx.memory
        wanted = goal_keywords(ctx.task.goal) + [normalize(k) for k in ctx.task.keywords]
        tried = mem.nav_tried.setdefault(mem.screen_sig, set())
        best, best_score = None, 0.0
        for t in items:
            if len(t.norm.split()) > 4 or t.norm in tried or len(t.norm) < 3:
                continue
            score = max((1.0 if t.norm == w else 0.8 if fuzzy_contains(t.norm, w, 0.88) else 0.0) for w in wanted) \
                if wanted else 0.0
            if score > best_score:
                best, best_score = t, score
        if best is not None and best_score >= 0.8:
            tried.add(best.norm)
            mem.nav_depth += 1
            x, y = best.center
            return _decision(ctx, items, Action("click", x=x, y=y, target=best.text),
                             f"'{best.text}' sounds like where this would be.", f"I try '{best.text}'.", 0.4)
        # Phones hide the menu behind a three-line icon.
        icons = menu_icons(ctx.screen)
        if icons and mem.screen_sig not in mem.menus_opened:
            mem.menus_opened.add(mem.screen_sig)
            x, y = icons[0]
            return _decision(ctx, items, Action("click", x=x, y=y, target="menu"),
                             "The menu should list the sections of the site.", "I open the menu (the three lines).",
                             0.3)
        # The site's own search box.
        boxes = getattr(self, "_search_boxes", [])
        if boxes and mem.searches < 2:
            direct, _ = target_keywords(ctx.task)
            query = " ".join(w for w in direct if len(w) > 2 and w not in SUBMIT_WORDS)[:40] or normalize(ctx.task.goal)
            mem.searches += 1
            mem.pending_enter = True
            mem.nav_depth += 1
            x, y = boxes[0].box.center
            return _decision(ctx, items, Action("fill", x=x, y=y, text=query, target=boxes[0].label_text or "Search",
                                                field_kind="search"),
                             "Searching the site should find it.", f"I type '{query}' into the search box.", 0.3)
        return None

    def _search(self, ctx: ActorContext, items: list[TextBox], observations: list[Observation]) -> Decision:
        mem, t = ctx.memory, ctx.persona.traits
        last = ctx.last_signals
        if mem.scrolls and last and last.scroll_end and not mem.at_bottom:
            mem.at_bottom = True
        step_px = int(ctx.screen.height * (0.85 if t.style == "scanner" else 0.7))
        # Looking for information: after a first look down the page, people turn to the menu or search.
        if ctx.task.type == "find" and (mem.scrolls >= 1 or mem.at_bottom) and mem.screen_sig not in mem.explored:
            mem.explored.add(mem.screen_sig)
            nav = self._explore(ctx, items)
            if nav is not None:
                return nav
        if not mem.at_bottom:
            mem.scrolls += 1
            mem.last_scroll_dy = step_px
            return _decision(ctx, items, Action("scroll", dy=step_px),
                             "What I need is probably further down.", "I scroll down to look for a way forward.",
                             0.3 + 0.1 * min(mem.scrolls, 4), observations)
        if not mem.careful:
            mem.careful = True
            observations.append(Observation("cannot_find", "no obvious way forward on this screen", None, 3))
            return _decision(ctx, items, Action("wait"), "There must be a button somewhere.",
                             "I read the page slowly, looking for anything clickable.", 0.6, observations)
        if not mem.scrolled_up:
            mem.scrolled_up = True
            mem.at_bottom = False
            mem.scrolls = 0
            return _decision(ctx, items, Action("scroll", dy=-ctx.screen.height * 6),
                             "Maybe I missed it at the top.", "I scroll back to the top.", 0.7, observations)
        nav = self._explore(ctx, items)
        if nav is not None:
            return nav
        if mem.nav_depth > 0 and mem.backs < 3:
            mem.backs += 1
            mem.nav_depth -= 1
            observations.append(Observation("dead_end", "this page did not have what I was looking for", None, 2))
            return _decision(ctx, items, Action("back"), "The previous page had other options.",
                             "I go back.", 0.6, observations)
        observations.append(Observation("cannot_find", "searched the whole page and found no way forward", None, 4))
        return _decision(ctx, items, Action("quit"), "There is no way forward.", "I give up.", 0.9,
                         observations, QUIT_LINES["cannot_find"])


def goal_keywords(goal: str) -> list[str]:
    """Words a person would look for, from the task goal (plus common synonyms)."""
    words = [w for w in normalize(goal).split() if len(w) >= 4 and w not in GOAL_STOPWORDS]
    out = list(words)
    for w in words:
        for root, synonyms in NAV_SYNONYMS.items():
            if w.startswith(root):
                out += synonyms
    return list(dict.fromkeys(out))


def target_keywords(task: TaskSpec) -> tuple[list[str], list[str]]:
    """Words straight from the task (strong) and their synonyms (weaker)."""
    if task.keywords:  # the study author said what to look for; don't second-guess it
        return [normalize(k) for k in task.keywords], []
    goal = goal_keywords(task.goal)
    direct = [w for w in normalize(task.goal).split() if w in goal]
    return direct, [w for w in goal if w not in direct]


def page_heading(items: list[TextBox]) -> TextBox | None:
    """The page title: clearly the largest text that is not on a button or the header bar."""
    body = [t for t in items if t.y0 >= 70]
    if len(body) < 2:
        return None
    top = max(body, key=lambda t: t.height)
    heights = sorted(t.height for t in body)
    median = heights[len(heights) // 2]
    return top if top.height >= 1.25 * median and not top.on_fill else None


def _field_key(f: FieldView) -> str:
    """Stable key for a field across screens (OCR may drop spaces: 'Fullname' vs 'Full name')."""
    return normalize(f.label_text).replace(" ", "") if f.label_text else f"field@{f.box.y0}"


def _union_region(boxes: list[InputBox]) -> dict[str, int] | None:
    if not boxes:
        return None
    x0, y0 = min(b.x0 for b in boxes), min(b.y0 for b in boxes)
    x1, y1 = max(b.x1 for b in boxes), max(b.y1 for b in boxes)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
