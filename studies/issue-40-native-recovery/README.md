# Issue 40: exact T089 native recovery

This is a separate Issue-scoped recovery record based on the reviewed Issue 39 Stage-0 commit `d4ab62b6172a14da2969ba48867a564d663008c3`. The copied `audit_stage0.py` is byte-identical to the reviewed Issue 39 script; its outputs are written here so the blocked Issue 39 snapshot remains unchanged.

The recovery restores only the pinned native source dependencies and a fresh native build. It does not modify STSRL policy/spec/artifacts or simulator source, regenerate targets, train a model, or run Stage 1. `native-recovery.json` records exact source, submodule, toolchain, extension path/hash and restoration commands. `recovery-manifest.json` records hashes for the Issue 40 evidence files.

The smoke is permitted only if the copied audit reports `STAGE0_READY_FOR_SMOKE` and the loaded native extension resolves to the exact build recorded in `native-recovery.json`. Any smoke must use the predeclared eight original-order held-out roots and frozen T089 identities; it must not be extended to Stage 1.
