from __future__ import annotations

import pytest

import factcheck
import grounding
from conftest import claim, verdict

TRANSCRIPT = (
    "So here's the thing. Amazon made five hundred and seventy four billion "
    "dollars in revenue last year, and Apple sold over two hundred million "
    "iPhones. Honestly, I don't think anyone can catch them."
)


def V(*claims, **kw):
    return factcheck.ShortVerdict.model_validate(verdict(*claims, **kw))


def test_exact_quote_is_kept():
    v = grounding.ground(V(claim("Apple sold over two hundred million iPhones")), TRANSCRIPT)
    assert len(v.claims) == 1
    assert v.ungrounded_dropped == 0


@pytest.mark.parametrize("quote", [
    # Re-punctuated and re-cased: the model tidies what it copies.
    "amazon made five hundred and seventy-four billion dollars in revenue LAST YEAR!",
    # One word dropped from a 13-word quote.
    "Amazon made five hundred and seventy four billion dollars revenue last year",
    # Contraction kept as one token after apostrophe stripping.
    "I dont think anyone can catch them",
    # Curly apostrophe, as models often emit.
    "I don’t think anyone can catch them",
])
def test_near_match_is_kept(quote):
    assert grounding.is_grounded(quote, TRANSCRIPT)


def test_fabricated_quote_is_dropped_and_counted():
    # The v1 failure mode: a claim about Nintendo lifted from a search result.
    v = V(
        claim("Apple sold over two hundred million iPhones"),
        claim("Nintendo sold one hundred million Switch consoles", rating="contradicted"),
        statements_checked=2, statements_wrong=1, score=4,
    )
    v = grounding.ground(v, TRANSCRIPT)
    assert [c.quote for c in v.claims] == ["Apple sold over two hundred million iPhones"]
    assert v.ungrounded_dropped == 1
    # The dropped claim was never said, so it cannot count as checked or wrong.
    assert v.statements_checked == 1
    assert v.statements_wrong == 0


def test_topic_overlap_is_not_enough():
    # Same entities, different statement -- must not ground.
    assert not grounding.is_grounded("Apple sold over two hundred million Macs last year", TRANSCRIPT)


@pytest.mark.parametrize("quote", ["", "   ", "Amazon", "the thing"])
def test_missing_or_too_short_quote_is_dropped(quote):
    v = grounding.ground(V(claim(quote)), TRANSCRIPT)
    assert v.claims == []
    assert v.ungrounded_dropped == 1


def test_all_dropped_reconciles_to_unverifiable():
    v = grounding.ground(V(claim("Tesla delivered ten million cars in March")), TRANSCRIPT)
    v = factcheck._reconcile(v)
    assert v.overall == "unverifiable"
    assert v.factual_pct == -1
    assert v.headline == "No claim matched the audio"


def test_dict_verdict_is_supported():
    d = verdict(claim("Apple sold over two hundred million iPhones"), claim("made up words entirely here"))
    out = grounding.ground(d, TRANSCRIPT)
    assert len(out["claims"]) == 1 and out["ungrounded_dropped"] == 1


def test_empty_transcript_grounds_nothing():
    assert not grounding.is_grounded("Apple sold over two hundred million iPhones", "")


def test_quote_is_clipped_to_25_words_and_still_grounds():
    long_transcript = " ".join(f"word{i}" for i in range(60))
    q = " ".join(f"word{i}" for i in range(10, 40))  # 30 words
    c = factcheck.ClaimCheck.model_validate(claim(q))
    assert len(c.quote.split()) == 25
    assert grounding.is_grounded(c.quote, long_transcript)


def test_null_quote_does_not_fail_the_verdict():
    raw = verdict(claim("Apple sold over two hundred million iPhones"), claim("x"))
    raw["claims"][1]["quote"] = None
    del raw["claims"][0]["quote"]  # missing entirely is tolerated too
    v = factcheck.ShortVerdict.model_validate(raw)
    assert [c.quote for c in v.claims] == ["", ""]


def test_model_cannot_set_ungrounded_dropped():
    v = factcheck.ShortVerdict.model_validate(verdict(ungrounded_dropped=7))
    assert v.ungrounded_dropped == 0


def test_strict_schema_requires_quote_and_hides_bookkeeping():
    s = factcheck._strict_schema()
    assert "ungrounded_dropped" not in s["properties"]
    claim_schema = s["$defs"]["ClaimCheck"]
    assert "quote" in claim_schema["required"]


# --- meaning-bearing tokens --------------------------------------------------
# Both cases below passed the 0.85 character ratio before the token check
# (0.923 and 0.967): a near-verbatim quote that says the opposite.

NEG_TRANSCRIPT = "Doctors say that vaccines do not cause autism, and that study was retracted."
NUM_TRANSCRIPT = "Last month unemployment fell to 3 percent, the lowest since 1969."


@pytest.mark.parametrize("quote", [
    "vaccines do cause autism",                 # negation dropped
    "that vaccines never do not cause autism",  # negation added
])
def test_negation_must_agree(quote):
    assert not grounding.is_grounded(quote, NEG_TRANSCRIPT)


@pytest.mark.parametrize("quote", [
    "vaccines do not cause autism",
    "vaccines don't cause autism",              # contraction <-> expansion is fine
])
def test_negation_kept_when_it_agrees(quote):
    assert grounding.is_grounded(quote, NEG_TRANSCRIPT)


@pytest.mark.parametrize("quote", [
    "unemployment fell to 8 percent",
    "unemployment fell to 13 percent",
    "the lowest since 1996",
])
def test_changed_number_is_dropped(quote):
    assert not grounding.is_grounded(quote, NUM_TRANSCRIPT)


def test_number_word_matches_digit():
    assert grounding.is_grounded("unemployment fell to three percent", NUM_TRANSCRIPT)


def test_misquote_rated_contradicted_never_reaches_the_card():
    v = grounding.ground(V(claim("vaccines do cause autism", "contradicted")), NEG_TRANSCRIPT)
    assert v.claims == []
    assert v.ungrounded_dropped == 1
