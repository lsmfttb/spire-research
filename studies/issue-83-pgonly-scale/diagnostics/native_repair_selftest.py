from __future__ import annotations
import copy, hashlib, importlib.util, json, sys
from pathlib import Path
from types import SimpleNamespace
REPO=Path(__file__).resolve().parents[3]
STUDY=REPO/"studies/issue-83-pgonly-scale"
DATA=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue83")
WITNESS_SHA="1fda4111128fc5915ab70d3087b3c3ef015a8de0aadbff9e1e8de7e1a4d77ebc"
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def canon(x): return json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def write_once(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);raw=canon(x)+b"\n"
 if p.exists():
  if p.read_bytes()!=raw:raise FileExistsError(p)
 else:
  with p.open("xb") as f:f.write(raw);f.flush()
 return hashlib.sha256(raw).hexdigest()
def load_contract():
 spec=importlib.util.spec_from_file_location("issue83_repair_selftest_driver",STUDY/"driver.py")
 module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
 module.configure_driver();module.check_lock();return module
def wrapper_order_test(c,original,actions):
 calls=[]
 def validate(projection,live_actions):
  calls.append((projection.get("coverage_status"),len(live_actions)))
  if projection.get("coverage_status")!="supported" or projection.get("supported_candidate_count")!=len(live_actions):
   raise ValueError("base encoder rejected unsupported projection")
 class Controller:
  def select_action(self,*args,**kwargs): return None
 runner=SimpleNamespace(validate_live_native_candidate_bindings=validate,StudyController=Controller)
 runtime=SimpleNamespace(initialize_native_runtime=lambda *args,**kwargs:(None,runner,None,None))
 previous=c.ISSUE81.patch_modules
 learn=SimpleNamespace(_new_optimizer=lambda policy:None); ex=SimpleNamespace(_model_path=lambda label:None); c.ISSUE81.patch_modules=lambda root,*,pilot=False:(None,runtime,None,learn,ex)
 try:
  c.load_modules(DATA/"diagnostics"/"selftest-runtime",pilot=False)
  _,actual,_,_=runtime.initialize_native_runtime()
  repaired=copy.deepcopy(original); before=canon([x.get("candidate_binding") for x in repaired["candidates"]])
  actual.validate_live_native_candidate_bindings(repaired,actions)
  after=canon([x.get("candidate_binding") for x in repaired["candidates"]])
  if calls!=[("supported",2)] or before!=after or len(actual._issue83_phase3_repair_events)!=1:
   raise RuntimeError("runtime wrapper did not repair before validating exactly once")
  return True
 finally:
  c.ISSUE81.patch_modules=previous


def main():
 c=load_contract()
 witnesses=[p for p in (DATA/"diagnostics/native-coverage-witness").glob("coverage-8301831-*/native-coverage-witness.json") if sha(p)==WITNESS_SHA]
 if len(witnesses)!=1:raise RuntimeError(f"expected one exact original witness; got {len(witnesses)}")
 witness=json.loads(witnesses[0].read_text());original=witness["projection"]
 actions=[SimpleNamespace(kind="event",raw={"idx1":0}),SimpleNamespace(kind="event",raw={"idx1":1})]
 projection=copy.deepcopy(original);before=copy.deepcopy([x.get("candidate_binding") for x in projection["candidates"]])
 event=c.repair_dead_adventurer_phase3_projection(projection,actions)
 wrapper_pass=wrapper_order_test(c,original,actions)
 if event is None or event.get("derived_phase")!=3:raise RuntimeError("exact phase-3 candidate was not repaired")
 if [x.get("candidate_binding") for x in projection["candidates"]]!=before:raise RuntimeError("repair changed legal bindings")
 choice=projection["candidates"][0]["features"]["choice"]
 expected={"choice_type":"search_dead_adventurer_site","effect_type":"native_phase_reward_or_encounter_transition","current_phase":3}
 if choice.get("value")!=expected:raise RuntimeError("repair emitted fields outside registered semantics")
 if projection.get("coverage_status")!="supported" or projection.get("supported_candidate_count")!=2:raise RuntimeError("coverage not restored")
 mutations=[
  lambda x:x.update(audience="normal_public"),lambda x:x.update(public_api_safe=True),
  lambda x:x.update(native_source_commit="wrong"),lambda x:x.update(candidate_order_source="deduplicated"),
  lambda x:x.update(screen_identity={"value":"OTHER"}),
  lambda x:x["unsupported_candidates"][0].update(reason="different"),
  lambda x:x["candidates"][0].update(reason="different"),
  lambda x:x["candidates"][1].update(coverage_status="unsupported")]
 for i,mutate in enumerate(mutations):
  bad=copy.deepcopy(original);mutate(bad);snapshot=copy.deepcopy(bad)
  if c.repair_dead_adventurer_phase3_projection(bad,actions) is not None or canon(bad)!=canon(snapshot):
   raise RuntimeError(f"unsupported variant {i} did not fail closed unchanged")
 reverse=[SimpleNamespace(kind="event",raw={"idx1":1}),SimpleNamespace(kind="event",raw={"idx1":0})]
 bad=copy.deepcopy(original);snapshot=copy.deepcopy(bad)
 if c.repair_dead_adventurer_phase3_projection(bad,reverse) is not None or canon(bad)!=canon(snapshot):
  raise RuntimeError("reordered actions did not fail closed unchanged")
 commit=c.current_source_commit();lock=sha(STUDY/"source-lock-repair.json")
 result={"schema_id":"issue83-phase3-repair-selftest-v1","status":"PASS","formal_seeds_consumed":0,"simulator_instances_created":0,
  "source_commit":commit,"source_lock_sha256":lock,"original_witness_path":str(witnesses[0]),
  "original_witness_sha256":WITNESS_SHA,"candidate_binding_bytes_unchanged":True,"feature_semantics_fields_exact":True,
  "unsupported_mutations_fail_closed":len(mutations)+1,"runtime_wrapper_repairs_before_validation":wrapper_pass,"exact_action_order":[0,1],"repair_audit":event}
 out=DATA/"diagnostics/native-coverage-witness"/f"repair-self-test-{commit[:12]}"/"repair-self-test.json"
 result["receipt_sha256"]=write_once(out,result);print(json.dumps(result,sort_keys=True))
if __name__=="__main__":main()
