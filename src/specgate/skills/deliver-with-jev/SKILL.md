---
name: deliver-with-jev
description: Take one delivery from the grill to draft pull requests in a single invocation, running grill-with-jev (the person's grill through Matt Pocock's grill-with-docs), to-spec-jev, to-tickets-jev and implement-spec-jev in that order and resuming from the state the Specgate host keeps. Use to run or resume a whole delivery.
---

# Deliver with Jev

Run one delivery through four phase skills, in this order, in this harness conversation:

1. `grill-with-jev`
2. `to-spec-jev`
3. `to-tickets-jev`
4. `implement-spec-jev`

The grill is the person's: `grill-with-jev` runs Matt Pocock's `grill-with-docs`, the person
makes every decision and the skill records them in the host. Jev does not choose between
alternatives in the grill; it takes part from `to-spec-jev` on.

For each phase, load its skill and follow it. The phase skill owns the work of its phase: its
tools, its codes, its pauses and its dead end. This skill only picks the phase, moves to the
next one and stops where a phase skill stops. It needs no `FlowAdapters` factory and does not
run `specgate flow`.

## Resume from the host's state

The host keeps the delivery's state. At every invocation, before any work:

1. Take the grill `run_id` of this delivery from the person's request or from the grill record
   the project keeps, and call `grill_get` with it. A delivery with no run is still in its
   grill: `grill-with-jev` records the decisions only after the person confirms the shared
   understanding, so run it, reading the `CONTEXT.md` and ADRs the grill already wrote.
2. Continue that grill. One grill run feeds one spec, as the phase skills say: never call
   `grill_start` again for the same spec, and never open another run to start over. Only a
   spec with no run starts one, through `grill-with-jev`; record the returned `run_id` in the
   project's grill record at once, so the next invocation resumes the same run. A delivery
   split by `spec_too_large` has one run per spec, and the grill record keeps each `run_id`.
   The run whose spec was split leaves the grill record once the new specs have their own
   runs: resume only those, and pick the phase of each below.
3. Pick the phase from the state the host returns, and read it with the phase's query tool.
   The first row that matches, from top to bottom, decides:

| Host state | Phase |
| --- | --- |
| no run, a `pending` turn, or `stage=grill` | `grill-with-jev`, with `grill_get` |
| `grill_complete`, no spec or a `stale` one, `spec_review`, `spec_ready`, `spec_publishing` | `to-spec-jev`, with `spec_get` |
| `spec_published`, no graph or a `stale` one, `tickets_review`, `tickets_ready`, `tickets_publishing`, `tickets_publication_review` | `to-tickets-jev`, with `tickets_get` |
| `tickets_published` | `implement-spec-jev`, with the `implementations` of `tickets_get` and `tickets_frontier` |

Never pick a phase from this conversation or from local files alone: the host's state decides.
The delivery ends when `implement-spec-jev` ends, at its draft pull requests and its
consolidated exception report.
A phase may send the delivery back (a `new_grill_question` or a `spec_reject` reopens the
grill, a delegated rewrite makes the graph stale): read the state again and follow the table.

## Move on without calling the person

When a phase ends, call `grill_get` again and start the next phase in the same invocation. Do
not ask the person to confirm, to invoke the next skill or to continue between phases. The
grill itself is an interview: ask its rounds as `grill-with-docs` says, and wait for the
person's answers.

Reuse the standing grant of `specgate setup` and the authorizations the person already gave
for this delivery; never create, widen or renew one. A passing gate only recommends:
`execution_authorized` stays `false`, and a score never replaces the grant.

## Pauses

Stop only where a phase skill already requires it, and name the phase and what it waits for:

1. **A question only the person can answer**: every decision of the grill, a business choice,
   a personal fact, an authorization, or a turn the phase skill hands to a person. Ask once,
   as that skill says.
2. **A review without a grant in force**: the spec, the tickets or the draft pull requests,
   when the standing grant or the person's approval does not cover the exact revision. Present
   it for review, as the phase skill says, and stop.
3. **A dead end**: the `Dead end` section of the phase skill, or the consolidated exception
   report of `implement-spec-jev`. Keep the artifact unpublished, report each item that blocks
   and what clears it, and offer only the exits inside Specgate that section lists. Never say
   Jev or Specgate is down unless a tool returned a provider or transport error.

After a pause, the next invocation resumes from the host's state.

## Never

- Never merge, remove the draft status, deploy or force-push: the delivery ends at draft pull
  requests.
- Never run `specgate setup`: only the person grants, widens or revokes authority.
- Never pass `--authorize-drafts` on your own: only the person authorizes the drafts.
- Publish the spec and the tickets only through Specgate, as `to-spec-jev` and `to-tickets-jev`
  say, and open a draft pull request only as `implement-spec-jev` says. Never publish a spec, a
  ticket or a pull request outside Specgate, and never offer or recommend that, not even when
  the person asks for a way out.
- Never change a rule of a phase skill, or a technical gate, to get past a pause.

## Feedback

Each phase skill calls `feedback_submit` once when it ends; this skill sends none of its own.
