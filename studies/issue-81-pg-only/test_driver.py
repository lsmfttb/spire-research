import importlib.util
from pathlib import Path
import unittest, subprocess, sys, inspect
p=Path(__file__).with_name("driver.py")
s=importlib.util.spec_from_file_location("issue81_driver_test",p)
d=importlib.util.module_from_spec(s); s.loader.exec_module(d)
class ContractTests(unittest.TestCase):
 def test_launcher_resolves_current_task_worktree(self):
  launcher=p.with_name("run_issue81.sh").read_text()
  self.assertIn("dirname",launcher)
  self.assertIn("/../..",launcher)
  self.assertNotIn("ROOT=/home/lsmft/spire-research-issue81",launcher)
  self.assertNotIn('" + "',launcher)
  self.assertIn("BASH_SOURCE",launcher)
 def test_native_source_commit_expectation_is_full_sha(self):
  self.assertEqual(d.EXPECT["sts"],"8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2")
  self.assertEqual(len(d.EXPECT["sts"]),40)
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
 def test_continuation_entrypoint_cannot_train_or_repeat_smoke(self):
  source=inspect.getsource(d.run_missing_heldout_continuation)
  self.assertNotIn("run_formal",source)
  self.assertFalse(hasattr(d,"run_formal"))
  self.assertNotIn("train_update",source)
  self.assertNotIn("run_smoke_once",source)
  self.assertIn("D.run_units(run_rows",source)
  self.assertIn("RESUME_ALLOWANCE",source)
  self.assertNotIn("SUPPLEMENTAL_ALLOWANCE",source)
  self.assertFalse(hasattr(d,"run_evaluation_only"))
  self.assertTrue(callable(d.verify))
 def test_continuation_clock_and_population_are_locked(self):
  c=d.continuation_contract()
  self.assertEqual(c["clock"]["id"],"issue81-heldout-resume-20261030")
  self.assertEqual(c["clock"]["allowance_seconds"],1500.0)
  self.assertEqual(c["heldout"]["registered_terminal_count"],288)
  self.assertEqual(c["heldout"]["run_only_missing_count"],83)
  self.assertFalse(c["smoke"]["run_new_smoke"])
  self.assertFalse(c["training"]["repeat_training"])
 def test_historical_success_mapping_audits_205_and_exact_83_missing(self):
  rows=d.evaluation_only_plan()["heldout"]
  records,mapping,missing,binding=d.prior_heldout_records(rows)
  self.assertEqual(len(records),205)
  self.assertEqual({arm:sum(value[0]["arm"]==arm for value in records.values()) for arm in d.ARMS},
   {"BASE":69,"PGONLY":68,"JTRAIN":68})
  self.assertEqual(len(mapping),205)
  self.assertEqual(len(missing),83)
  self.assertEqual(missing[:2],["EVAL:7640069:PGONLY","EVAL:7640069:JTRAIN"])
  self.assertEqual(missing[-3:],["EVAL:7640096:BASE","EVAL:7640096:PGONLY","EVAL:7640096:JTRAIN"])
  self.assertEqual(binding["terminal_mapping_count"],205)
 def test_smoke_receipts_are_reused_and_not_in_heldout_mapping(self):
  smoke,provenance=d.prior_smoke_batch(d.evaluation_only_plan()["smoke"])
  self.assertEqual(smoke["completed"],3)
  self.assertEqual(len(smoke["reused_prior_receipts"]),3)
  self.assertEqual(provenance["terminal_mapping_count"],3)
  self.assertFalse(d.readj(d.ART/"evaluation-only"/d.PREVIOUS_TERMINAL_PACKAGE_COMMIT/"diagnostic-smoke-terminals.json")["efficacy_cohort"])
 def test_supplemental_authorization_is_evaluation_only(self):
  c=d.evaluation_only_contract()
  self.assertEqual(c["supplemental_clock"]["allowance_seconds"],1800.0)
  self.assertTrue(c["no_training_loop"])
  self.assertTrue(c["no_optimizer_updates"])
  self.assertEqual(c["original_clock"]["elapsed_seconds"],3784.857659886001)
 def test_unapproved_all_288_mode_is_not_available(self):
  for mode in ("formal","evaluation-only"):
   result=subprocess.run([sys.executable,str(p),"--mode",mode],capture_output=True,text=True)
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
if __name__=="__main__": unittest.main()
