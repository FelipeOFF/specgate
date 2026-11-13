# Verification of the graph

Each blocking claim needs `verified`, confidence and support strictly above the greater
of 0.80 and the frozen workflow threshold. An average cannot clear a weak claim,
with one fixed, symmetric exception: a claim whose confidence or support is within
0.05 of that cutoff, above or below, is evaluated once more by resending the
identical provider request (same claims in the same order, evidence and context),
and the mean of the two decides. Keep only the rows of the claims inside the band
as second evaluations; a claim farther away keeps its first evaluation alone, and
no claim is evaluated a third time. The host recomputes the decision from the
stored rows, requires both evaluations of a claim to carry the same
`request_revision`, and records both evaluations, the mean and the rule under the graph's
`verification.confirmation`.

Keep a verdict between two verifications only while the claim text, the evidence
(the approved spec and the whole graph, research sources included) and the context
are exactly those the earlier verification judged. A strong claim keeps its
verdict. A weak claim keeps its weak verdict and returns to `jev_verify` only
with new content in the claim, the evidence or the context, or through the symmetric
confirmation when the host cannot use its pair; it is never sent alone again.
Every claim receives the whole graph, so editing a ticket judges all of them again.
The host derives `verification.reuse` from the stored rows (`reused`, `retained`, `evaluated` with
its reason, `held`), ignores a client-provided one, and holds the graph with
`verification_reuse_mismatch` when a row judged on another input is carried onto
this one or a weak claim is judged again with nothing changed. It compares each
claim with the latest stored verification that judged it, so leaving a claim out clears
neither its verdict nor its hold. The return through the
symmetric confirmation is allowed once per input, only for a near claim without a
usable pair, as a new first evaluation with its own second one, and never after a
contradicted verdict.
