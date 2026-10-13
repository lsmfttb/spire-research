# Snapshot guard correction

Ordinary implementation repair of Reviewer finding #6136956243. Original
Phase A seal and its input/output files remain unchanged and describe original
commit `9e3b4c6f531b86f7d2e94d1098fc5ad22a79bb27`. The additive repair manifest
describes the corrected source and verification receipts at the new handoff.

`mapped_action` requires a fresh `current_public` snapshot from the same native
boundary as `current_candidates`. It rejects a missing snapshot, changed public
observation or choice set, or a candidate list inconsistent with that snapshot,
before matching executor indices. Guard comparisons remain external to policy
features. Supplying an old snapshot as if it were current violates the caller
contract; this function cannot obtain the native boundary on its own.

Retained native transitions establish two concrete identity-reuse regressions:
Anger group 0/card 0 becomes Cleave group 0/card 0 after taking the old group;
Blood Potion slot 0 becomes Strength Potion slot 0 after discard and pickup.
Both stale choices are rejected. All current fixture actions still resolve,
and all candidate features match the original prototype exactly.

No native execution, new games, source owners, Q labels, model training or
scientific variant was introduced. Run `verify_snapshot_guard.py ORIGINAL_STORE
REPAIR_STORE`, `test_prototype.py REPAIR_STORE`, and `visibility_checks.py
REPAIR_STORE ORIGINAL_STORE` under Python 3.14. Original reward-field visibility
limits, old split confounds and study-only/no-promotion disposition remain.
