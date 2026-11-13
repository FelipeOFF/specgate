# Delegated rewrite of an approved decision

Never rewrite or split a decision of an approved spec on your own. Only
`SpecClient.rewrite_decisions` does it (`spec_submit` with `rewrite`), and only while a
delegation in force includes the operation `rewrite_decisions`, which no publication,
score or delegated approval grants: the person adds it with
`specgate setup --allow-rewrite`. The client reads that grant with
`specgate.delegation.rewrite_authority` before asking for any text and again right
before writing (`rewrite_not_authorized` when it is expired, revoked, edited or lacks
the operation). The host writes only if each original's equivalence passed the gate and
only the implementation decisions and their `decision_lineage` changed
(`equivalence_rejected`, `rewrite_equivalence_unverified`, `rewrite_out_of_scope`). The
rewrite approves nothing: the revision returns to review, the ticket graph becomes
stale, and the pull request records the rewrite with the original decision
(`## Reescritas delegadas`). Without the grant the defective decision goes to the
person as an exception; an ordinary synthesis over an approved spec is no way around
it.
