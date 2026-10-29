"""Group duplicate issues into clusters using text similarity (plus an optional model check)."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from proofloop.core.llm import LLMGateway, LLMRequest
from proofloop.core.logging import get_logger
from proofloop.core.similarity import cluster_by_threshold, similarity_matrix
from proofloop.web.runner.observe import TITLES

log = get_logger(__name__)

MERGE_THRESHOLD = 0.32       # average-linkage similarity needed to merge different categories on one screen
BORDERLINE = (0.22, 0.32)    # pairs in this band are asked to the grouper model when one is configured
RELATED = {frozenset({"slow_response", "no_loading_feedback"})}  # same root cause, reported together


@dataclass
class Cluster:
    id: str
    members: list[dict[str, Any]]
    category: str = ""
    title: str = ""
    screen_id: str = ""
    tasks: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


def _text(issue: dict[str, Any]) -> str:
    return f"{issue['category'].replace('_', ' ')} {issue['title']} {issue['description']}"


def cluster_issues(issues: list[dict[str, Any]]) -> list[list[int]]:
    """Indices of issues grouped into clusters. Never merges across screens."""
    if not issues:
        return []
    sim = similarity_matrix([_text(i) for i in issues])

    def cannot_link(a: int, b: int) -> bool:
        return (issues[a]["screen_id"] or "") != (issues[b]["screen_id"] or "")

    def must_link(a: int, b: int) -> bool:
        ia, ib = issues[a], issues[b]
        same = ia["category"] == ib["category"] and ia["category"] != "elicited"
        related = frozenset({ia["category"], ib["category"]}) in RELATED
        return same or related

    return cluster_by_threshold(sim, MERGE_THRESHOLD, must_link=must_link, cannot_link=cannot_link)


async def confirm_borderline(issues: list[dict[str, Any]], groups: list[list[int]],
                             gateway: LLMGateway | None) -> list[list[int]]:
    """Ask the grouper model whether borderline-similar clusters on one screen are the same problem."""
    if gateway is None or not gateway.is_model_backed("grouper") or len(groups) < 2:
        return groups
    sim = similarity_matrix([" ".join(_text(issues[i]) for i in g) for g in groups])
    merged = [list(g) for g in groups]
    alive = list(range(len(groups)))
    schema = {"type": "object", "required": ["same"], "properties": {"same": {"type": "boolean"}}}
    for a in range(len(groups)):
        for b in range(a + 1, len(groups)):
            if a not in alive or b not in alive:
                continue
            if issues[groups[a][0]]["screen_id"] != issues[groups[b][0]]["screen_id"]:
                continue
            if not (BORDERLINE[0] <= sim[a, b] < BORDERLINE[1]):
                continue
            prompt = ("Are these two usability findings the same underlying problem? Answer only true or false.\n"
                      f"A: {_text(issues[groups[a][0]])}\nB: {_text(issues[groups[b][0]])}")
            data = (await gateway.complete(LLMRequest(role="grouper", system="You de-duplicate usability findings.",
                                                      prompt=prompt, schema=schema, max_tokens=50))).data
            if data["same"]:
                merged[a].extend(merged[b])
                alive.remove(b)
    return [sorted(merged[i]) for i in alive]


def build_clusters(issues: list[dict[str, Any]], groups: list[list[int]]) -> list[Cluster]:
    clusters = []
    for n, group in enumerate(groups):
        members = [issues[i] for i in group]
        cats = Counter(m["category"] for m in members)
        category = cats.most_common(1)[0][0]
        if category == "elicited" and len(cats) > 1:
            category = next(c for c, _ in cats.most_common() if c != "elicited")
        names = [TITLES.get(c, c.replace("_", " ").capitalize()) for c in sorted(cats, key=lambda c: -cats[c])
                 if c != "elicited"]
        title = " / ".join(dict.fromkeys(names)) or members[0]["title"]
        clusters.append(Cluster(id=f"c{n + 1:03d}", members=members, category=category, title=title,
                                screen_id=members[0]["screen_id"] or "",
                                tasks=sorted({m["task_id"] for m in members})))
    return clusters


def by_category_screen(issues: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for i in issues:
        out[(i["category"], i["screen_id"] or "")].append(i)
    return out
