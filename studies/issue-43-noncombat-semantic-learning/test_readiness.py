"""Bounded synthetic negative checks. Never constructs a native simulator."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from budget import Journal,Supervisor,caps,overlay,sha,processes
from formal import CONFIG,Completed,digest,evaluation_plan,keys
from formal import evaluate_one
from launcher import identity,authorize,Pipeline,immutable
from student import Scorer
from reporting import fresh,heldout
from worker import fixture,synthetic_source


def checkpoints():
    out={}
    for arm in ('visible','masked'):
        c={'arm':arm,'state':Scorer(430081).state(),'model_seed':430081,'config_sha256':sha(CONFIG)}
        c['checkpoint_sha256']=sha(c); out[arm]=c
    return out


def runs(models):
    rows=[]
    for p in evaluation_plan(models):
        choices=[]
        for family in ('MAP_SCREEN','REWARDS'):
            public=fixture(family); serialized=json.dumps(public)
            choices.append({'family':family,'route':'expert' if p['arm']=='expert_source' else 'learned',
                'ordinal':0,'public_sha256':sha(public),'serialized_public_input':serialized})
        rows.append({'key':[p['key'],p['seed'],p['arm']],'seed':p['seed'],'arm':p['arm'],'terminal':True,
            'problems':[],'outcome':'PLAYER_LOSS','floor':10,'max_act':1,'heart_encountered':False,'heart_win':False,'choices':choices})
    return rows


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.ident=identity('4f099fc33de275b11900bfe1936db32101cd8a6e',True)
    def request(self,directory,mode,**payload):
        path=Path(directory)/('request-'+str(time.time_ns())+'.json')
        immutable(path,{'identity':self.ident,'kind':'fault','payload':{'mode':mode,**payload},
            'key':['fault',mode,time.time_ns()],'store':str(Path(directory)/'jobs')})
        return path
    def supervisor(self,path,**kwargs):
        return Supervisor(path,self.ident,{'cpu_seconds':20,'wall_seconds':8},job_cpu=3,job_wall=2,free=0,**kwargs)
    def test_overlay_and_authorization(self):
        self.assertEqual(caps('stage1'),{'cpu_seconds':180000,'wall_seconds':27000})
        self.assertEqual(caps('stage2'),{'cpu_seconds':36000,'wall_seconds':12000})
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'overlay.json'; value=overlay(); value['model']={}; p.write_text(json.dumps(value))
            with self.assertRaises(ValueError): overlay(p)
        with self.assertRaises(ValueError): authorize('stage1',self.ident,None)
    def test_completed_corruption_and_original_keys(self):
        with tempfile.TemporaryDirectory() as d:
            store=Completed(d,self.ident); root=synthetic_source(430001,'train')[0][0]; k=keys(root)[0]
            store.publish(k,{'terminal':True,'problems':[],'terminal_floor':20})
            original=(Path(d)/(digest(k)+'.json')).read_text()
            (Path(d)/(digest(k)+'.json')).write_text(original.replace('20','21'))
            with self.assertRaises(ValueError): Completed(d,self.ident).get(k)
            (Path(d)/'completed.jsonl').write_text('{truncated')
            with self.assertRaises(ValueError): Completed(d,self.ident)
    def test_resume_completed_does_not_rerun(self):
        with tempfile.TemporaryDirectory() as d:
            request=self.request(d,'sleep',seconds=.02); s=self.supervisor(Path(d)/'resources')
            s.run([request]); s.close(); first=s.state['cpu']
            result=Completed(Path(d)/'jobs',self.ident).get(json.loads(request.read_text())['key'])
            self.assertTrue(result['terminal'])
            s=self.supervisor(Path(d)/'resources'); self.assertGreaterEqual(s.state['cpu'],first)
            self.assertEqual(s.state['attempts'],2); s.close()
    def test_worker_death_charged_and_journal_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.supervisor(Path(d)/'resources')
            with self.assertRaises(RuntimeError): s.run([self.request(d,'exit')])
            self.assertGreaterEqual(s.state['cpu'],3); s.close(); first=s.state['cpu']
            s=self.supervisor(Path(d)/'resources'); self.assertGreaterEqual(s.state['cpu'],first); s.close()
            with (Path(d)/'resources/resource.jsonl').open('a') as stream: stream.write('{truncated')
            with self.assertRaises(ValueError): self.supervisor(Path(d)/'resources')
    def test_wall_timeout_and_child_group_termination(self):
        with tempfile.TemporaryDirectory() as d:
            pid_file=Path(d)/'child.pid'; s=self.supervisor(Path(d)/'resources')
            with self.assertRaises(RuntimeError): s.run([self.request(d,'child',seconds=10,pid_file=str(pid_file))])
            s.close(); time.sleep(.1); self.assertTrue(pid_file.exists())
            pid=int(pid_file.read_text()); path=Path('/proc')/str(pid)/'stat'
            self.assertTrue(not path.exists() or path.read_text().split(') ',1)[1].startswith('Z '))
            self.assertGreater(s.state['wall'],1.5)
    def test_cpu_memory_and_system_guards(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.supervisor(Path(d)/'cpu')
            with self.assertRaises(RuntimeError): s.run([self.request(d,'cpu',seconds=10)])
            self.assertGreaterEqual(s.state['cpu'],3); s.close()
            s=self.supervisor(Path(d)/'rss',rss=32*1024**2)
            with self.assertRaises(RuntimeError): s.run([self.request(d,'rss')])
            s.close()
            with patch('budget.available',return_value=0):
                with self.assertRaises(RuntimeError): Supervisor(Path(d)/'free',self.ident,{'cpu_seconds':20,'wall_seconds':8},free=1)
    def test_supervisor_death_recovery_debits_full_lease(self):
        with tempfile.TemporaryDirectory() as d:
            request=self.request(d,'sleep',seconds=10); directory=Path(d)/'resources'
            code="from budget import Supervisor; import json,os,time; from pathlib import Path; r=Path(%r); i=json.loads(r.read_text())['identity']; s=Supervisor(%r,i,{'cpu_seconds':20,'wall_seconds':8},job_cpu=3,job_wall=2,free=0); s.launch(r); time.sleep(.2); os._exit(0)"%(str(request),str(directory))
            subprocess.run([sys.executable,'-c',code],check=True)
            journal=[json.loads(line) for line in (directory/'resource.jsonl').read_text().splitlines()]
            lease=next(iter(journal[-1]['state']['leases'].values())); s=self.supervisor(directory)
            self.assertGreaterEqual(s.state['cpu'],3); self.assertGreaterEqual(s.state['wall'],2)
            time.sleep(.05); self.assertTrue(all(r['own_cpu']==0 or Path('/proc/'+str(r['pid'])+'/stat').read_text().split(') ',1)[1].startswith('Z ') for r in processes(lease['pid'])))
            s.close()
    def test_cumulative_cap_cannot_reset(self):
        with tempfile.TemporaryDirectory() as d:
            j=Journal(d,self.ident,{'cpu_seconds':2,'wall_seconds':8}); j.state['cpu']=2; j.record('used'); j.close()
            with self.assertRaises(RuntimeError): Supervisor(d,self.ident,{'cpu_seconds':2,'wall_seconds':8},free=0)
            with self.assertRaises(ValueError): Supervisor(d,self.ident,{'cpu_seconds':3,'wall_seconds':8},free=0)
    def test_fixed_claim_and_fresh_negative_gates(self):
        models=checkpoints(); original=runs(models)
        with patch('reporting.interval',side_effect=lambda v,s:{'mean':next(iter(v.values()))[0],'lower':next(iter(v.values()))[0],'upper':next(iter(v.values()))[0]}):
            rows=copy.deepcopy(original)
            for r in rows:
                if r['arm']=='visible': r.update(floor=12,max_act=2)
            report=fresh(rows,models,[])
            self.assertIn('NONCOMBAT_TEACHER_ASSISTED_RUN_IMPROVEMENT_ESTABLISHED',report['claims'])
            self.assertEqual(report['shared_public_state_counts']['REWARDS'],128)
            self.assertEqual(fresh(rows,models,[],True)['claims'],[])
            for r in rows:
                if r['arm']=='expert_source': r.update(max_act=2,floor=15)
            self.assertNotIn('NONCOMBAT_TEACHER_ASSISTED_RUN_IMPROVEMENT_ESTABLISHED',fresh(rows,models,[])['claims'])
            for r in rows:
                if r['arm']=='visible': r['floor']=1
            self.assertEqual(fresh(rows,models,[])['claims'],['PUBLIC_SEMANTICS_HARM_CONFIRMED'])
            self.assertEqual(fresh(original,models,[])['claims'],['LEARNING_IMPROVEMENT_NOT_ESTABLISHED'])
            for r in rows:
                if r['arm']=='visible': r['choices']=[]
            self.assertEqual(fresh(rows,models,[])['claims'],['FRESH_LEARNED_CONTROL_INSUFFICIENT'])
        with self.assertRaises(ValueError): fresh(original[:-1],models,[])
        bad=copy.deepcopy(original); bad[0]['terminal']=False
        with self.assertRaises(ValueError): fresh(bad,models,[])

    def test_expert_other_choices_are_reported_from_native_executor_role(self):
        models=checkpoints(); p=next(p for p in evaluation_plan(models) if p['arm']=='expert_source')
        adapter=SimpleNamespace()
        def execute(adapter,selected,**kwargs):
            kwargs['before_decision'](SimpleNamespace(raw={'act':2,'encounter_id':'THE_HEART'}),[],
                SimpleNamespace(screen_state='REWARDS',public_run_context={}),0)
            return SimpleNamespace(terminal=True,problems=[],outcome='PLAYER_VICTORY',final_raw={'floor_num':55,'act':4},
                steps=[SimpleNamespace(step_index=0,chosen_action_index=0,controller_role='non_combat_driver',screen_state='REWARDS'),
                       SimpleNamespace(step_index=1,chosen_action_index=1,controller_role='non_combat_driver',screen_state='REST_SCREEN')])
        with tempfile.TemporaryDirectory() as d, patch('formal.execute_controlled_run',side_effect=execute), patch('formal.prepared',
            return_value=SimpleNamespace(serialized_input=json.dumps(fixture()))):
            row=evaluate_one(adapter,p,models,Completed(d,self.ident),lambda *args:None)
            self.assertEqual([c['family'] for c in row['choices']],['REWARDS','REST_SCREEN'])
            self.assertEqual(row['max_act'],4); self.assertTrue(row['heart_win'])
        bad=copy.deepcopy(original); bad[0]['choices'][0]['route']='fallback'
        with self.assertRaises(ValueError): fresh(bad,models,[])


if __name__=='__main__': unittest.main()
