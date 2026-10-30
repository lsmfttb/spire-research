from __future__ import annotations
import argparse, importlib.util, json, subprocess, sys
from pathlib import Path

REPO=Path(__file__).resolve().parents[2]
PACKAGE_SHA="6771374736cd1589b59b79100f383414b070e209"

def load_driver(path: Path, name: str):
 spec=importlib.util.spec_from_file_location(name,path)
 if spec is None or spec.loader is None: raise RuntimeError(f"cannot load driver: {path}")
 module=importlib.util.module_from_spec(spec); sys.modules[name]=module; spec.loader.exec_module(module)
 return module

def disable_simulator(package):
 def forbidden(*_args, **_kwargs):
  raise RuntimeError("analysis-only closeout refuses all simulator dispatch")
 package.D.run_units=forbidden

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument("--package-worktree",type=Path,required=True)
 package_worktree=parser.parse_args().package_worktree.resolve()
 head=subprocess.run(["git","rev-parse","HEAD"],cwd=package_worktree,check=True,text=True,capture_output=True).stdout.strip()
 if head!=PACKAGE_SHA: raise RuntimeError(f"package source worktree is not pinned to {PACKAGE_SHA}: {head}")
 package_driver=package_worktree/"studies/issue-81-pg-only/driver.py"
 package=load_driver(package_driver,"issue81_pinned_package_driver")
 optimized=load_driver(REPO/"studies/issue-81-pg-only/driver.py","issue81_streaming_closeout_driver")
 disable_simulator(package)
 package.elite_note=optimized.elite_note
 result=package.run_missing_heldout_continuation()
 print(json.dumps(result,sort_keys=True,separators=(",",":")),flush=True)
 if result.get("status")!="COMPLETE": raise RuntimeError(f"closeout did not complete: {result.get('status')}")

if __name__=="__main__": main()
