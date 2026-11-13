# Payload limit

A `payload_too_large` result (`error.code`, with `size` and `limit` in bytes) means the
request passed the host's 64 KB limit (or its 256 KB HTTP body), not that Jev or the
provider failed. Stop in review and show the code, the size and the limit; never retry
it unchanged, raise the limit or spend another research cycle on it. Shrink the request
(fewer or shorter sources) and resume the same idempotency key: a reservation already
made resumes without a new one. After admission it stays in evaluation; before it, the
client has already reported the size to the host (sizes and ids, never content), which
keeps the reservation in `collecting` for that same key to collect again. Never collect
again from a bare `collecting` reservation: without that report it is an unknown
outcome.

A spec body above 48 KB is `spec_too_large`, a limit of the spec itself: split the delivery
into several specs, each with its own grill run.
