"""Short isolated regression checks for the two independent readiness findings."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from budget import Supervisor
from launcher import identity,producer_sources


class RepairTests(unittest.TestCase):
    def test_successful_worker_window_growth_crash_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d)
            # Real process-group launch/settlement, with a cheap synthetic worker.
            (directory/'worker.py').write_text('import time; time.sleep(.05)\n')
            code="""
import json,os,time
from pathlib import Path
import budget
budget.HERE=Path(%r)
s=budget.Supervisor(budget.HERE/'resources',{'fixture':'repair'},
    {'cpu_seconds':100,'wall_seconds':15},job_cpu=90,job_wall=2,free=0)
token=s.launch(budget.HERE/'request.json')
until=time.process_time()+1.2
while time.process_time()<until: pass
s.tick()
old=s.state['parent_cpu_lease']
while s.settle(token) is None: time.sleep(.01)
s.tick()
new=s.state['parent_cpu_lease']
assert old<new and new==10,(old,new)
rows=[json.loads(line) for line in s.journal.path.read_text().splitlines()]
assert rows[-1]['state']['parent_cpu_lease']==new
(budget.HERE/'observed.json').write_text(json.dumps({'old':old,'new':new,'cpu':s.state['cpu']}))
until=time.process_time()+.2
while time.process_time()<until: pass
os._exit(0)
"""%d
            subprocess.run([sys.executable,'-c',code],check=True,timeout=10)
            observed=json.loads((directory/'observed.json').read_text())
            s=Supervisor(directory/'resources',{'fixture':'repair'},
                         {'cpu_seconds':100,'wall_seconds':15},free=0)
            try:
                self.assertGreaterEqual(s.state['cpu']-observed['cpu'],observed['new'])
                self.assertEqual(s.state['leases'],{})
            finally: s.close()

    def test_declared_producer_binds_python_config_and_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); study=root/'studies'/'fixture'; study.mkdir(parents=True)
            names=('launcher.py','student.py','config.json','planner_budget_amendment.json')
            for name in names: (study/name).write_text('{}\n' if name.endswith('.json') else '# fixture\n')
            def git(*args):
                return subprocess.run(['git','-C',d,*args],check=True,capture_output=True,text=True).stdout.strip()
            git('init'); git('add','.')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture')
            head=git('rev-parse','HEAD')
            with patch('launcher.HERE',study),patch('launcher.runtime',return_value={}):
                self.assertEqual(len(producer_sources(head)),4)
                self.assertEqual(identity(head,False)['producer_source_binding']['mode'],'EXACT_GIT_TREE')
                for name in names:
                    original=(study/name).read_bytes(); (study/name).write_bytes(original+b'\n')
                    with self.assertRaises(ValueError): identity(head,False)
                    (study/name).write_bytes(original)
                with self.assertRaises(ValueError): identity('0'*40,False)
                (study/'injected.py').write_text('# extra executable\n')
                with self.assertRaises(ValueError): identity(head,False)
                self.assertEqual(identity(head,True)['producer_source_binding']['mode'],
                                 'SYNTHETIC_UNBOUND_SOURCE_SNAPSHOT')


if __name__=='__main__': unittest.main()
