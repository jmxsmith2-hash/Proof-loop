"""The free, built-in persona engine: rule-based behaviour on top of what OCR can see.

It does not call any model. Each persona's traits shape what it notices
(contrast and size thresholds), how it fills forms (date habits, typos),
how long it searches and when it gives up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from synthux.engines.base import ActorContext
from synthux.personas.models import Traits
from synthux.runner.observe import explained, visible_errors
from synthux.runner.perception import InputBox, Screen, TextBox, fuzzy_contains, normalize
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


# ----------------------------------------------------------- perception


def glance_threshold(t: Traits) -> float:
    base = {"normal": 2.2, "color_blind": 2.3, "low_vision": 3.6}[t.eyesight]
    base += {"scanner": 0.4, "methodical": -0.2, "explorer": 0.0}[t.style]
    if t.time_pressure or t.mood != "calm":
        base += 0.3
    return base


def careful_threshold(t: Traits) -> float:
    return {"normal": 1.3, "color_blind": 1.35, "low_vision": 2.4}[t.eyesight]


def noticed(screen: Screen, traits: Traits, careful: bool) -> list[TextBox]:
    """Text a person with these traits would notice on this screen."""
    min_contrast = careful_threshold(traits) if careful else glance_threshold(traits)
    min_height = 12 if traits.eyesight == "low_vision" else 9
    return [t for t in screen.texts if t.contrast >= min_contrast and t.height >= min_height and t.conf >= 0.5]


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


def form_fields(screen: Screen, traits: Traits) -> list[FieldView]:
    fields = []
    for box in sorted(screen.inputs, key=lambda b: (b.y0, b.x0)):
        label = label_for(box, screen.texts)
        text = label.text if label else ""
        kind, ambiguous, jargon = classify_label(text, traits) if text else (None, True, False)
        fields.append(FieldView(box, label, text, kind, ambiguous, jargon))
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
            mem.dead_targets.add((mem.screen_sig, normalize(mem.clicks[-1]["text"] or "")))

    def _decide(self, ctx: ActorContext) -> Decision:
        screen, mem, t = ctx.screen, ctx.memory, ctx.persona.traits
        items = noticed(screen, t, mem.careful)
        observations: list[Observation] = []
        if ctx.interrupted:
            observations.append(Observation("interruption", "came back after an interruption"))

        # 1. The screen did not react yet but the browser shows it is loading.
        waiting = self._handle_waiting(ctx, items)
        if waiting:
            return waiting
        # 2. Errors on screen.
        error_decision = self._handle_errors(ctx, items, observations)
        if error_decision:
            return error_decision
        # 3. Forms.
        fields = form_fields(screen, t)
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
            return self._click(ctx, items, target, observations)
        # 6. Something remembered from a previous visit.
        remembered = mem.known_targets.get(mem.screen_sig)
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
        sig, timing, mem = ctx.last_signals, ctx.last_timing, ctx.memory
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

    def _handle_errors(self, ctx: ActorContext, items: list[TextBox], observations: list[Observation]) -> Decision | None:
        errors = [e for e in visible_errors(ctx.screen) if e in items or e.contrast >= 2.0]
        mem, t = ctx.memory, ctx.persona.traits
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
            visible_errors(ctx.screen) and mem.typed and all(f.box.empty for f in fields))
        # Data the persona typed is gone after a failed submit.
        labels = {_field_key(f) for f in fields}
        if mem.typed and not set(mem.typed) <= labels:
            mem.typed.clear()  # a different form
            mem.typed_kinds.clear()
        if mem.typed and all(f.box.empty for f in fields):
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
                if f.box.empty and mem.typed_kinds.get(key) not in ("password", None) and len(fields) > 1:
                    del mem.typed[key]  # what I typed isn't there (missed the field); type it again
                    mem.typed_kinds.pop(key, None)
                    confusion_note = Observation("missed_field", f"my text didn't appear in '{key}'")
                    observations.append(confusion_note)
                else:
                    continue
            if not f.box.empty and f.kind != "password":
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
                             f"I click '{label}' and type {'my password' if kind == 'password' else repr(value)}.",
                             confusion, observations)

        submit = self._submit_button(ctx, items, fields)
        if submit is None:
            return None
        mem.remember(f"submitted the form with {', '.join(mem.typed_kinds.values())}")
        x, y = submit.center
        return _decision(ctx, items, Action("click", x=x, y=y, target=submit.text),
                         f"'{submit.text}' should send the form.", f"I click '{submit.text}'.",
                         0.2 if mem.form_attempts else 0.0, observations)

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
        keywords = [normalize(k) for k in ctx.task.keywords]
        scored: list[tuple[float, TextBox]] = []
        for t in items:
            if (mem.screen_sig, t.norm) in mem.dead_targets:
                continue
            if len(t.norm) > 40:
                continue  # paragraphs are not controls
            score = 0.0
            for i, kw in enumerate(keywords):
                if t.norm == kw:
                    score = max(score, 3.0 - i * 0.1)
                elif fuzzy_contains(t.norm, kw, 0.86):
                    score = max(score, 2.2 - i * 0.1)
            if score == 0.0 and any(t.norm == w or fuzzy_contains(t.norm, w, 0.9) for w in PROGRESS_WORDS):
                score = 1.2
            if score == 0.0:
                continue
            if t.on_fill:
                score += 0.6       # looks like a button
            if t.y0 < 70 and t.height < 28:
                score -= 0.8       # small header/navigation link
            scored.append((score, t))
        if not scored:
            return None
        return max(scored, key=lambda st: (st[0], st[1].y0))[1]

    def _click(self, ctx: ActorContext, items: list[TextBox], target: TextBox,
               observations: list[Observation]) -> Decision:
        mem, t = ctx.memory, ctx.persona.traits
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
        mem.known_targets[mem.screen_sig] = {"text": target.text, "x": x, "y": y, "scrolls": mem.scrolls,
                                             "dy": mem.last_scroll_dy}
        expects = f"'{target.text}' should take me to the next step."
        if ctx.framing == "competitor":
            expects = f"On {ctx.competitor_label}, '{target.text}' should take me to the next step."
        return _decision(ctx, items, Action("click", x=x, y=y, target=target.text), expects,
                         f"I click '{target.text}'.", confusion, observations)

    def _search(self, ctx: ActorContext, items: list[TextBox], observations: list[Observation]) -> Decision:
        mem, t = ctx.memory, ctx.persona.traits
        last = ctx.last_signals
        if mem.scrolls and last and last.scroll_end and not mem.at_bottom:
            mem.at_bottom = True
        step_px = int(ctx.screen.height * (0.85 if t.style == "scanner" else 0.7))
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
        observations.append(Observation("cannot_find", "searched the whole page and found no way forward", None, 4))
        return _decision(ctx, items, Action("quit"), "There is no way forward.", "I give up.", 0.9,
                         observations, QUIT_LINES["cannot_find"])


def _field_key(f: FieldView) -> str:
    """Stable key for a field across screens (OCR may drop spaces: 'Fullname' vs 'Full name')."""
    return normalize(f.label_text).replace(" ", "") if f.label_text else f"field@{f.box.y0}"


def _union_region(boxes: list[InputBox]) -> dict[str, int] | None:
    if not boxes:
        return None
    x0, y0 = min(b.x0 for b in boxes), min(b.y0 for b in boxes)
    x1, y1 = max(b.x1 for b in boxes), max(b.y1 for b in boxes)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
