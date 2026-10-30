from __future__ import annotations
import hashlib, importlib.util, json, os, subprocess, sys, time
from pathlib import Path

REPO=Path(__file__).resolve().parents[3]
STUDY=REPO/"studies/issue-83-pgonly-scale"
DATA=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue83")
ART=DATA/"artifacts"
FORMAL=ART/"formal/3951ee0749d8e9c3dfcba33590ceb1f880047acc"
SOURCE="3951ee0749d8e9c3dfcba33590ceb1f880047acc"
LOCK="224c7a6e2cbd46d986294e68b31a07ec6518ea6f82eda4636ae0c98ef4fa63b2"
NATIVE="bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad"
ALLOWANCE=67272.0
PYTHON=Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/bin/python3.13")

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def canon(x): return json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def write_once(p,x):
 p=Path(p); p.parent.mkdir(parents=True,exist_ok=True); b=canon(x)+b"\n"
 if p.exists():
  if p.read_bytes()!=b: raise FileExistsError(str(p))
 else:
  with p.open("xb") as f: f.write(b); f.flush(); os.fsync(f.fileno())
 return hashlib.sha256(b).hexdigest()
def load_driver():
 s=importlib.util.spec_from_file_location("issue83_driver_coverage_witness",STUDY/"driver.py")
 m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); m.configure_driver(); return m

def base_adapter(adapter):
 cur=adapter; seen=set()
 while id(cur) not in seen:
  seen.add(id(cur))
  if getattr(cur,"_sim",None) is not None: return cur
  cur=getattr(cur,"base",None)
  if cur is None: break
 raise RuntimeError("cannot resolve native LightSpeedAdapter")

def capture(runner,controller,adapter,snapshot,actions,context,step,error,out):
 from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions
 sim=runner.native_simulator(adapter)
 proj=controller.module.PrivilegedNoncombatProjectionAccess(sim,"training_oracle_only").project_v1()
 valid=runner.validate_live_native_candidate_bindings(proj,actions)
 ids=action_identity_dicts_for_actions(actions); base=base_adapter(adapter)
 before=runner._snapshot_debug_fingerprints(base,snapshot); cp=base.capture_checkpoint(snapshot); effects=[]
 try:
  for i in range(len(actions)):
   restored=base.restore_checkpoint(cp); restored_fp=runner._snapshot_debug_fingerprints(base,restored)
   live=base.legal_actions(restored); live_ids=action_identity_dicts_for_actions(live)
   if canon(live_ids)!=canon(ids): raise RuntimeError(f"legal occurrence order changed at {i}")
   tr=base.step(live[i]); nxt=[] if tr.terminal else base.legal_actions(tr.snapshot)
   effects.append({"candidate_index":i,"action_identity":ids[i],"binding":proj["candidates"][i].get("candidate_binding"),
    "restore_matched_decision_state":restored_fp==before,"legal_order_unchanged":True,"terminal":bool(tr.terminal),
    "transition_info":dict(tr.info),"next_state_fingerprints":runner._snapshot_debug_fingerprints(base,tr.snapshot),
    "next_public_projection":runner._public_projection_payload(base,tr.snapshot),
    "next_legal_action_identities":action_identity_dicts_for_actions(nxt)})
 finally: base.restore_checkpoint(cp)
 write_once(out,{"schema_id":"issue83-native-coverage-witness-v1","classification":"DIAGNOSTIC_ONLY_NOT_FORMAL_TERMINAL",
  "source_commit":SOURCE,"source_lock_sha256":LOCK,"native_module_sha256":NATIVE,"owner_seed":8301831,
  "policy_rng_seed":378609158,"step_index":int(step),"screen_state":str(context.screen_state),
  "state_fingerprints":before,"public_projection":runner._public_projection_payload(base,snapshot),
  "coverage_error":{"type":type(error).__name__,"message":str(error)},"projection":proj,
  "live_action_identities_in_native_order":ids,"binding_validation_result":valid,
  "one_step_native_effects_from_identical_restored_state":effects,
  "all_legal_orders_preserved":all(x["legal_order_unchanged"] for x in effects)})

def run_worker(case,job_path,witness):
 c=load_driver(); original_load=c.D.load_modules
 def diagnostic_load(root,*,pilot):
  loaded=original_load(FORMAL,pilot=pilot)
  _,runtime,_,_,executor=loaded
  executor.ARTIFACT_ROOT=Path(root)
  original_init=runtime.initialize_native_runtime
  def diagnostic_init(*a,**kw):
   cfg,runner,module,encoder=original_init(*a,**kw)
   cls=runner.StudyController; original_select=cls.select_action
   if not getattr(cls,"_coverage_witness_installed",False):
    def select(self,adapter,snapshot,actions,context,step):
     try: return original_select(self,adapter,snapshot,actions,context,step)
     except BaseException as exc:
      if int(self.seed)==8301831 and "native projection does not support every live legal candidate" in str(exc):
       capture(runner,self,adapter,snapshot,actions,context,step,exc,witness)
      raise
    cls.select_action=select; cls._coverage_witness_installed=True
   return cfg,runner,module,encoder
  runtime.initialize_native_runtime=diagnostic_init
  return loaded
 c.D.load_modules=diagnostic_load
 return int(c.worker_main(job_path))

def prepare(case,root):
 seed=8301831 if case=="coverage-8301831" else int(case.removeprefix("replay-"))
 original=FORMAL/f"units/TRAIN-{seed}/attempt-0001/worker-job.json"
 job=json.loads(original.read_text()); attempt=root/"attempt-0001"; native=attempt/"native"; unit=f"DIAGNOSTIC:{case}"
 row=dict(job["row"]); row.update({"unit_id":unit,"formal":False,"cohort":"diagnostic-only","paired_owner_key":f"diagnostic-{seed}"})
 job.update({"artifact_root":str(root),"attempt_dir":str(attempt),"output_dir":str(native),"result_path":str(attempt/"result.json"),
  "run_id":f"{unit}-attempt-0001","unit_id":unit,"row":row,"package_commit":SOURCE,"runner_code_commit":SOURCE,
  "source_lock_sha256":LOCK,"diagnostic_only":True})
 return attempt,job

def preflight():
 c=load_driver(); lock=c.check_lock(); clock=c.read_json(ART/"active-wall-formal.json"); close=c.read_json(FORMAL/"active-wall-closeout.json")
 if clock.get("active_session") or clock.get("process_ids"): raise RuntimeError("formal clock active")
 print(json.dumps({"status":"PREFLIGHT_PASS","source_commit":c.current_source_commit(),"lock_sha256":sha(STUDY/"source-lock.json"),
  "runtime_identity":lock["runtime_identity"],"wall_seconds":clock["elapsed_seconds"],"closeout_sha256":sha(FORMAL/"active-wall-closeout.json"),
  "prior_unmeasured_status":close["post_clock_closeout_active_wall_status"]},indent=2)); return 0

def supervisor(case):
 c=load_driver(); c.check_lock()
 if c.current_source_commit()!=SOURCE or c.file_sha256(c.NATIVE)!=NATIVE: raise RuntimeError("original source/runtime lock mismatch")
 cp=ART/"active-wall-formal.json"; prior=c.read_json(cp); closep=FORMAL/"active-wall-closeout.json"; close=c.read_json(closep)
 if prior.get("active_session") or prior.get("process_ids"): raise RuntimeError("global clock is active")
 baseline=float(prior["elapsed_seconds"])
 if close.get("allowance")!=ALLOWANCE or close.get("post_clock_closeout_active_wall_status")!="UNMEASURED" or baseline<float(close["active_wall_seconds"]):
  raise RuntimeError("global clock baseline/closeout is inconsistent")
 script=Path(__file__).resolve(); sh=sha(script); root=DATA/"diagnostics/native-coverage-witness"/f"{case}-{sh[:12]}"
 if root.exists(): raise FileExistsError(f"refusing to overwrite {root}")
 attempt,job=prepare(case,root); attempt.mkdir(parents=True); write_once(root/"worker-job.json",job)
 witness=root/"native-coverage-witness.json"; logpath=root/"worker.log"; logpath.touch(exist_ok=False)
 env=os.environ.copy(); env["STS_LIGHTSPEED_SOURCE_DIR"]=str(c.STS_SOURCE); env["STSRL_SOURCE_DIR"]=str(c.STSRL_SOURCE)
 clock=c.D.ActiveClock("formal",ALLOWANCE); child=None
 try:
  with logpath.open("ab",buffering=0) as log:
   child=subprocess.Popen([str(PYTHON),str(script),"worker",case,str(root/"worker-job.json"),str(witness)],cwd=str(REPO),env=env,
    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
   clock.set_processes({child.pid}); code=child.wait()
 finally: total=clock.close()
 receipt={"schema_id":"issue83-coverage-diagnostic-wall-receipt-v1","classification":"counted_in_original_global_allowance; not_formal_terminal",
  "case":case,"script_sha256":sh,"prior_global_wall_seconds":baseline,"global_wall_seconds_after":total,
  "diagnostic_active_wall_seconds":total-baseline,"allowance_seconds":ALLOWANCE,"old_formal_closeout_sha256":sha(closep),
  "old_post_clock_status_remains":"UNMEASURED","clock_path":str(cp),"closed_clock_sha256":sha(cp),
  "worker_exit_code":code,"result_path":str(attempt/"result.json"),"witness_path":str(witness) if witness.exists() else None}
 write_once(root/"diagnostic-wall-receipt.json",receipt); print(json.dumps(receipt,indent=2)); return code

def main():
 if len(sys.argv)==2 and sys.argv[1]=="preflight": return preflight()
 if len(sys.argv)==5 and sys.argv[1]=="worker": return run_worker(sys.argv[2],Path(sys.argv[3]),Path(sys.argv[4]))
 if len(sys.argv)!=2: raise SystemExit("usage: native_coverage_witness.py preflight|coverage-8301831")
 return supervisor(sys.argv[1])
if __name__=="__main__": raise SystemExit(main())
