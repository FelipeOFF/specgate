---
name: research-filter-jev
description: Filter a research-with-jev packet against its question or claim with Jev, preserving relevant, contradictory, rejected and repeated evidence.
---

# Research filter with Jev

Use this after `research-with-jev` has saved a `schema=1` collected packet. Reuse
the question, claim, selected topic, source metadata and captured text exactly as
recorded. Provide earlier collected packets for the same question/claim with
`--previous` so repeated content, excerpts and source revisions cannot masquerade
as material novelty.

```sh
specgate research-filter-jev docs/research/COLLECTED_HASH.json \
  --previous docs/research/OLDER_HASH.json --project . \
  --host https://YOUR_MCP/mcp --authorize-jev
```

Obtain authorization to send the captured source text, question and claim to the
configured MCP host/provider. Reuse authorization already granted for that scope;
`--authorize-jev` records it but does not create consent. Use the existing API-key
environment configuration. The command sends each new item to `jev_screen` for
pertinence and, when a claim exists, to `jev_verify` for support or contradiction.
Jev calls are attached and are not a new research loop. Contradictions remain in
`selected` even if a favorable source exists; injection blocks remain rejected.
An item counts as evidence only when its Screen and Verify results both pass their
tool's predicate under the host's basis: `gate.basis=confidence_policy` with
`calibrated=false` and a `gate.policy_binding` the client confirmed against the
captured text, or `validated_manifest`. A result without `gate.basis` stays closed.

The result is a hash-addressed JSON file under `docs/research/filtered` by default.
It contains full provenance and verdicts for selected and rejected items, explicit
gaps when no pertinent new evidence exists, and the input/previous packet history.
Keep the file and source snapshots in the repo/workspace. The filter leaves
`claim_status=unresolved`; the score is an uncertainty filter, and neither it nor the
filter result authorizes advancing, publishing or claiming calibration or accuracy.
Review the evidence and contradictory claims through the existing workflow before
any later decision.
