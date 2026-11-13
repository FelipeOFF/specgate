# Payload limit

A `payload_too_large` result (`error.code`, with `size` and `limit` in bytes) means the
request passed the host's 64 KB limit (or its 256 KB HTTP body), not that Jev or the
provider failed. Stop in review and show the code, the size and the limit; never retry
it unchanged, raise the limit or spend another research cycle on it. Shrink the request
(fewer or shorter sources) and resume the same idempotency key: the reservation resumes
without a new one. After admission it stays in evaluation; before it, the client has
reported the size (sizes and ids, never content) and the host keeps the reservation in
`collecting` for that key to collect again. Never collect again from a bare `collecting`
reservation: without that report the outcome is unknown.
