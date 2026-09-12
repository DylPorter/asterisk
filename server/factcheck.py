"""Agentic fact-check for one short-form video.

The model owns the search strategy -- which claims are worth a query, how many
queries to spend, when it has enough. We own the deadline.

Provider-agnostic by construction: the LLM is any OpenRouter model (OpenAI-
compatible wire format), search is Exa. Swapping either is a config change.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from exa_py import Exa
from openai import OpenAI
from pydantic import BaseModel, BeforeValidator, Field, ValidationError, model_validator
from typing_extensions import Annotated

load_dotenv(Path(__file__).parent / ".env")

MODEL = os.environ.get("MODEL", "nex-agi/nex-n2.5-mini:free")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Google ships an OpenAI-compatible surface, so switching providers is a
# base_url + key swap rather than a second client implementation.
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _client_for(model: str) -> tuple[str, str] | None:
    """Return (base_url, api_key) for whichever provider owns this model id."""
    if model.startswith(("gemini", "models/gemini")):
        key = os.environ.get("GEMINI_API_KEY")
        return (GEMINI_BASE, key) if key else None
    key = os.environ.get("OPENROUTER_API_KEY")
    return (OPENROUTER_BASE, key) if key else None

# Exa search tiers, from the API guide: instant ~250ms, fast ~450ms, auto ~1s.
# Every search is a round trip we orchestrate now, so the tier is a real
# latency lever -- not the same as Anthropic's server-side tool, which ran the
# whole loop remotely.
SEARCH_TYPE = os.environ.get("EXA_SEARCH_TYPE", "fast")


# --- Output contract -------------------------------------------------------
# Concision lives in the schema, not in the prompt. max_length holds where
# "be brief" drifts.

Rating = Literal["supported", "contradicted", "misleading", "unverifiable"]


def _clip(limit: int):
    """Truncate over-length strings instead of rejecting the whole verdict.

    Measured: 2 of 5 failures in a live scroll session were `string_too_long`
    on `headline` or `claim` -- verdicts that were otherwise complete and
    correct, thrown away over a few characters. maxLength in the JSON schema is
    advisory; models routinely exceed it. Rejecting is the wrong response to a
    cosmetic overrun.
    """
    def v(s: Any) -> Any:
        if isinstance(s, str) and len(s) > limit:
            return s[: limit - 1].rstrip() + "…"
        return s
    return BeforeValidator(v)


class ClaimCheck(BaseModel):
    # Deliberately flat. An earlier version nested Citation objects inside
    # claims inside the verdict -- three levels under strict:true -- and every
    # small model failed schema validation (qwen3.7-flash: 8 errors,
    # mercury-2.5: 1). Depth costs more compliance than the quote field was
    # worth, so citations are bare URLs.
    claim: Annotated[str, _clip(95)] = Field(max_length=95, description="The claim in as few words as possible. Not a sentence from the transcript -- the claim itself.")
    rating: Rating
    note: Annotated[str, _clip(120)] = Field(max_length=120, description="What the evidence says, in one short clause. No preamble, no hedging.")
    sources: list[str] = Field(default_factory=list, max_length=3, description="URLs that support the rating.")


class ShortVerdict(BaseModel):
    # 1-5 drives the badge glyph. It is a single digit because the badge is a
    # circle the size of a fingertip -- there is no room for prose, and the
    # viewer reads it in under a second while the video plays.
    score: int = Field(
        ge=1, le=5,
        description=(
            "5 = every checked claim holds up. "
            "4 = substantially true, minor caveat or missing context. "
            "3 = mixed; true and false parts, or true but materially misleading. "
            "2 = mostly wrong; the main claim does not hold. "
            "1 = the central claim is false."
        ),
    )
    overall: Literal["clear", "caution", "misleading", "unverifiable"]
    headline: Annotated[str, _clip(55)] = Field(max_length=55, description="A few words. What a viewer needs to know at a glance.")

    # Counts, not prose. One wrong statement in a six-statement video is a
    # different thing from a video that is wrong throughout, and a single
    # "mixed" label collapses that distinction -- which is exactly how the
    # Elon clip got marked down for one conflation (daily income vs net worth)
    # while the rest of it held up.
    statements_checked: int = Field(ge=0, description="Factual statements actually verified.")
    statements_wrong: int = Field(ge=0, description="Of those, how many did not hold up.")
    factual_pct: int = Field(
        ge=0, le=100,
        description="Percentage of the video's factual content that holds up. Weight by how load-bearing each statement is, not by sentence count.",
    )

    claims: list[ClaimCheck] = Field(max_length=4)
    skipped: int = Field(description="Checkable claims deliberately left unchecked to stay in budget.")

    @model_validator(mode="before")
    @classmethod
    def _salvage(cls, data: Any) -> Any:
        """Repair the payload shapes models actually emit, instead of rejecting.

        Every rule here is a failure observed live, not a hypothetical:
          - `claims:too_long`   -- more than 4 claims returned; keep the first 4
                                   rather than discard a good verdict.
          - all fields missing  -- the object was wrapped in another key
                                   ("verdict", "output", "result"); unwrap it.
          - numeric fields absent -- derive them from the claims, which are the
                                   part the model reliably produces.

        The verdict is expensive (searches already paid for). Throwing it away
        over a structural quirk is the worst possible trade.
        """
        if not isinstance(data, dict):
            return data

        if "score" not in data:
            for key in ("verdict", "output", "result", "response"):
                inner = data.get(key)
                if isinstance(inner, dict) and "score" in inner:
                    data = inner
                    break

        data = dict(data)
        claims = data.get("claims")
        if isinstance(claims, list) and len(claims) > 4:
            data["claims"] = claims[:4]
        claims = data.get("claims") if isinstance(data.get("claims"), list) else []

        bad = sum(
            1 for c in claims
            if isinstance(c, dict) and c.get("rating") in ("contradicted", "misleading")
        )
        data.setdefault("statements_checked", len(claims))
        data.setdefault("statements_wrong", bad)
        data.setdefault("skipped", 0)
        if "score" not in data:
            data["score"] = 5 if not claims else (2 if bad else 4)
        if "factual_pct" not in data:
            total = max(len(claims), 1)
            data["factual_pct"] = int(100 * (total - bad) / total)
        if "overall" not in data:
            data["overall"] = "misleading" if bad else "clear"
        if "headline" not in data:
            data["headline"] = "Checked" if claims else "Nothing checkable found"
        return data


SYSTEM = """You fact-check short-form video for a badge that overlays the video while someone watches it.

You MUST call exa_search before answering. You have a budget of 3 searches and you should normally use at least 2. Your job is to decide WHICH claims get them -- not whether to search at all. A verdict reached without searching is worthless here: you are checking claims against the world, not against the transcript.

EVERY claim you list MUST be something actually said in the transcript. Search results are evidence ABOUT those claims -- never a source OF them. If a search returns information on a topic the video never mentions, discard it. Do not report a claim the video did not make: a clip about Amazon and Apple must never produce a claim about Nintendo, however relevant the search results looked. If you cannot quote the transcript for a claim, it does not go in the list.

How to spend the budget:

1. Rank the claims by whether being wrong would change how a viewer acts, spends, or votes. Search the top one or two.
2. Opinion, taste, jokes and unfalsifiable statements do not get searches. Count them in `skipped`.
3. One claim per search. Query the claim, not the video's topic.
4. Prefer primary sources and named institutions over aggregators.
5. Rate `unverifiable` ONLY after a search failed to settle it -- never as a way to avoid searching.

Then report counts and a score.

Counts first -- they are what the viewer actually reads:
    statements_checked  factual statements you verified
    statements_wrong    of those, how many did not hold up
    factual_pct         how much of the video's factual content holds up, weighted by how load-bearing each statement is

Score 1-5, and anchor it to `factual_pct` and to whether the CENTRAL point survives:

    5  everything checked holds up (~95-100% factual)
    4  the main point stands; one secondary detail is wrong or imprecise (~75-95%)
    3  genuinely mixed -- the central claim is in doubt, or true-but-materially-misleading (~45-75%)
    2  the main claim does not hold (~20-45%)
    1  the central claim is false (<20%)

Judge the video's thesis, not its worst sentence. A clip that gets its main argument right but conflates two related figures in one line is a 4 with one wrong statement -- not a 3. Reserve 3 for when a viewer would actually walk away believing something false. Conversely, a clip whose every sentence is literally true but whose framing leads to a false conclusion is a 3, however clean the individual statements are.

Write for a phone badge, not a report. The headline is a few words; each note is one short clause. Cut every qualifier that does not change the meaning -- "reportedly", "it appears that", "according to available sources" all go. If a note would be truncated on screen it was too long to begin with.

A confidently wrong badge is worse than an honest "unverifiable" one, but an unresearched badge is worse than both."""

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "exa_search",
        "description": "Search the web for evidence about one specific factual claim. One claim per call.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A focused query about a single claim. Not the video's topic -- the claim.",
                }
            },
            "required": ["query"],
        },
    },
}


def _strict_schema() -> dict[str, Any]:
    """Pydantic JSON schema, tightened for structured-output endpoints."""
    schema = ShortVerdict.model_json_schema()

    def tighten(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                tighten(v)
        elif isinstance(node, list):
            for v in node:
                tighten(v)

    tighten(schema)
    return schema


def _extract_json(raw: str) -> str:
    """Pull the JSON object out of a response that may be wrapped in prose.

    Measured: a model emitted its own chain of thought ("Then output a single
    valid JSON value... Reasoning prose... --- JSON OUTPUT: {...}") before the
    object. The verdict was in there; only the framing broke the parse.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start : end + 1] if 0 <= start < end else raw


def _reconcile(v: ShortVerdict) -> ShortVerdict:
    """Force score, overall and the claims to agree.

    Measured failure: a run returned overall="unverifiable" with all three
    claims rated "supported" and a headline reading "Mostly accurate". The
    schema lets these fields float free of each other, and the badge is the one
    thing a viewer actually reads -- so derive it rather than trust it.

    The model's own `score` is kept when it is consistent with the claims; it
    carries judgement the ratings alone don't (a technically-true claim that
    misleads should score 3, not 5). It is only overridden when it contradicts
    the evidence outright.
    """
    ratings = [c.rating for c in v.claims]

    if not ratings or all(r == "unverifiable" for r in ratings):
        v.score, v.overall = 3, "unverifiable"
        v.factual_pct = -1          # -1 means "no claim", not "0% true"
        return v

    # Coverage gate. factual_pct is computed over what we CHECKED, but the
    # badge is read as a statement about the VIDEO. Measured: a motivational
    # clip checked 1 claim, skipped 4, and displayed "100% FACTUAL" with a
    # green star -- an unearned verdict on four unexamined statements.
    # If we looked at less than half of what was checkable, we do not know
    # enough to certify the video, however well the sample did.
    checked = max(len(ratings), v.statements_checked)
    coverage = checked / max(checked + v.skipped, 1)
    # Low coverage hides the PERCENTAGE but must not invent a fault. Capping
    # the score at 3 put "Mixed" on the badge -- which asserts the video has
    # false parts -- for videos where every checked claim came back supported.
    # The honest signal is: here is what we found, and here is how much we
    # looked at. CHECKED/SKIPPED on the card carries the second half; the
    # badge carries the first.
    low_coverage = coverage < 0.5

    # Cap by the PROPORTION that is wrong, not by whether anything is.
    # The presence-based version capped at 2 on a single `contradicted`
    # claim, so a clip whose only error was a misremembered year scored an
    # orange X while its own headline read "mostly true" -- the badge and the
    # text disagreeing in front of the viewer. One bad detail among several
    # good ones is a 4; you need most of it wrong to earn a 2.
    bad = sum(1 for r in ratings if r in ("contradicted", "misleading"))
    frac = bad / len(ratings)
    if bad == 0:
        ceiling = 4 if "unverifiable" in ratings else 5
    elif frac <= 0.34:
        ceiling = 4          # a minority slip; the thesis survives
    elif frac <= 0.5:
        ceiling = 3
    elif frac < 1.0:
        ceiling = 2
    else:
        ceiling = 1          # everything checked failed

    v.score = max(1, min(v.score, ceiling))
    v.overall = {5: "clear", 4: "clear", 3: "caution", 2: "misleading", 1: "misleading"}[v.score]

    # The counts are shown as "N of M wrong", so they have to be arithmetically
    # possible. Models routinely emit wrong > checked, or a 90% factual score
    # next to a verdict saying the central claim is false.
    v.statements_checked = max(v.statements_checked, len(v.claims))
    v.statements_wrong = min(v.statements_wrong, v.statements_checked)
    bad = sum(1 for c in v.claims if c.rating in ("contradicted", "misleading"))
    v.statements_wrong = max(v.statements_wrong, min(bad, v.statements_checked))

    # Nothing wrong means nothing to deduct. A "95% factual" badge beside
    # three claims all rated supported invites the obvious question -- what is
    # the missing 5%? -- and there is no answer, because the model picked the
    # number from vibes rather than arithmetic.
    if v.statements_wrong == 0 and ratings and all(r == "supported" for r in ratings):
        v.factual_pct = 100
    else:
        band = {5: (90, 100), 4: (70, 95), 3: (40, 75), 2: (15, 45), 1: (0, 25)}[v.score]
        v.factual_pct = max(band[0], min(v.factual_pct, band[1]))
    if low_coverage:
        v.factual_pct = -1
    return v


class Result(BaseModel):
    verdict: ShortVerdict | None = None
    queries: list[str] = Field(default_factory=list)
    seconds: float = 0.0
    search_seconds: float = 0.0
    llm_calls: int = 0
    error: str | None = None
    raw: str | None = None   # model output when parsing failed, for shape analysis


def check(
    transcript: str,
    title: str,
    channel: str,
    *,
    deadline_s: float = 10.0,
    max_searches: int = 3,
    model: str | None = None,
) -> Result:
    """Run one fact-check. Never raises; failures come back as Result.error."""
    model = model or MODEL
    started = time.perf_counter()
    out = Result()

    provider = _client_for(model)
    exa_key = os.environ.get("EXA_API_KEY")
    if not provider or not exa_key:
        need = "GEMINI_API_KEY" if model.startswith("gemini") else "OPENROUTER_API_KEY"
        missing = [n for n, ok in ((need, provider), ("EXA_API_KEY", exa_key)) if not ok]
        out.error = f"missing in .env: {', '.join(missing)}"
        return out

    base_url, api_key = provider
    llm = OpenAI(base_url=base_url, api_key=api_key)
    exa = Exa(api_key=exa_key)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": f"Channel: {channel}\nTitle: {title}\n\nTranscript:\n{transcript}\n\nFact-check this.",
        },
    ]

    def remaining() -> float:
        return deadline_s - (time.perf_counter() - started)

    # --- Agent loop: the model decides how many searches to spend ----------
    for _ in range(max_searches + 1):
        if remaining() <= 0.5:
            out.error = f"deadline exceeded ({deadline_s}s) during search"
            out.seconds = time.perf_counter() - started
            return out

        force_answer = len(out.queries) >= max_searches

        # Two phases, and they must not be combined. Sending a strict
        # response_format alongside tools makes the model satisfy the schema
        # immediately and skip searching entirely -- measured: searches=0 and
        # every claim rated `unverifiable`, with the model writing "search this
        # first" into its own notes. Schema only on the answering call.
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "tools": [SEARCH_TOOL]}
        if force_answer:
            # Keep the tool DECLARED and forbid it with tool_choice instead of
            # dropping it. The history still contains tool_calls and tool
            # results; removing the declaration leaves that history dangling
            # and providers handle it inconsistently -- measured as schema
            # violations in-pipeline on a model that validates cleanly when
            # called standalone.
            kwargs["tool_choice"] = "none"
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "short_verdict", "strict": True, "schema": _strict_schema()},
            }

        try:
            out.llm_calls += 1
            resp = llm.with_options(timeout=max(remaining(), 1.0)).chat.completions.create(**kwargs)
        except Exception as e:
            out.error = f"{type(e).__name__}: {e}"
            out.seconds = time.perf_counter() - started
            return out

        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []

        if force_answer:
            # Answering call: schema was enforced, so this is the verdict.
            raw = msg.content or ""
            try:
                out.verdict = _reconcile(ShortVerdict.model_validate_json(_extract_json(raw)))
            except ValidationError as e:
                # "1 error" is useless for debugging. Record the exact field,
                # the error type, and the offending value so failure SHAPES can
                # be grouped across a scroll session instead of guessed at.
                parts = [
                    f"{'.'.join(str(x) for x in err['loc'])}:{err['type']}"
                    for err in e.errors()[:6]
                ]
                out.error = f"schema violation: {'; '.join(parts)}"
                out.raw = raw[:4000]
            except Exception as e:
                out.error = f"parse failed: {type(e).__name__}: {e}"
                out.raw = raw[:4000]
            out.seconds = time.perf_counter() - started
            return out

        if not calls:
            # The agent decided it has searched enough. Its reply here is
            # unstructured prose -- keep it as context and let the next pass
            # produce the schema-conforming verdict.
            # Must be followed by a user turn: Gemini rejects a request whose
            # messages end with an assistant turn ("Requests ending with a
            # model turn are not supported"). OpenRouter tolerated it, so this
            # only surfaced on switching providers.
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": "Give the final verdict now."})
            max_searches = len(out.queries)  # trip force_answer next iteration
            continue

        messages.append(msg.model_dump(exclude_none=True))
        for call in calls:
            try:
                query = json.loads(call.function.arguments).get("query", "")
            except Exception:
                query = ""
            out.queries.append(query)

            t0 = time.perf_counter()
            try:
                hits = exa.search(
                    query, type=SEARCH_TYPE, num_results=5, contents={"highlights": True}
                )
                payload = "\n\n".join(
                    f"{r.title}\n{r.url}\n{' … '.join(getattr(r, 'highlights', None) or [])}"
                    for r in hits.results
                ) or "No results."
            except Exception as e:
                payload = f"Search failed: {type(e).__name__}: {e}"
            out.search_seconds += time.perf_counter() - t0

            messages.append(
                {"role": "tool", "tool_call_id": call.id, "name": "exa_search", "content": payload}
            )

    out.error = "loop exhausted without a verdict"
    out.seconds = time.perf_counter() - started
    return out
