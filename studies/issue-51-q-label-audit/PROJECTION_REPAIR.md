# Public trace projection repair

Addresses Reviewer finding 6139802574.
Original handoff: `a5381f256c433f1a10d8f5981a28c8453f67c3c2`.
Repair producer: `2be9425ba3e7fb31f011831b981520c098902191`.

The accepted public-action convention retains action kind and legal-option
indices while removing the opaque native executor integer. Seventy-two labels
were corrected from existing derived examples. Every other field, example,
resource, receipt/hash, driver outcome and owner/ordinal association is identical.
Recursive export assertions reject opaque bits and private state fields;
map/discard labels, semantic card labels and nested rejection checks pass.
Future trace exports apply the same guard before writing public examples.

Canonical corrected examples: `projection-repair/public_examples.json`.
The original flawed examples are immutable provenance, audit-only; the original
seal and all original scientific/output hashes remain unchanged:
`b586f6ac2c9bf6e30b4525941aa2674ae73af2226c8eb5bfd21793bd5b281a3d`.

New additive store:
`/home/lsmft/.cache/issue51-q-label-audit/projection-repair-2be9425b`.
New seal: `6375c94302707445da9638a4f4b37cee23bce677c6f0a968a5ce4620a7d606f5`.
Measured Python repair CPU: 0.043444856 seconds; 40 additional upper seconds
cover setup, hashing, failures, publication and cleanup. Cumulative Builder
upper: 451.773590768 seconds, with 1000 reserved Reviewer seconds of 3000.
No trace replay, simulation, new labels or learning.

Planner comment 6139666995 is respected: the original incumbent-swap outline
remains unexecuted and is not an automatic successor; standing Issue1's unmet
evaluation matrix has priority. This is an interpretation clarification,
without changes to the Phase A statistics or execution.

Consumer: Issue51 independent Reviewer then Planner, review/delete 2026-11-14.
Original #46/#47 raw retention remains November12/13. STUDY_ONLY, no merge or
promotion; return independently reviewed evidence to Planner.
