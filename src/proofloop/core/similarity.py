"""Text similarity without external services: TF-IDF vectors and cosine scores.

This is the default backend for persona diversity checks and issue grouping.
It runs locally and costs nothing.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

import numpy as np

TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have he her his i
    if in into is it its just me my no not of on or our she so than that the their them then there
    these they this to too up us was we were what when where which while who will with would you your
    very really about after again all also am any because before being both each few more most other
    out over same some such only own here how why down off once under until""".split()
)


def tokenize(text: str, *, bigrams: bool = True) -> list[str]:
    words = [w for w in TOKEN.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]
    words = [_stem(w) for w in words]
    if bigrams:
        words += [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
    return words


def _stem(word: str) -> str:
    """Tiny suffix stripper so 'clicked', 'clicking' and 'clicks' match."""
    for suffix in ("ingly", "edly", "ing", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


class TfidfModel:
    def __init__(self, documents: Sequence[str]):
        self.docs = [tokenize(d) for d in documents]
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(set(doc))
        n = max(1, len(self.docs))
        self.vocab = {term: i for i, term in enumerate(sorted(df))}
        self.idf = np.array([math.log((1 + n) / (1 + df[t])) + 1.0 for t in sorted(df)], dtype=float)
        self.matrix = np.vstack([self._vector(doc) for doc in self.docs]) if self.docs else np.zeros((0, 0))

    def _vector(self, tokens: list[str]) -> np.ndarray:
        vec = np.zeros(len(self.vocab), dtype=float)
        for term, count in Counter(tokens).items():
            if term in self.vocab:
                vec[self.vocab[term]] = (1 + math.log(count)) * self.idf[self.vocab[term]]
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def vector(self, text: str) -> np.ndarray:
        return self._vector(tokenize(text))

    def pairwise(self) -> np.ndarray:
        if self.matrix.size == 0:
            return np.zeros((len(self.docs), len(self.docs)))
        return np.clip(self.matrix @ self.matrix.T, 0.0, 1.0)


def similarity_matrix(texts: Sequence[str]) -> np.ndarray:
    return TfidfModel(texts).pairwise()


def similarity(a: str, b: str) -> float:
    return float(similarity_matrix([a, b])[0, 1])


def cluster_by_threshold(sim: np.ndarray, threshold: float, *, must_link=None, cannot_link=None) -> list[list[int]]:
    """Average-linkage agglomerative clustering: merge while mean similarity >= threshold.

    ``cannot_link(i, j)`` may veto merging two items (for example, different screens).
    """
    clusters: list[list[int]] = [[i] for i in range(sim.shape[0])]

    def linkage(a: list[int], b: list[int]) -> float:
        if cannot_link and any(cannot_link(i, j) for i in a for j in b):
            return -1.0
        if must_link and any(must_link(i, j) for i in a for j in b):
            return 1.0
        return float(np.mean([sim[i, j] for i in a for j in b]))

    while len(clusters) > 1:
        best, pair = -1.0, None
        for x in range(len(clusters)):
            for y in range(x + 1, len(clusters)):
                score = linkage(clusters[x], clusters[y])
                if score > best:
                    best, pair = score, (x, y)
        if pair is None or best < threshold:
            break
        x, y = pair
        clusters[x] = clusters[x] + clusters[y]
        del clusters[y]
    return sorted((sorted(c) for c in clusters), key=lambda c: c[0])
