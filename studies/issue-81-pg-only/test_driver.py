import importlib.util
from pathlib import Path
import unittest, subprocess, sys, inspect
p=Path(__file__).with_name("driver.py")
s=importlib.util.spec_from_file_location("issue81_driver_test",p)
d=importlib.util.module_from_spec(s); s.loader.exec_module(d)
class ContractTests(unittest.TestCase):
 def test_fixed_populations(self):
  t=d.seed_table(); tr=[r for r in t["rows"] if r["phase"]=="training"]; ev=[r for r in t["rows"] if r["phase"]=="heldout"]
  self.assertEqual(len(tr),384); self.assertEqual(len(ev),288)
  self.assertEqual({r["owner_seed"] for r in tr},set(range(7600001,7600385)))
  self.assertEqual({r["owner_seed"] for r in ev},set(range(7640001,7640097)))
  self.assertEqual({r["arm"] for r in ev},{"BASE","PGONLY","JTRAIN"})
  self.assertTrue(all(not r["search_invocation_seeds"] for r in t["rows"]))
  for owner in range(7640001,7640097):
   self.assertEqual(len({r["policy_rng_seed"] for r in ev if r["owner_seed"]==owner}),3)
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
if __name__=="__main__": unittest.main()
