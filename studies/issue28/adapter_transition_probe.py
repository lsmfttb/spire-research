"""Study-only STSRL adapter compatibility probe for Issue #28."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stsrc", type=Path, required=True)
    parser.add_argument("--module-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=49)
    parser.add_argument("--ascension", type=int, default=20)
    args = parser.parse_args()
    sys.path.insert(0, str(args.module_dir.resolve()))
    sys.path.insert(0, str(args.stsrc.resolve()))

    import slaythespire
    from sts_combat_rl.sim.lightspeed import LightSpeedAdapter

    adapter = LightSpeedAdapter(
        seed=args.seed, ascension=args.ascension, module=slaythespire
    )
    try:
        snapshot = adapter.reset(seed=args.seed)
        actions = adapter.legal_actions(snapshot)
        if not actions:
            raise RuntimeError("native adapter returned no root legal actions")
        transition = adapter.step(actions[0])
        raw_projection = adapter._sim.public_projection()
        print(
            "transition=pass"
            f" legal_actions={len(actions)}"
            f" selected_kind={actions[0].kind}"
            f" next_screen={transition.snapshot.raw.get('screen_state')}"
            f" native_projection_schema={raw_projection.get('schema_id')}"
        )
        try:
            parsed = adapter.public_projection(transition.snapshot)
        except (RuntimeError, ValueError) as exc:
            print(f"adapter_projection=rejected {type(exc).__name__}: {exc}")
        else:
            print(f"adapter_projection=accepted schema={parsed.schema_id}")
        return 0
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
