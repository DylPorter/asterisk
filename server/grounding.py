"""Code-enforced grounding: every claim must be something the video said.

The prompt already tells the model to list only claims it can quote. That is
not enough -- v1 measured a clip about Amazon and Apple producing a claim about
Nintendo, lifted from a search result. A prompt rule is a request; this module
is the enforcement. Each claim carries a short verbatim `quote`, and a claim
whose quote cannot be found in the transcript is dropped and counted, never
shown.

Matching is fuzzy on purpose. The model sees the same transcript it quotes, so
a genuine quote is usually exact, but models re-punctuate, fix casing, expand
"don't" to "do not" or drop a filler word. A fabricated quote does not survive
a sliding-window similarity check against the whole transcript; a re-punctuated
real one does.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

# Similarity a quote must reach against its best transcript window. 0.85 keeps
# a one-word slip in a ~10-word quote (ratio ~0.9) and rejects a quote that
# only shares the topic. Not tuned on real data yet -- eval/ is where that
# happens; the env override exists so it can be moved without a deploy.
MATCH_THRESHOLD = float(os.environ.get("GROUNDING_THRESHOLD", "0.85"))

# A one- or two-word quote ("the economy") is in nearly every transcript, so
# it grounds nothing. Three words is the shortest span that still pins a claim
# to a specific moment.
MIN_QUOTE_WORDS = 3

# Windows a few words longer and shorter than the quote, because ASR and the
# model disagree on word boundaries ("gonna" / "going to", "5" / "five").
WINDOW_SLACK = 2

_PUNCT = re.compile(r"[^\w\s]+")
# "1,000" -> "1000" before punctuation is spaced out, so a thousands separator
# does not split one number into two tokens.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")

# Tokens that flip or change what a sentence asserts while barely moving a
# character-level ratio. Measured on the 0.85 threshold: "vaccines do cause
# autism" scored 0.923 against "vaccines do not cause autism", and "fell to 8
# percent" scored 0.967 against "fell to 3 percent" -- both passed, so the
# model could quote a statement the video never made and rate it contradicted.
# These must agree EXACTLY between the quote and the matched window.
#
# Every negation counts as one generic marker, so the "don't" -> "do not"
# expansion the module docstring allows still matches; only the COUNT of
# negations has to agree. Apostrophes are already gone, hence "dont".
_NEGATIONS = frozenset(
    "not no never nor neither none nobody nothing nowhere cannot "
    "dont doesnt didnt isnt arent wasnt werent wont wouldnt cant couldnt "
    "shouldnt hasnt havent hadnt aint mustnt neednt".split()
)
# Number words map to digits so "5" / "five" (the ASR-vs-model drift the
# window slack exists for) still agree. Scale words stay words: composing
# "five hundred and seventy four" into 574 is a parser this check does not
# need -- the quote is copied from the same transcript, so both sides spell a
# large number the same way, and a mismatch only costs a dropped claim.
_NUMBER_WORDS = {
    w: str(i) for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_NUMBER_WORDS.update({w: str(10 * i) for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)})
_SCALE_WORDS = frozenset("hundred thousand million billion trillion".split())


def normalise(s: str) -> str:
    """Case, punctuation and whitespace removed -- the parts quotes drift on.

    Apostrophes are deleted rather than spaced so "don't" stays one token.
    """
    s = (s or "").casefold().replace("'", "").replace("’", "")
    s = _THOUSANDS.sub("", s)
    return " ".join(_PUNCT.sub(" ", s).split())


def _critical(word: str) -> str | None:
    """The meaning-bearing form of `word`, or None if it is not one."""
    if word in _NEGATIONS:
        return "<neg>"
    if word in _NUMBER_WORDS:
        return _NUMBER_WORDS[word]
    if word in _SCALE_WORDS or any(ch.isdigit() for ch in word):
        return word
    return None


def critical_tokens(words: list[str]) -> Counter[str]:
    """Negations and numbers in a normalised word list, as a multiset."""
    return Counter(c for w in words if (c := _critical(w)) is not None)


def match_score(quote: str, transcript: str) -> float:
    """Best similarity (0..1) of `quote` against any window of `transcript`.

    Only windows whose negations and numbers agree exactly with the quote's
    count (see _NEGATIONS); the character ratio alone cannot tell "do cause"
    from "do not cause".
    """
    q = normalise(quote)
    t = normalise(transcript)
    if not q or not t:
        return 0.0
    # Word-bounded, so "to 8 percent" is not found inside "to 18 percent".
    if f" {q} " in f" {t} ":
        return 1.0

    qw, tw = q.split(), t.split()
    want = critical_tokens(qw)
    n = len(qw)
    best = 0.0
    sm = SequenceMatcher(autojunk=False)
    sm.set_seq2(q)
    sizes = {min(s, len(tw)) for s in range(max(1, n - WINDOW_SLACK), n + WINDOW_SLACK + 1)}
    for size in sorted(sizes):
        for i in range(0, len(tw) - size + 1):
            window = tw[i : i + size]
            sm.set_seq1(" ".join(window))
            # quick_ratio is an upper bound and ~10x cheaper; skip windows
            # that cannot beat the current best.
            if sm.quick_ratio() <= best:
                continue
            r = sm.ratio()
            if r > best and critical_tokens(window) == want:
                best = r
                if best == 1.0:
                    return best
    return best


def is_grounded(quote: str, transcript: str, threshold: float | None = None) -> bool:
    if len(normalise(quote).split()) < MIN_QUOTE_WORDS:
        return False
    return match_score(quote, transcript) >= (MATCH_THRESHOLD if threshold is None else threshold)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _set(obj: Any, key: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)


def ground(verdict: Any, transcript: str) -> Any:
    """Drop every claim whose quote is not in the transcript. Mutates and returns.

    Accepts a `factcheck.ShortVerdict` or its dict form, so eval code can run
    it on stored verdicts too. The count of dropped claims goes in
    `ungrounded_dropped` -- the quality bar is "grounded claims = 100%", and a
    silent drop would hide how often the model is inventing them.

    The counts are adjusted alongside the list: a dropped claim was never a
    statement the video made, so it cannot count as checked or as wrong.
    Callers re-run `factcheck._reconcile` afterwards to re-derive the badge.
    """
    if verdict is None:
        return verdict
    claims = list(_get(verdict, "claims", None) or [])
    kept, dropped, dropped_bad = [], 0, 0
    for c in claims:
        if is_grounded(_get(c, "quote", "") or "", transcript):
            kept.append(c)
        else:
            dropped += 1
            if _get(c, "rating") in ("contradicted", "misleading"):
                dropped_bad += 1

    _set(verdict, "claims", kept)
    _set(verdict, "ungrounded_dropped", (_get(verdict, "ungrounded_dropped", 0) or 0) + dropped)
    if dropped:
        checked = _get(verdict, "statements_checked", len(claims)) or 0
        wrong = _get(verdict, "statements_wrong", 0) or 0
        _set(verdict, "statements_checked", max(len(kept), checked - dropped))
        _set(verdict, "statements_wrong", max(0, wrong - dropped_bad))
        if not kept:
            # The model's headline described claims that no longer exist.
            _set(verdict, "headline", "No claim matched the audio")
    return verdict
