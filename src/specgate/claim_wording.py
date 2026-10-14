"""The wording check of the texts an agent wrote, run before the main judgment.

The verification of a spec checks each implementation decision, and the verification of a
ticket graph each acceptance criterion, with `jev_check`: one call per text, against the
scope the grill decided. The code reads each result the same way on both sides.

- A check that passed the gate with a defect refuses the text. The client returns the
  typed refusal of `specgate.wording`, spends no evaluation and opens no research.
- A check that passed the gate without a defect lets the main judgment go on.
- Any other check (the mock, an error, a probability at the cutoff, a host with a manifest)
  keeps the text in review.

The host repeats the last two rules from what the verification stored: a text with no clean
check that passed the gate, bound to this client's policy and recipe, is a gap, so a client
that skips the check never reaches ready or publishes. The Jev only judges: no field here
carries suggested text, and the actions come from the defects, never from a score.

A graph can hold hundreds of criteria and a submit carries them all, so the verification
stores a compact record: what every check shares (the scope, the context and the authorities
that judged) once, and per text only its digest, the request, the authority and the five
probabilities. The host rebuilds the result the client confirmed from that record and
reads it with `policy_verification_intact`, as it reads the stored Verify. The equivalence
check of a division stores the same record (`claim_equivalence`), through `Kind`.

The claims the server derives are checked in the same pass, after those texts: the coverage
of the decisions of the grill and the scope on a spec, the obligations derived from the
sentences of the spec on a ticket graph. The agent wrote none of them, so a defect found
there is never a refusal and never a request to rewrite. A check that passed the gate with a
defect is a derivation diagnostic for the maintainer, which `derivation_diagnostics` reads
from the stored record, and the actions of that claim hold no rewrite for the agent. The host
requires no such check: a derived claim with no check that passed the gate has no gap,
because the agent does not control its text.

A graph derives far more claims than it has criteria, and a submit carries them all, so the
record keeps the whole entry only of what the host reads, a defect or a check the gate did
not pass (`derived`). The claims that passed clean are one digest of their requests, with the
authorities that judged them (`clean`): the next verification reuses them when the set of
requests it would send is the same, and asks again when any of them changed.
"""

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import TypeAdapter

from specgate.client import confirmed_result, policy_verification_intact
from specgate.context import ContextPacket
from specgate.decision_ids import CLAIM_PREFIX
from specgate.gate_policy import BASIS_POLICY, PROVENANCE_KEYS, GatePolicy
from specgate.payload import PayloadTooLarge
from specgate.recipe_inputs import request_revision
from specgate.slices.check import DEFECTS, next_actions
from specgate.verification_refs import AUTHORITY_KEYS, digest
from specgate.wording import WordingRejected

TOOL = "jev_check"
# What the check can allow next, so the context names what it will compare.
ALTERNATIVES = ("judge", "reformulate", "split", "new_grill_question")
# The checks of a batch run together, and a batch is as large as one of jev_verify.
CHECK_BATCH_SIZE = 20
MISSING = "wording_check_missing"
REVIEW = "wording_check_review"
ERROR = "decision_check_error"
# The one action of a weak claim whose derivation has a defect: it belongs to the maintainer,
# who is the only one that can change how the host derived the claim.
REVIEW_DERIVATION = "review_derivation"
# The fields of the wording record that hold the claims the host derived.
DERIVED = "derived"
CLEAN = "clean"


@dataclass(frozen=True)
class Kind:
    """What tells one check from another when its compact record is stored and read.

    The wording check and the equivalence check store the same record: the authority that
    judged, the request and the probabilities of the defects, in the order of the tool.
    """

    tool: str
    defects: Mapping[str, str]
    actions: Callable[[list[str]], list[str]]
    error: str


WORDING = Kind(TOOL, DEFECTS, next_actions, ERROR)

State = Literal["clear", "defective", "review", "missing"]
Support = Literal["supported", "unsupported", "contradicted"]
Target = tuple[str, str]
Call = Callable[..., Awaitable[dict[str, Any]]]


def authority_key(authority: Mapping[str, str]) -> str:
    return digest(json.dumps({key: authority[key] for key in AUTHORITY_KEYS}, sort_keys=True))


def probabilities_of(value: Any, kind: Kind = WORDING) -> list[float] | None:
    """One probability per defect of the check, in the order of its defects, or None."""
    if (
        not isinstance(value, list)
        or len(value) != len(kind.defects)
        or not all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(item)
            and 0 <= item <= 1
            for item in value
        )
    ):
        return None
    return [float(item) for item in value]


def is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def rebuilt(
    wording: Mapping[str, Any], entry: Mapping[str, Any], kind: Kind = WORDING
) -> dict[str, Any] | None:
    """The result the client confirmed, rebuilt from the compact record.

    The policy and the recipe come from the authority the record names, so a record
    judged under other code reads as another policy and recipe, never as this client's.
    """
    authorities = wording.get("authorities")
    authority = authorities.get(entry.get("authority")) if isinstance(authorities, dict) else None
    probabilities = probabilities_of(entry.get("probabilities"), kind)
    request, context = entry.get("request_revision"), wording.get("context_revision")
    if (
        not isinstance(authority, dict)
        or not all(is_text(authority.get(key)) for key in AUTHORITY_KEYS)
        or authority_key(authority) != entry.get("authority")
        or probabilities is None
        or not is_text(request)
        or not is_text(context)
    ):
        return None
    scores = dict(zip(kind.defects, probabilities, strict=True))
    present = [key for key in kind.defects if scores[key] > GatePolicy().check_min_present]
    provenance = {key: authority[key] for key in PROVENANCE_KEYS}
    return {
        "tool": kind.tool,
        "mode": "real",
        "action": "auto",
        "reason": "confidence_policy_satisfied",
        "auto_advance": True,
        "calibrated": False,
        "provider_calls": [provenance],
        "context_revision": context,
        "request_revision": request,
        "probabilities": scores,
        "max_defect": max(scores.values()),
        "defects": present,
        "next_actions": kind.actions(present),
        "gate": {
            "passed": True,
            "basis": BASIS_POLICY,
            "reason": "confidence_policy_satisfied",
            "artifact_revision": None,
            "report_sha256": None,
            "binding": None,
            "policy_binding": {
                "tool": kind.tool,
                "policy": authority["policy"],
                "recipe": authority["recipe"],
                "request_revision": request,
                "context_revision": context,
                **provenance,
            },
            "policy": GatePolicy().model_dump(),
            "scope": "recommendation",
            "execution_authorized": False,
        },
    }


def _judge(
    wording: Any, field: str, target: str, text: str, scope: str
) -> tuple[State, list[str]]:
    """What the entries kept under `field` say about the check of a text, decided again."""
    checks = wording.get(field) if isinstance(wording, dict) else None
    entry = checks.get(target) if isinstance(checks, dict) else None
    if (
        not isinstance(entry, dict)
        or wording.get("scope_revision") != digest(scope)
        or entry.get("text_revision") != digest(text)
    ):
        return "missing", []
    if entry.get("authority") is None:
        return "review", []
    result = rebuilt(wording, entry)
    if result is None or not policy_verification_intact(
        result, result["context_revision"], tool=TOOL
    ):
        return "missing", []
    defects: list[str] = result["defects"]
    return ("defective", defects) if defects else ("clear", [])


def judge(wording: Any, target: str, text: str, scope: str) -> tuple[State, list[str]]:
    """What a stored verification says about the check of a text, decided again.

    clear: a real check that passed the gate under the policy, bound to this client's
    policy and recipe, found no defect. defective: it passed with the defects returned.
    review: the check ran for this text and scope but the gate stayed closed. missing:
    there is no check for this text and scope, or one that claims to have passed does not
    hold, which the host reads as no check.
    """
    return _judge(wording, "checks", target, text, scope)


def wording_gaps(wording: Any, targets: Sequence[Target], scope: str) -> list[str]:
    """One gap per text that has no clean check that passed the gate."""
    gaps = []
    for target, text in targets:
        state, _ = judge(wording, target, text, scope)
        if state != "clear":
            gaps.append(f"Jev: {REVIEW if state == 'review' else MISSING}:{target}")
    return gaps


def _above(value: Any, cutoff: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and cutoff < value <= 1
    )


def support_of(row: Mapping[str, Any], threshold: float, *, strong: bool) -> Support:
    """How the evidence stands on a claim, without moving the cutoff or the criterion.

    A contradiction is confident when confidence and the contradicts probability are both
    above the cutoff of the support criterion; a weak verified row is unsupported.
    """
    cutoff = max(0.8, threshold)
    probabilities = row.get("probabilities")
    contradicts = (
        probabilities.get("contradicts") if isinstance(probabilities, dict) else None
    )
    if (
        row.get("verdict") == "contradicted"
        and _above(row.get("confidence"), cutoff)
        and _above(contradicts, cutoff)
    ):
        return "contradicted"
    return "supported" if strong else "unsupported"


def claim_actions(
    support: Support,
    *,
    wording: State | None = None,
    defects: Sequence[str] = (),
    derivation: Sequence[str] = (),
) -> list[str]:
    """What the code allows to do with a claim, by the cause of its weakness.

    `wording` is what the check of the text found; None means no check applies to the
    claim, as for one the host derived. A text with a defect takes the actions of the
    defect (reformulate or split, or a new grill question for a subject the grill did
    not decide) whatever its score; a text whose check has not passed the gate waits for
    a person. A clean text is researched when unsupported, its contradiction is accepted
    when confident, and a supported one needs nothing.

    `derivation` holds the defects a check that passed the gate found in a claim the host
    derived. The agent did not write that text and cannot rewrite it, so the claim gets
    the one action of the maintainer, whatever its score, and never a reformulation, a
    split or a new grill question. A derived claim whose check did not pass the gate has
    no defect to act on and is routed by the evidence, as before.
    """
    if wording in {"review", "missing"}:
        return []
    if wording == "defective":
        return next_actions(list(defects)) if defects else []
    if derivation:
        return [REVIEW_DERIVATION]
    if support == "contradicted":
        return ["accept_contradiction"]
    return ["research"] if support == "unsupported" else []


def _finding(
    target: str, defects: list[str], probabilities: Any, position: int
) -> dict[str, Any]:
    """Copied from the check result: the defects, the actions and the probabilities.

    The id of a decision with no label is a digest, and the refusal stores nothing the
    agent could look it up in, so the finding of a decision also says where it stands in
    the draft (counted from 1). An acceptance criterion is named by its place already.
    """
    return {
        "target": target,
        **({"position": position} if target.startswith(CLAIM_PREFIX) else {}),
        "defects": list(defects),
        "next_actions": next_actions(defects),
        "probabilities": dict(zip(DEFECTS, probabilities, strict=True)),
    }


async def run_check(
    call: Call, arguments: dict[str, Any], kind: Kind = WORDING
) -> dict[str, Any]:
    """One request, one call. An error is a result that stays in review, not a crash."""
    try:
        result = await call(kind.tool, **arguments)
    except PayloadTooLarge:
        raise
    except ValueError as error:
        return {"error": {"code": kind.error, "message": str(error)}}
    if overflow := PayloadTooLarge.from_result(result):
        raise overflow
    if result.get("error"):
        return result
    # What is stored advances only if this client confirmed the request.
    return confirmed_result(result, kind.tool, arguments)


def stored_entry(
    result: dict[str, Any], identity: dict[str, Any], kind: Kind = WORDING
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """The compact record of one confirmed result, and the authority it names if it passed.

    `identity` is what binds the record to the texts it judged: their digests.
    """
    gate: dict[str, Any] = result["gate"] if isinstance(result.get("gate"), dict) else {}
    binding = gate.get("policy_binding")
    scores = result.get("probabilities")
    probabilities = (
        probabilities_of([scores.get(key) for key in kind.defects], kind)
        if isinstance(scores, dict)
        else None
    )
    entry: dict[str, Any] = dict(identity)
    if (
        result.get("auto_advance") is True
        and result.get("action") == "auto"
        and gate.get("passed") is True
        and isinstance(binding, dict)
        and all(is_text(binding.get(key)) for key in AUTHORITY_KEYS)
        and is_text(result.get("request_revision"))
        and probabilities is not None
    ):
        authority = {key: binding[key] for key in AUTHORITY_KEYS}
        entry.update(
            request_revision=result["request_revision"],
            authority=authority_key(authority),
            probabilities=probabilities,
        )
        return entry, authority
    error = result.get("error")
    reason = gate.get("reason")
    entry["reason"] = (
        reason
        if is_text(reason) and not result.get("error")
        else error.get("code")
        if isinstance(error, dict) and is_text(error.get("code"))
        else "gate_closed"
    )
    if probabilities is not None:
        entry["probabilities"] = probabilities
    if isinstance(error, dict):
        entry["error"] = {
            key: str(error[key]) for key in ("code", "message") if key in error
        }
    return entry, None


def _requests(
    targets: Sequence[Target], scope: str, context: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, Any]]:
    """The call of each text, its request revision and the shell of the record.

    The context of the verification travels with the alternatives the check can allow,
    and the record starts with what every check of the pass shares.
    """
    packet = {**context, "alternatives": list(ALTERNATIVES)}
    arguments = {
        target: {"text": text, "scope": scope, "context": packet} for target, text in targets
    }
    revisions = {
        target: request_revision(TOOL, arguments[target]) for target, _ in targets
    }
    record: dict[str, Any] = {
        "scope_revision": digest(scope),
        "context_revision": TypeAdapter(ContextPacket).validate_python(packet).revision,
        "authorities": {},
        "checks": {},
    }
    return arguments, revisions, record


async def _run(
    call: Call, jobs: list[tuple[str, str]], arguments: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The checks of a pass, by request revision, one batch of calls at a time.

    `jobs` pairs a request revision with the target that carries it. A batch runs to the
    end before the first failure it hit is raised.
    """
    results: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(jobs), CHECK_BATCH_SIZE):
        batch = jobs[offset : offset + CHECK_BATCH_SIZE]
        done = await asyncio.gather(
            *(run_check(call, arguments[target]) for _, target in batch),
            return_exceptions=True,
        )
        for (revision, _), outcome in zip(batch, done, strict=True):
            if isinstance(outcome, BaseException):
                raise outcome
            results[revision] = outcome
    return results


async def check_wording(
    call: Call,
    targets: Sequence[Target],
    scope: str,
    context: Mapping[str, Any],
    prior: Any = None,
) -> dict[str, Any]:
    """Check each text before the main judgment; the record a verification stores.

    An identical request is checked once, and a clean check stored for the very same
    request is kept. Raises `WordingRejected` when a check passed the gate with a
    defect, before the caller spends any evaluation, and `PayloadTooLarge` when the host
    reports the request above the limit.
    """
    arguments, revisions, wording = _requests(targets, scope, context)
    before: Any = prior if isinstance(prior, dict) else {}
    kept: dict[str, dict[str, Any]] = {}
    pending: dict[str, str] = {}
    for target, text in targets:
        if judge(before, target, text, scope)[0] == "clear":
            entry = before["checks"][target]
            if (
                entry.get("request_revision") == revisions[target]
                and before.get("context_revision") == wording["context_revision"]
            ):
                kept[target] = entry
                continue
        pending.setdefault(revisions[target], target)
    results = await _run(call, list(pending.items()), arguments)
    for target, text in targets:
        if target in kept:
            entry, authority = kept[target], before["authorities"][kept[target]["authority"]]
        else:
            entry, authority = stored_entry(
                results[revisions[target]], {"text_revision": digest(text)}
            )
        wording["checks"][target] = entry
        if authority is not None:
            wording["authorities"][entry["authority"]] = authority
    findings = []
    for position, (target, text) in enumerate(targets, 1):
        state, defects = judge(wording, target, text, scope)
        if state == "defective":
            findings.append(
                _finding(
                    target, defects, wording["checks"][target]["probabilities"], position
                )
            )
    if findings:
        raise WordingRejected(findings)
    return wording


def requests_digest(requests: Iterable[str]) -> str:
    """One digest for a set of request revisions, whatever their order or repetition."""
    return digest(json.dumps(sorted(set(requests))))


def _held_clean(
    before: Mapping[str, Any],
    claims: Sequence[str],
    revisions: Mapping[str, str],
    texts: Mapping[str, str],
    scope: str,
) -> list[str] | None:
    """The authorities under which these claims passed clean, or None if they did not.

    The clean claims are stored as one digest of their requests, so they are reused together
    or not at all: the digest must be the one of the requests asked now. Each authority is
    read again as for any check, with a probe entry that has every defect absent, and holds
    only if it names this client's policy and recipe.
    """
    clean = before.get(CLEAN)
    if not claims or not isinstance(clean, dict):
        return None
    keys = clean.get("authorities")
    if (
        clean.get("requests") != requests_digest(revisions[claim] for claim in claims)
        or not isinstance(keys, list)
        or not keys
        or not all(isinstance(key, str) for key in keys)
    ):
        return None
    claim = claims[0]
    for key in keys:
        probe = {
            **before,
            "checks": {
                claim: {
                    "text_revision": digest(texts[claim]),
                    "request_revision": revisions[claim],
                    "authority": key,
                    "probabilities": [0.0] * len(DEFECTS),
                }
            },
        }
        if judge(probe, claim, texts[claim], scope)[0] != "clear":
            return None
    return list(keys)


async def check_derivation(
    call: Call,
    claims: Sequence[Target],
    wording: Mapping[str, Any],
    scope: str,
    context: Mapping[str, Any],
    prior: Any = None,
) -> dict[str, Any]:
    """Check each claim the server derived; the wording record with their checks added.

    The pass is the one of `check_wording`, with the record it returned: one call per
    claim, in batches, the same scope and context, an identical request checked once. It
    never raises for a defect, because the agent did not write these texts: a defect is kept
    for the maintainer. A check that passed the gate stands for the very same request, with
    a defect or without one, so a claim is not asked again until its text, the scope or the
    context changes. A defect and a check the gate did not pass keep their whole entry in
    `derived`; the claims that passed clean are one digest in `clean`, reused together.
    Raises `PayloadTooLarge` when the host reports the request above the limit.
    """
    arguments, revisions, _ = _requests(claims, scope, context)
    texts = dict(claims)
    before: Any = prior if isinstance(prior, dict) else {}
    same = (
        before.get("scope_revision") == wording["scope_revision"]
        and before.get("context_revision") == wording["context_revision"]
    )
    stored = before.get(DERIVED) if same else None
    old: dict[str, dict[str, Any]] = stored if isinstance(stored, dict) else {}
    view = {**before, "checks": old}
    kept: dict[str, dict[str, Any]] = {}
    candidates: list[str] = []
    for claim, text in claims:
        if (
            judge(view, claim, text, scope)[0] == "defective"
            and old[claim].get("request_revision") == revisions[claim]
        ):
            kept[claim] = old[claim]
        elif claim not in old:
            # It may have passed clean: only the whole set of those is reused.
            candidates.append(claim)
    held = _held_clean(before, candidates, revisions, texts, scope) if same else None
    reused = set(candidates) if held is not None else set()
    pending = {
        revisions[claim]: claim
        for claim, _ in claims
        if claim not in kept and claim not in reused
    }
    results = await _run(call, list(pending.items()), arguments)
    authorities = dict(wording["authorities"])
    clean_keys = set(held or ())
    clean_requests = {revisions[claim] for claim in reused}
    for key in clean_keys:
        authorities[key] = before["authorities"][key]
    derived: dict[str, dict[str, Any]] = {}
    for claim, text in claims:
        if claim in reused:
            continue
        if claim in kept:
            entry, authority = kept[claim], before["authorities"][kept[claim]["authority"]]
        else:
            entry, authority = stored_entry(
                results[revisions[claim]], {"text_revision": digest(text)}
            )
        if authority is not None:
            authorities[entry["authority"]] = authority
        probe = {**wording, "authorities": authorities, "checks": {claim: entry}}
        if judge(probe, claim, text, scope)[0] == "clear":
            clean_requests.add(revisions[claim])
            clean_keys.add(entry["authority"])
        else:
            derived[claim] = entry
    record = {**wording, "authorities": authorities}
    if derived:
        record[DERIVED] = derived
    if clean_requests:
        record[CLEAN] = {
            "authorities": sorted(clean_keys),
            "requests": requests_digest(clean_requests),
        }
    return record


def derivation_diagnostics(
    wording: Any, claims: Sequence[Target], scope: str
) -> list[dict[str, Any]]:
    """The derivation diagnostics a stored record holds for the claims the host derived.

    The host reads each check as it reads the check of a text, from what the record kept,
    and only for the claims it derives itself: a record cannot diagnose a text the agent
    wrote. A check that passed the gate with a defect is `diagnosed`, with the defects and
    the probabilities. One that stayed in review is `review`: there is no defect to act on
    yet. A claim with a clean check, or with no check that holds, has no row, because the
    host does not require the check of a derived claim.
    """
    if not isinstance(wording, dict):
        return []
    view = {**wording, "checks": wording.get(DERIVED)}
    rows: list[dict[str, Any]] = []
    for claim, text in claims:
        state, defects = judge(view, claim, text, scope)
        if state == "defective":
            rows.append(
                {
                    "claim_id": claim,
                    "status": "diagnosed",
                    "defects": defects,
                    "probabilities": dict(
                        zip(DEFECTS, view["checks"][claim]["probabilities"], strict=True)
                    ),
                }
            )
        elif state == "review":
            reason = view["checks"][claim].get("reason")
            rows.append(
                {
                    "claim_id": claim,
                    "status": "review",
                    "defects": [],
                    **({"reason": reason} if isinstance(reason, str) else {}),
                }
            )
    return rows


def diagnosed_defects(diagnostics: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """The defects of each claim whose derivation has a diagnosis, by claim id."""
    return {
        row["claim_id"]: list(row["defects"])
        for row in diagnostics
        if row["status"] == "diagnosed"
    }
