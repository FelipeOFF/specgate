---
name: to-tickets-jev
description: Turn an approved specification into a validated graph of vertical tickets and publish it through project-selected trackers. Use after to-spec-jev approval, including deliveries that span multiple repositories.
---

# To Tickets with Jev

Load the installed `to-tickets` skill and preserve its decomposition guidance.
Read the approved spec revision, active decisions, domain vocabulary, repository
boundaries, and the project's chosen destinations.

Draft vertical behavior slices. Every ticket must:

- own one observable behavior and its acceptance criteria;
- name exactly one `owner/repository`;
- cover explicit spec scope without implementation paths or code;
- list only real ticket IDs in `blocked_by`.

Before review, use `jev_verify` to check complete scope coverage, verticality,
missing blockers, and whether the graph can ship in topological order. Reject
duplicate IDs, duplicate blockers, missing nodes, cycles, uncovered scope, or
horizontal layer tickets. Keep gaps visible and do not publish a rejected graph.

Cross-repository blockers remain edges in the delivery graph. When combined
behavior needs independent validation, add one `integration: true` ticket that
depends on completed behavior from at least two repositories. Do not combine
implementation for multiple repositories in one ticket.

GitHub, GitLab, Jira, Beads, local documents, and a free-form workflow are
project choices handled by the authorized harness adapter. Read
`.specgate/tracker.json` (`{"destination":"github"}`). A missing file keeps
GitHub. GitLab, Jira, and Beads publish only through an injected adapter.
Local documents and the free-form workflow do not use tracker credentials.
The MCP stores the graph, origins, revisions, and public references; it does
not require tracker credentials or silently choose a destination.

Present one consolidated human review before publication. Approval records the
exact graph revision, relation capabilities, and publication authorization.
Rejection keeps the graph unpublished and returns to decomposition.

Close that review with a mermaid flowchart. One node per ticket, with its
title and repository. Draw an edge from each blocker to the ticket it blocks.
A ticket with an empty `blocked_by` has no incoming edge.

On GitHub, leave `native_dependencies` enabled. Publication adds each
`blocked_by` link and reads the dependencies back; the ticket counts as
published only when those links are present. Tell the person the edges are
those GitHub blocked-by links. The issue body may repeat the same edges as a
reading list.

Keep the graph revision inside the approval tool call. The message ends on
the diagram.

Publish with a stable idempotency key. Reconcile an uncertain response before
retrying, reject ambiguous duplicate references, and never create a second item
for the same ticket revision. If the project changes destination, preserve the
previous graph and its public references in history before publishing the new
revision.
