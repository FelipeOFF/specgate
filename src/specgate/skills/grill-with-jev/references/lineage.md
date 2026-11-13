# Lineage of a stored question

A question the grill already stored (`pending` or `stale`) is reformulated or split by
declaring `lineage` (`{original, kind}`, with `reformulation` or `split`) on each part, with
`lineage_action` (`{action, reason}`, the reason not empty) and, for a division, the other
parts in `siblings` (two to ten parts, the first in `question`). Submit the reformulation or
division to `jev_equivalence` against the original before sending it, and send the parts when
it returns `accept`. The host submits the parts together to the same check, and
`equivalence_rejected` (a part adds, or the parts lose, something the original says; its
`next_actions` is `revise_parts`) asks you to revise the parts. An equivalence left in review
(`action=review`) accepts nothing and finds no defect: do not reword the parts to draw another
result. Send them, and the host keeps the turn for a person (`decision_equivalence_review`).
