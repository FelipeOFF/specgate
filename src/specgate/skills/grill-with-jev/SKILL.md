---
name: grill-with-jev
description: Grill the scope with the person through Matt Pocock's grill-with-docs, then record each decision the person made in the Specgate host so to-spec-jev can cite it. Jev takes no part in the grill. Use when clarifying scope before writing a spec.
---

# Grill with Jev

The grill is Matt Pocock's `grill-with-docs`. The person makes every decision. Jev does not
choose between alternatives in the grill, and it does not check the wording of a question.
Jev starts at `to-spec-jev` and `to-tickets-jev`. This skill adds one step to
`grill-with-docs`: it records the decisions in the Specgate host, because the spec cites
exactly the decisions of its grill run.

## Run grill-with-docs

`grill-with-docs` loads two skills, `grilling` and `domain-modeling`. When the person invoked
`/grill-with-docs`, follow it. Otherwise load those two skills yourself, as it does: the
harness does not let a model invoke `grill-with-docs`. They come from `mattpocock/skills`,
not from the Specgate set. When one of them is missing, stop and ask the person to install
them:

```sh
npx skills add mattpocock/skills --global --skill grill-with-docs grilling domain-modeling
```

Follow them as written: rounds over the frontier, your recommended answer on each question,
facts you look up instead of asking the person, and `CONTEXT.md` and ADRs written as terms
and decisions settle. In the grill, never call `jev_decide`, `jev_check`, `jev_equivalence`,
`grill_research_begin` or `grill_research`, and never send the person's alternatives to Jev.

The grill ends when the frontier is empty and the person confirms the shared understanding.

## Record the decisions

After that confirmation, record the decisions in the host, one grill run per spec:

1. Write each decision as one atomic sentence in the proposed mode, saying what the change
   will do, with the person's choice and nothing beyond it. Show the list as the shared
   understanding the person confirms.
2. Build the context packet from the authorized sources the grill read, with the
   `CONTEXT.md` and the ADRs it wrote. Set each evidence revision to the sha256 of that
   evidence text. Exclude secrets, session transcripts and material outside the authorized
   project roots. A gap, conflict or unexamined item that remains goes in the packet, and it
   blocks `finish`: never hide one.
3. Call `grill_start` once per spec with the objective and that packet. Record the returned
   `run_id` in the project's grill record at once. Resume it with `grill_get`; never call
   `grill_start` again for the same spec.
4. For each decision, call `grill_continue` with `question_type="open"`, no `options`, a
   stable `id` (the decision's slug) and the question as the grill asked it. The host never
   sends an open question to Jev, so no wording check and no choice run. The turn waits for
   the person (`pending`).
5. Answer that turn with `grill_answer`: the current run revision, `pending.id` (not the
   question id), a new idempotency key and the decision sentence in `answer.text`. For an
   authorization, set `requires_authorization=true` on the question and send
   `authorization_granted` exactly as the person granted or refused it. Confirm the turn is
   `resolved` with `origin=human` before the next decision. A revision increment alone is not
   success: reconcile with `grill_get` and report the inconsistency.
6. Call `grill_continue` with `finish=true`, then `grill_summary`. Show a compact list of
   each question, its decision, the evidence sources and the gaps, and hand off to
   `to-spec-jev`.

On a transport failure, retry the identical request with its original key or reconcile with
`grill_get`; never loop over new keys. A delivery too large for one spec (`spec_too_large`,
above 48 KB) becomes several specs, each with its own run.

## Back to the grill

`to-spec-jev` sends the delivery back with `new_grill_question`, and `spec_reject` reopens the
grill. Grill that subject with the person as above and record it on the same run. A new
decision gets a new id. A changed decision keeps its id and brings the evidence that changed
it, such as the ADR or the `CONTEXT.md` entry the grill wrote: the host refuses the same
packet again (`new_evidence_required`). Then finish again, because the spec stays stale until
the grill is complete.

A run started before this change can hold a pending turn. Ask the person and answer it with
`grill_answer`. A turn the policy held (`context_incomplete`, with `agent_action` on the
pending turn) is yours: repair the packet and resend it with the same `question.id`, as an
open question.

## Disclosure

Before `grill_start`, check existing authorization for this delivery and the setup
delegation. Reuse a valid disclosure grant covering these project sources and the Specgate
host. A new destination or wider source scope requires a new grant. Never send secrets or
harness transcripts.

## Codes

| Code | What to do |
| --- | --- |
| `context_mismatch` | The packet's `objective` must be the run's objective. |
| `pending_review` | Answer the pending turn before the next decision or `finish`. |
| `pending_mismatch` | Read the current `pending.id` with `grill_get` and answer that turn. |
| `invalid_answer` | Send the decision sentence in `answer.text`. |
| `authorization_required` | Send `authorization_granted` as the person granted or refused it. |
| `new_evidence_required` | A changed decision needs the evidence that changed it. |
| `context_incomplete` | A held turn: repair the packet and resend the same `question.id`. |
| `incomplete_grill` | `finish` names what blocks: a gap, a pending turn or an authorization. |
| `spec_too_large` | Split the delivery into several specs, one run each. |

## Dead end

A turn that waits for the person is a pause, not a dead end: ask once and send the answer
through `grill_answer`. The grill is at a dead end only when `finish` stays refused (`incomplete_grill` names what
blocks) and you have nothing left to send:

1. Keep the grill open: never force `finish=true` or open another run.
2. Report each item that blocks and what it needs: a fact, an authorization, an answer.
3. Offer only the exits inside Specgate: the person's answer through `grill_answer`, or the
   fact that clears the gap.
4. End with `feedback_submit` with `stage=grill` and `outcome=blocked` (below).

Never say Jev or Specgate is down unless a tool returned a provider or transport error.

## Feedback

When the recording ends, finished or at a dead end, call `feedback_submit` once with
`stage=grill`, the `outcome` (`completed` or `blocked`) and an honest review of the tool:
what worked, the difficulties, the error codes you saw and the tools involved. A pause for
the person's answer records none. Never include transcripts, secrets, ids or paths of a
harness session, or the person's words verbatim. On a host without `feedback_submit`, go on
without it. A feedback refusal never blocks the flow.
