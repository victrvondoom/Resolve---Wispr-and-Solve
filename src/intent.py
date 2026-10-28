"""Deterministic utterance classifier (no model, no keys).

Kinds: NEW_GOAL, REFINEMENT, CORRECTION, CANCEL, APPROVE, QUESTION.
Qualifiers ("Jordan from Operations", "the Contoso one") are matched against the
mission's discovered candidates by their stable attributes, never by name alone.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

KINDS = ('NEW_GOAL', 'REFINEMENT', 'CORRECTION', 'CANCEL', 'APPROVE', 'QUESTION')

CANCEL = re.compile(r"\b(cancel|abort|stop everything|never ?mind|forget it|do not send|don't send|dont send)\b", re.I)
APPROVE = re.compile(r"^\s*(yes|yep|approve[sd]?|approved|confirm(ed)?|go ahead|send it|that's right|do it)\b", re.I)
CORRECT = re.compile(r"\b(no[,.]? i mean|i meant|not (the )?\w+[^,]*,? (but|i mean)|actually|instead|wrong (one|person|jordan))\b", re.I)
QUESTION = re.compile(r"(\?\s*$)|^\s*(what|which|who|why|how|is|are|can|did|does|status)\b", re.I)
NEW = re.compile(r"\b(new task|new mission|also prepare|prepare a follow-up for|send .* to [A-Z])", re.I)


@dataclass
class Classification:
    kind: str
    text: str
    qualifier_terms: list[str] = field(default_factory=list)
    matched_candidates: list[str] = field(default_factory=list)
    negated_terms: list[str] = field(default_factory=list)
    reason: str = ''

    def as_dict(self) -> dict:
        return asdict(self)


def _hit(word: str, c: dict) -> bool:
    """Department/org may match by prefix (prefixes of 4+ letters); id/email only exactly.
    The display name is deliberately never a match key."""
    soft = [t.casefold() for t in (c.get('dept', ''), c.get('org', '')) if t]
    hard = [t.casefold() for t in (c.get('id', ''), c.get('email', '')) if t]
    return word in hard or any(word == t or (len(word) > 3 and t.startswith(word)) for t in soft)


def match_candidates(text: str, candidates: list[dict]) -> tuple[list[str], list[str]]:
    """Return (matched ids, negated ids). 'not Jordan from Sales' negates Sales."""
    low = text.casefold()
    negated = set()
    for m in re.finditer(r"\bnot (?:the )?(?:\w+ )?(?:\w+ )?(?:from|in|at)? ?(\w+)", low):
        word = m.group(1)
        negated |= {c['id'] for c in candidates if _hit(word, c)}
    positive = re.sub(r"\bnot (?:the )?(?:\w+ )?(?:\w+ )?(?:from|in|at)? ?\w+", ' ', low)
    words = set(re.findall(r"[\w.@-]+", positive))
    matched = [c['id'] for c in candidates if c['id'] not in negated and any(_hit(w, c) for w in words)]
    return matched, sorted(negated)


def classify(text: str, mission: dict | None) -> Classification:
    text = (text or '').strip()
    if not text:
        return Classification('QUESTION', text, reason='empty utterance; nothing executed')
    cands = (mission or {}).get('candidates') or []
    matched, negated = match_candidates(text, cands)
    if mission is None or NEW.search(text) and not CORRECT.search(text):
        return Classification('NEW_GOAL', text, reason='no active mission' if mission is None else 'new goal phrase')
    if CANCEL.search(text):
        return Classification('CANCEL', text, reason='cancel phrase')
    if CORRECT.search(text) or (negated and matched):
        return Classification('CORRECTION', text, matched_candidates=matched, negated_terms=negated, reason='correction phrase')
    if QUESTION.search(text):
        return Classification('QUESTION', text, reason='question form')
    if APPROVE.search(text):
        return Classification('APPROVE', text, reason='approval phrase')
    if matched:
        return Classification('REFINEMENT', text, matched_candidates=matched, negated_terms=negated, reason='names a distinguishing attribute')
    return Classification('REFINEMENT', text, reason='no recognised command; recorded only')
