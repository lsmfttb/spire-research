import importlib.util
from pathlib import Path
import unittest, subprocess, sys, inspect
p=Path(__file__).with_name("driver.py")
s=importlib.util.spec_from_file_location("issue81_driver_test",p)
d=importlib.util.module_from_spec(s); s.loader.exec_module(d)
class ContractTests(unittest.TestCase):
 def test_native_source_commit_expectation_is_full_sha(self):
  self.assertEqual(d.EXPECT["sts"],"8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2")
  self.assertEqual(len(d.EXPECT["sts"]),40)
 def test_prior_base_smoke_receipt_is_verified_and_reused(self):
  row=d.evaluation_only_plan()["smoke"][0]
  result,provenance=d.previous_smoke_receipt(row,current_root=Path("/issue81-no-current-attempts"))
  self.assertEqual(result["package_commit"],d.PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT)
  self.assertEqual(provenance["result_sha256"],d.PREVIOUS_BASE_SMOKE_RESULT_SHA256)
  self.assertEqual(provenance["source_lock_sha256"],d.PREVIOUS_BASE_SMOKE_SOURCE_LOCK_SHA256)
 def test_fixed_populations(self):
  t=d.seed_table(); tr=[r for r in t["rows"] if r["phase"]=="training"]; ev=[r for r in t["rows"] if r["phase"]=="heldout"]
  self.assertEqual(len(tr),384); self.assertEqual(len(ev),288)
  self.assertEqual({r["owner_seed"] for r in tr},set(range(7600001,7600385)))
  self.assertEqual({r["owner_seed"] for r in ev},set(range(7640001,7640097)))
  self.assertEqual({r["arm"] for r in ev},{"BASE","PGONLY","JTRAIN"})
  self.assertTrue(all(not r["search_invocation_seeds"] for r in t["rows"]))
  for owner in range(7640001,7640097):
   self.assertEqual(len({r["policy_rng_seed"] for r in ev if r["owner_seed"]==owner}),3)
 def test_evaluation_only_plan_reuses_only_registered_heldout_rows(self):
  plan=d.evaluation_only_plan()
  self.assertEqual(len(plan["smoke"]),3)
  self.assertEqual(len(plan["heldout"]),288)
  self.assertEqual({r["owner_seed"] for r in plan["smoke"]},set(d.SMOKE_SEEDS))
  self.assertTrue(all(r["formal"] is False and r["search_invocation_seeds"]==[] for r in plan["smoke"]))
  self.assertTrue(all(r["formal"] is True and r["search_invocation_seeds"]==[] for r in plan["heldout"]))
  self.assertTrue(set(d.SMOKE_SEEDS).isdisjoint(set(d.TRAIN)|set(d.EVAL)))
  self.assertEqual({r["owner_seed"] for r in plan["heldout"]},set(d.EVAL))
 def test_evaluation_only_entrypoint_cannot_train(self):
  source=inspect.getsource(d.run_evaluation_only)
  self.assertNotIn("run_formal",source)
  self.assertFalse(hasattr(d,"run_formal"))
  self.assertNotIn("train_update",source)
  self.assertNotIn("rows_for(table,\"training\"",source)
  self.assertIn("SUPPLEMENTAL_ALLOWANCE",source)
 def test_supplemental_authorization_is_evaluation_only(self):
  c=d.evaluation_only_contract()
  self.assertEqual(c["supplemental_clock"]["allowance_seconds"],1800.0)
  self.assertTrue(c["no_training_loop"])
  self.assertTrue(c["no_optimizer_updates"])
  self.assertEqual(c["original_clock"]["elapsed_seconds"],3784.857659886001)
 def test_formal_training_cli_is_not_available(self):
  result=subprocess.run([sys.executable,str(p),"--mode","formal"],capture_output=True,text=True)
  self.assertNotEqual(result.returncode,0)
  self.assertIn("invalid choice",result.stderr)
 def test_registered_allowance(self):
  c=d.contract(); self.assertGreaterEqual(c["resource"]["allowance_factor"],1.5)
  self.assertLessEqual(c["resource"]["allowance_factor"],2.0)
  self.assertEqual(c["evaluation"]["terminals"],288)
  self.assertEqual(c["training"]["objective"],"L_pg - 0.01 H")
 def test_search_guard_and_zero_elite_are_wired(self):
  source=p.read_text(); self.assertIn("Issue 81 forbids every search call",source)
  self.assertIn("PGONLY performed a nonzero elite update",source)
 def test_worker_entry_does_not_require_mode(self):
  missing=p.with_name("missing-worker-job-for-cli-test.json")
  result=subprocess.run([sys.executable,str(p),"--worker",str(missing)],capture_output=True,text=True)
  self.assertNotIn("the following arguments are required: --mode",result.stderr)
  self.assertIn("FileNotFoundError",result.stderr)
 def test_worker_module_loader_accepts_pilot_keyword(self):
  self.assertIn("pilot",inspect.signature(d.patch_modules).parameters)
 def test_registered_eval_arms_do_not_eagerly_call_fallback(self):
  def fail(*_args): raise AssertionError("fallback must not run for a registered arm")
  for arm,expected in (("BASE",("pi1",False)),("PGONLY",("pi4",False)),("JTRAIN",("jtrain",False))):
   self.assertEqual(d.registered_arm_policy("heldout",arm,{"phase":"heldout"},fail),expected)
  self.assertEqual(d.registered_arm_policy("training","TRAIN",{"phase":"training","block_index":3},fail),("pi3",False))
 def test_unregistered_eval_arm_uses_fallback(self):
  fallback=lambda phase,arm,row:("fallback",phase,arm,row["phase"])
  self.assertEqual(d.registered_arm_policy("heldout","OTHER",{"phase":"heldout"},fallback),("fallback","heldout","OTHER","heldout"))
 def test_registered_eval_arms_map_to_native_controller_labels_without_search(self):
  self.assertEqual({arm:d.native_controller_arm(arm) for arm in ("BASE","PGONLY","JTRAIN")},
   {"BASE":"P0","PGONLY":"P1","JTRAIN":"P1"})
  with self.assertRaisesRegex(ValueError,"unregistered Issue 81 evaluation arm"):
   d.native_controller_arm("R")
 def test_diagnostic_smoke_stops_after_one_failed_worker_attempt(self):
  source=inspect.getsource(d.run_smoke_once)
  self.assertNotIn("D.run_units",source)
  self.assertIn("PRIOR_FAILED_ATTEMPT_EXISTS",source)
  self.assertIn("SMOKE_WORKER_FAILED",source)
  self.assertIn("previous_smoke_receipt",source)
  helper=inspect.getsource(d.previous_smoke_receipt)
  self.assertIn("PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT",helper)
  main=inspect.getsource(d.run_evaluation_only)
  self.assertIn("run_smoke_once",main)
  self.assertIn("previous_evaluation_only_package_commit",main)
  self.assertEqual(d.PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT,"a484175828868da5de7147694122b5ff41da4d25")
if __name__=="__main__": unittest.main()
