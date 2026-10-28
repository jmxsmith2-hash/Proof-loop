"""Post-task questionnaires that ask about behaviour, never "what do you think?".

Question types:
  * forced-choice purchase intent at fixed prices (used only to find price objections)
  * "which step would make you quit"
  * rank the screens you saw from worst to best
  * anchored ratings (every scale point has a concrete description)
  * at least one specific, located complaint per screen

The product is presented without its maker (optionally as a competitor's product).
The heuristic engine answers from what the persona actually did; a model engine
answers from the screenshots, and is sent back when an answer is vague.
"""

from __future__ import annotations

import io
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from synthux.core.config import load_yaml_file
from synthux.core.llm import LLMGateway, LLMRequest
from synthux.core.logging import get_logger
from synthux.personas.models import Persona
from synthux.runner.perception import Screen, perceive

log = get_logger(__name__)

PROBLEM_SIGNALS = ("error_seen", "unexplained_error", "no_change", "slow_load", "data_lost", "cannot_find",
                   "repeat_action", "no_loading_feedback", "scroll_search")
PRAISE_WORDS = ("great", "love", "easy", "intuitive", "nice", "perfect", "excellent", "well designed",
                "beautiful", "amazing", "smooth", "clean design")
VAGUE_PHRASES = ("could be better", "a bit confusing", "not great", "some issues", "improve", "fine", "ok")


@dataclass
class Complaint:
    screen_id: str
    screen_name: str
    element: str               # the visible element the complaint is about
    region: dict[str, int] | None
    what: str
    why: str
    severity: int
    step_idx: int
    category: str = "elicited"
    screenshot: str = ""


@dataclass
class Questionnaire:
    session_id: str
    persona_id: str
    framing: str
    price_choices: list[dict[str, Any]] = field(default_factory=list)
    quit_step: dict[str, Any] = field(default_factory=dict)
    screen_ranking: list[dict[str, Any]] = field(default_factory=list)   # worst first
    ratings: list[dict[str, Any]] = field(default_factory=list)          # per screen, anchored
    complaints: list[Complaint] = field(default_factory=list)
    rounds: int = 1

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["complaints"] = [asdict(c) for c in self.complaints]
        return data


def load_scales(config_dir: Path) -> dict[str, Any]:
    return load_yaml_file(Path(config_dir) / "scales.yaml")


def _png_from_jpeg(path: str) -> bytes:
    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _screen_name(screen: Screen) -> str:
    big = [t for t in screen.texts if t.y0 > 60 and len(t.text) > 2]
    if not big:
        return "this screen"
    return max(big, key=lambda t: (t.height, -t.y0)).text[:50]


def _problem_score(signals: dict[str, Any]) -> float:
    weights = {"error_seen": 2, "unexplained_error": 2, "no_change": 1, "slow_load": 2, "data_lost": 3,
               "cannot_find": 2, "repeat_action": 1, "no_loading_feedback": 1, "scroll_search": 0.5}
    score = sum(w for k, w in weights.items() if signals.get(k))
    return score + 2 * float(signals.get("confusion", 0.0))


# --------------------------------------------------------------- heuristic


class HeuristicElicitor:
    """Answers the questionnaire from the persona's own recorded behaviour."""

    def __init__(self, scales: dict[str, Any]):
        self.scales = scales

    def answer(self, persona: Persona, session: dict[str, Any], steps: list[dict[str, Any]],
               price_points: list[float], framing: str, issues: list[dict[str, Any]]) -> Questionnaire:
        q = Questionnaire(session_id=session["id"], persona_id=persona.id, framing=framing)
        screens = self._screens(steps)
        problems = {sid: sum(_problem_score(s["signals_json"]) for s in rows) for sid, rows in screens.items()}
        quit_screen = steps[-1]["screen_id"] if session["outcome"] in ("quit", "budget_exhausted") and steps else None
        for sid in screens:
            if sid == quit_screen:
                problems[sid] += 10

        q.ratings = [self._rating(sid, rows, sid == quit_screen) for sid, rows in screens.items()]
        q.screen_ranking = [{"screen_id": sid, "problem_score": round(problems[sid], 2)}
                            for sid in sorted(screens, key=lambda s: -problems[s])]
        q.quit_step = self._quit_step(steps, session)
        q.price_choices = [self._price(p, persona, session) for p in price_points]
        q.complaints = [self._complaint(sid, rows, issues) for sid, rows in screens.items()]
        return q

    @staticmethod
    def _screens(steps: list[dict[str, Any]]) -> "OrderedDict[str, list[dict[str, Any]]]":
        grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for s in steps:
            grouped.setdefault(s["screen_id"], []).append(s)
        return grouped

    def _rating(self, sid: str, rows: list[dict[str, Any]], quit_here: bool) -> dict[str, Any]:
        problems = sum(1 for r in rows for k in PROBLEM_SIGNALS if r["signals_json"].get(k))
        confusion = max(float(r["signals_json"].get("confusion", 0)) for r in rows)
        if quit_here:
            ease = 1
        elif problems >= 3:
            ease = 2
        elif problems >= 1:
            ease = 3
        elif confusion >= 0.2:
            ease = 4
        else:
            ease = 5
        unexplained = any(r["signals_json"].get("unexplained_error") for r in rows)
        clarity = 1 if unexplained and quit_here else 2 if unexplained else 3 if confusion >= 0.5 else 4 if confusion >= 0.2 else 5
        return {"screen_id": sid, "ease": ease, "ease_anchor": self.scales["ease"]["points"][ease],
                "clarity": clarity, "clarity_anchor": self.scales["clarity"]["points"][clarity]}

    @staticmethod
    def _quit_step(steps: list[dict[str, Any]], session: dict[str, Any]) -> dict[str, Any]:
        if not steps:
            return {}
        deltas, prev = [], 0.0
        for s in steps:
            deltas.append((s["frustration"] - prev, s))
            prev = s["frustration"]
        delta, worst = max(deltas, key=lambda d: d[0])
        return {"screen_id": worst["screen_id"], "step_idx": worst["idx"], "frustration_jump": round(delta, 1),
                "action": worst["intended_json"].get("target") or worst["intended_json"].get("kind"),
                "actually_quit": session["outcome"] in ("quit", "budget_exhausted")}

    def _price(self, price: float, persona: Persona, session: dict[str, Any]) -> dict[str, Any]:
        choices = self.scales["price"]["choices"]
        budget = persona.traits.budget_usd or 120.0
        ratio = session.get("final_frustration", 0) / 100
        if session["outcome"] != "completed" and ratio > 0.3:
            choice = choices[3]
            reason = "the process put me off"
        elif price > budget * 1.1:
            choice = choices[1] if persona.traits.budget != "tight" else choices[3]
            reason = f"${price:.0f} is above what I planned to spend (about ${budget:.0f})"
        elif ratio > 0.35:
            choice = choices[2]
            reason = "price is fine but the experience made me hesitate"
        else:
            choice = choices[0]
            reason = "within budget and nothing stopped me"
        return {"price": price, "choice": choice, "reason": reason}

    @staticmethod
    def _measured_problem(row: dict[str, Any]) -> tuple[str, str] | None:
        sig = row["signals_json"]
        target = sig.get("load_trigger") or row["intended_json"].get("target") or "it"
        if _problem_score(sig) <= 0.5:
            return None
        if sig.get("slow_load") or sig.get("loading"):
            return "slow_response", f"After I clicked '{target}' the screen stayed the same for several seconds."
        if sig.get("error_seen"):
            return "unexplained_error", f"After '{target}' I got the error '{sig.get('error_text', '')}'."
        if sig.get("no_change"):
            return "dead_click", f"Clicking '{target}' did not do anything I could see."
        if sig.get("cannot_find") or sig.get("scroll_search"):
            return "hidden_control", "I had to search this screen for a way to continue."
        return None

    @staticmethod
    def _complaint(sid: str, rows: list[dict[str, Any]], issues: list[dict[str, Any]]) -> Complaint:
        """Every screen gets at least one specific, located complaint."""
        first = rows[0]
        screen = perceive(_png_from_jpeg(first["screenshot_path"]))
        name = _screen_name(screen)
        own = [i for i in issues if i["screen_id"] == sid and i["source"] == "behavior"]
        if own:
            worst = max(own, key=lambda i: i["severity"])
            return Complaint(sid, name, element=worst["title"], region=worst["region_json"], what=worst["description"],
                             why="it slowed me down or stopped me", severity=worst["severity"],
                             step_idx=worst["step_idx"], category=worst["category"],
                             screenshot=worst["evidence_json"].get("screenshot", ""))
        worst_row = max(rows, key=lambda r: _problem_score(r["signals_json"]))
        measured = HeuristicElicitor._measured_problem(worst_row)
        if measured:  # something went wrong here: complain about that, in the persona's words
            category, what = measured
            action = worst_row["intended_json"]
            region = ({"x": action["x"] - 30, "y": action["y"] - 15, "w": 60, "h": 30}
                      if action.get("x") is not None else None)
            return Complaint(sid, name, element=action.get("target") or name, region=region, what=what,
                             why="it slowed me down", severity=2, step_idx=worst_row["idx"], category=category,
                             screenshot=worst_row["screenshot_path"])
        texts = [t for t in screen.texts if t.y0 > 60 and len(t.text) >= 4]
        if not texts:
            return Complaint(sid, name, "the page", None, "There is very little on this screen to go on.",
                             "I had to guess what to do", 1, first["idx"], screenshot=first["screenshot_path"])
        faint = min(texts, key=lambda t: (t.contrast, t.height))
        return Complaint(sid, name, element=faint.text[:60], region=faint.region(8),
                         what=f"The text '{faint.text[:60]}' is the least visible thing here "
                              f"({faint.contrast}:1 contrast, {faint.height}px tall).",
                         why="small or faint text is easy to miss", severity=1,
                         step_idx=worst_row["idx"], screenshot=first["screenshot_path"])


# ------------------------------------------------------------------- model

QUESTION_SCHEMA = {
    "type": "object",
    "required": ["price_choices", "quit_step", "screen_ranking", "ratings", "complaints"],
    "properties": {
        "price_choices": {"type": "array", "items": {"type": "object", "required": ["price", "choice", "reason"],
                          "properties": {"price": {"type": "number"}, "choice": {"type": "string"},
                                         "reason": {"type": "string"}}}},
        "quit_step": {"type": "object", "required": ["screen_index", "reason"],
                      "properties": {"screen_index": {"type": "integer"}, "reason": {"type": "string"}}},
        "screen_ranking": {"type": "array", "items": {"type": "integer"}},
        "ratings": {"type": "array", "items": {"type": "object", "required": ["screen_index", "ease", "clarity"],
                    "properties": {"screen_index": {"type": "integer"},
                                   "ease": {"type": "integer", "minimum": 1, "maximum": 5},
                                   "clarity": {"type": "integer", "minimum": 1, "maximum": 5}}}},
        "complaints": {"type": "array", "items": {"type": "object",
                       "required": ["screen_index", "element", "x", "y", "w", "h", "what", "why", "severity"],
                       "properties": {"screen_index": {"type": "integer"}, "element": {"type": "string"},
                                      "x": {"type": "integer"}, "y": {"type": "integer"},
                                      "w": {"type": "integer"}, "h": {"type": "integer"},
                                      "what": {"type": "string"}, "why": {"type": "string"},
                                      "severity": {"type": "integer", "minimum": 1, "maximum": 4}}}},
    },
}


def vague_complaint(c: Complaint) -> str | None:
    """Return why a complaint is not specific enough, or None if it is fine."""
    text = f"{c.what} {c.why}".lower()
    if any(w in text for w in PRAISE_WORDS) and not any(w in text for w in ("not", "hard", "slow", "confus", "miss")):
        return "it praises instead of naming a problem"
    if len(c.what.split()) < 6 or not c.element.strip():
        return "it does not name the exact element"
    if c.region is None or c.region.get("w", 0) <= 0:
        return "it has no location on the screen"
    if any(p == c.what.strip().lower() for p in VAGUE_PHRASES):
        return "it is too vague"
    return None


class ModelElicitor:
    """Asks a model persona the same questions, with the screenshots of each screen it visited."""

    def __init__(self, gateway: LLMGateway, scales: dict[str, Any], max_rounds: int = 2):
        self.gateway = gateway
        self.scales = scales
        self.max_rounds = max_rounds

    async def answer(self, persona: Persona, session: dict[str, Any], steps: list[dict[str, Any]],
                     price_points: list[float], framing: str, competitor_label: str) -> Questionnaire:
        screens = HeuristicElicitor._screens(steps)
        ids = list(screens)
        images = [_png_from_jpeg(rows[0]["screenshot_path"]) for rows in screens.values()]
        product = competitor_label if framing == "competitor" else "the product you just used"
        scale_text = "\n".join(f"{name}: " + "; ".join(f"{k} = {v}" for k, v in spec["points"].items())
                               for name, spec in self.scales.items() if "points" in spec)
        base = (
            f"You are this person: {persona.narrative}\nYou just tried to do this on {product}: "
            f"outcome was {session['outcome']}. The {len(images)} screenshots are the screens you saw, in order "
            f"(screen_index 0..{len(images) - 1}).\nAnswer about what you DID, not what you think in general.\n"
            f"1. For each price in {price_points}, pick exactly one of {self.scales['price']['choices']} and say why.\n"
            "2. Which screen would make you quit, and why?\n3. Rank the screens from worst to best.\n"
            f"4. Rate each screen with these anchored scales (pick the point whose description fits):\n{scale_text}\n"
            "5. For EVERY screen give at least one specific complaint: the exact element, its pixel box, what is "
            "wrong and why it matters to you. No praise."
        )
        prompt = base
        q = Questionnaire(session_id=session["id"], persona_id=persona.id, framing=framing)
        for round_no in range(1, self.max_rounds + 2):
            data = (await self.gateway.complete(LLMRequest(role="persona", system="You answer usability "
                    "questionnaires as a specific person, honestly and critically.", prompt=prompt,
                    schema=QUESTION_SCHEMA, images=images, seed=int(session["seed"]), max_tokens=3000,
                    cacheable=round_no == 1))).data
            q = self._to_questionnaire(q, data, ids, screens)
            q.rounds = round_no
            problems = [f"screen {i}: {why}" for i, c in enumerate(q.complaints) if (why := vague_complaint(c))]
            missing = [i for i, sid in enumerate(ids) if not any(c.screen_id == sid for c in q.complaints)]
            if not problems and not missing:
                break
            prompt = base + "\n\nYour previous answer was not specific enough: " + "; ".join(
                problems + [f"screen {i} has no complaint" for i in missing]) + ". Answer again."
        return q

    @staticmethod
    def _to_questionnaire(q: Questionnaire, data: dict[str, Any], ids: list[str],
                          screens: "OrderedDict[str, list[dict[str, Any]]]") -> Questionnaire:
        def sid(i: int) -> str:
            return ids[max(0, min(len(ids) - 1, i))]

        q.price_choices = data["price_choices"]
        q.quit_step = {"screen_id": sid(data["quit_step"]["screen_index"]), "reason": data["quit_step"]["reason"]}
        q.screen_ranking = [{"screen_id": sid(i)} for i in data["screen_ranking"]]
        q.ratings = [{"screen_id": sid(r["screen_index"]), "ease": r["ease"], "clarity": r["clarity"]}
                     for r in data["ratings"]]
        q.complaints = []
        for c in data["complaints"]:
            screen_id = sid(c["screen_index"])
            first = screens[screen_id][0]
            q.complaints.append(Complaint(screen_id, screen_id, c["element"],
                                          {"x": c["x"], "y": c["y"], "w": c["w"], "h": c["h"]}, c["what"], c["why"],
                                          int(c["severity"]), first["idx"], screenshot=first["screenshot_path"]))
        return q
