"""Bounded fixtures: no authentic training or 435xxx simulator execution."""
import copy
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
import replication as r

def public():
    return {'public_state':{'screen':'REWARDS','persistent_resources':{'hp':50},'screen_context':{'coverage_status':'supported'}},
        'legal_options':[{'candidate_index':i,'action':{'kind':'reward_card','idx1':i},'semantics':{'card':{'id':i+5,'upgraded':False}}} for i in range(2)]}

def fixture_tables():
    return {split:[{'split':split,'owner':owner,'root_id':str(owner),'serialized':json.dumps(public(),sort_keys=True),'q':[5.,10.]}]
            for split,owner in [('train',1),('validation',2)]}

class ReplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models=r.train_block(fixture_tables(),0)
        cls.checkpoints={a:v['checkpoint'] for a,v in cls.models.items()}

    def test_exact_two_seed_selection(self):
        import learning
        original=copy.deepcopy(learning.CONFIG)
        for b in range(6):
            result=r.train_block(fixture_tables(),b)
            for arm,value in result.items():
                curve=value['optimization']; c=value['checkpoint']
                self.assertEqual(len(curve),200)
                self.assertEqual({(v['seed'],v['update']) for v in curve}, {(s,u) for s in r.CONTRACT['model_seeds'][b] for u in range(1,101)})
                best=min(curve,key=lambda v:(v['validation_mse'],v['update'],v['seed']))
                self.assertEqual((c['validation_mse'],c['update'],c['model_seed']),(best['validation_mse'],best['update'],best['seed']))
                self.assertEqual(c['parameters'],2081); learning.load(c)
        self.assertEqual(original,learning.CONFIG)

    def test_split_and_owner_fail_closed(self):
        bad=fixture_tables(); bad['validation'][0]['owner']=1
        with self.assertRaises(ValueError): r.train_block(bad,0)
        bad=fixture_tables(); bad['validation'][0]['split']='heldout'
        with self.assertRaises(ValueError): r.train_block(bad,0)

    def test_fixed_manifest_and_freeze(self):
        blocks={b:self.checkpoints for b in range(6)}; plan=r.manifest(blocks)
        self.assertEqual(len(plan),1536)
        self.assertEqual({p['seed'] for p in plan},set(range(435001,435129)))
        self.assertEqual({p['arm'] for p in plan},{'visible','masked'})
        self.assertEqual(len({(p['block'],p['seed'],p['arm']) for p in plan}),1536)
        with self.assertRaises(ValueError): r.manifest({0:self.checkpoints})
        bad=copy.deepcopy(self.checkpoints); bad['visible']['state']['c']+=1
        with self.assertRaises(ValueError): r.block_plan(bad,0)

    def test_evaluate_delegates_exact_original_contract(self):
        import formal
        p=r.block_plan(self.checkpoints,0)[0]
        original=copy.deepcopy(formal.CONFIG)
        def fake(adapter,plan,models,store,progress):
            self.assertEqual(formal.CONFIG['fresh_seeds'],[435001,435128])
            for name in ('fresh_driver_seed','max_steps','native_commit','frozen_stsrl_commit'):
                self.assertEqual(formal.CONFIG[name],original[name])
            self.assertNotIn('block',plan); self.assertEqual(models,self.checkpoints)
            return 'same-native-executor'
        with patch.object(formal,'evaluate_one',side_effect=fake):
            self.assertEqual(r.evaluate(None,p,self.checkpoints,None,None),'same-native-executor')
        self.assertEqual(formal.CONFIG,original)
        bad={**p,'seed':433001}
        with self.assertRaises(ValueError): r.evaluate(None,bad,self.checkpoints,None,None)

    def test_public_model_input_and_masking(self):
        from student import treatment
        import learning
        raw=public(); modified=copy.deepcopy(raw)
        modified['legal_options'][0]['semantics']['card']['id']=999
        self.assertEqual(treatment(json.dumps(raw),'masked'),treatment(json.dumps(modified),'masked'))
        self.assertNotEqual(treatment(json.dumps(raw),'visible'),treatment(json.dumps(modified),'visible'))
        for arm in self.models:
            c=self.models[arm]['checkpoint']; model=learning.load(c)
            self.assertIn(model.choose(json.dumps(raw),arm),range(2))

    def test_crossed_matches_independent_reference(self):
        grid=[[b*3+s%5 for s in range(128)] for b in range(6)]
        rng=random.Random(440901); samples=[]
        for _ in range(31):
            bs=[rng.randrange(6) for _ in range(6)]; ss=[rng.randrange(128) for _ in range(128)]
            samples.append(sum(grid[b][s] for b in bs for s in ss)/768)
        expected=r.percentile(samples); actual=r.crossed(grid,replicates=31)
        self.assertEqual(actual['lower'],expected['lower']); self.assertEqual(actual['upper'],expected['upper'])
        self.assertEqual(actual['mean'],sum(map(sum,grid))/768)
        self.assertEqual(r.crossed([[2]*128 for _ in range(6)],replicates=31)['lower'],2)
        with self.assertRaises(ValueError): r.crossed([[1]*128])

    def test_classification_all_gates(self):
        self.assertEqual(r.classify(True,True,{'lower':1,'upper':2},[1]*5+[0]),'SEMANTIC_GAIN_MODEL_SEED_ROBUST_WITHIN_FIXED_DATA')
        self.assertEqual(r.classify(True,True,{'lower':1,'upper':2},[1]*4+[-1]*2),'MODEL_SEED_ROBUSTNESS_NOT_ESTABLISHED')
        self.assertEqual(r.classify(True,True,{'lower':-2,'upper':-1},[-1]*5+[0]),'SEMANTIC_HARM_MODEL_SEED_REPLICATED')
        self.assertEqual(r.classify(False,True,{'lower':1,'upper':2},[1]*6),'INCOMPLETE')
        self.assertEqual(r.classify(True,False,{'lower':1,'upper':2},[1]*6),'FRESH_LEARNED_CONTROL_INSUFFICIENT')

    def test_full_fixed_grid_report_and_incomplete_rejection(self):
        blocks={b:self.checkpoints for b in range(6)}; rows=[]
        for p in r.manifest(blocks):
            floor=11 if p['arm']=='visible' else 10
            choices=[]
            for family in ('MAP_SCREEN','REWARDS'):
                value=public(); value['public_state']['screen']=family
                choices.append({'family':family,'route':'learned','ordinal':0,'public_sha256':r.sha(value),'serialized_public_input':json.dumps(value,sort_keys=True)})
            rows.append({'block':p['block'],'seed':p['seed'],'arm':p['arm'],'key':[p['key'],p['seed'],p['arm']],
                'terminal':True,'problems':[],'outcome':'PLAYER_LOSS','floor':floor,'max_act':1,'heart_encountered':False,'heart_win':False,
                'steps':2,'choices':choices,'final_raw_privileged':{'cur_hp':0,'floor_num':floor,'outcome':'PLAYER_LOSS'},
                'battle_search_calls':0,'battle_search_cpu':0,'battle_search_wall':0})
        result=r.report(rows,blocks,[])
        self.assertEqual(result['classification'],'SEMANTIC_GAIN_MODEL_SEED_ROBUST_WITHIN_FIXED_DATA')
        self.assertEqual(result['crossed_interval']['lower'],1)
        self.assertEqual(len(result['runs']),1536)
        self.assertEqual(result['pooled_choices']['visible']['MAP_SCREEN_learned'],768)
        self.assertEqual(set(result['blocks'][0]['shift']['fresh']),{'visible','masked'})
        with self.assertRaises(ValueError): r.report(rows[:-1],blocks,[])
        with self.assertRaises(ValueError): r.report(rows[:-1]+[rows[0]],blocks,[])
        rows[0]['choices'][0]['route']='explicit_other_fallback'
        with self.assertRaisesRegex(ValueError,'PUBLIC_INPUT_INVALID'): r.report(rows,blocks,[])

    def test_authority_closed_without_real_comments(self):
        import launch
        with self.assertRaises(ValueError): launch.authorize('a'*40,Path('/tmp/frozen'),None,None)
        reviewer={'body':'ISSUE44_READINESS_PASS\n'+('a'*40),'created_at':'2026-10-12T01:00:00Z'}
        planner={'body':'ISSUE44_FRESH_LAUNCH_AUTHORIZED\n'+('a'*40)+' review\nLINEAGE_DIRECTORY=/tmp/frozen','created_at':'2026-10-12T02:00:00Z'}
        with patch.object(launch,'comment',side_effect=[reviewer,planner]):
            self.assertEqual(launch.authorize('a'*40,Path('/tmp/frozen'),'review','planner')['review_url'],'review')
        planner['body']=planner['body'].replace('/tmp/frozen','/tmp/other')
        with patch.object(launch,'comment',side_effect=[reviewer,planner]),self.assertRaises(ValueError):
            launch.authorize('a'*40,Path('/tmp/frozen'),'review','planner')

    def test_budget_recovery_and_corruption(self):
        import budget
        with tempfile.TemporaryDirectory() as directory:
            identity={'synthetic':True,'fixture':'no native run'}; limit={'cpu_seconds':30,'wall_seconds':30}
            journal=budget.Journal(directory,identity,limit)
            journal.state['leases']={'unused':{'cpu_cap':2,'charged_cpu':0,'wall_cap':1,'elapsed':0,'boot':'never-current','token':'never-launched'}}
            journal.record('fixture_reserved'); journal.close()
            sup=budget.Supervisor(directory,identity,limit,free=0,job_cpu=2,job_wall=1)
            self.assertGreaterEqual(sup.state['cpu'],2); self.assertGreaterEqual(sup.state['wall'],1)
            self.assertFalse(sup.state['leases']); sup.close()
            before=json.loads(Path(directory,'resource.jsonl').read_text().splitlines()[-1])['state']['cpu']
            sup=budget.Supervisor(directory,identity,limit,free=0); self.assertGreaterEqual(sup.state['cpu'],before); sup.close()
            with self.assertRaises(ValueError): budget.Journal(directory,{'changed':True},limit)
            with Path(directory,'resource.jsonl').open('a') as f: f.write('{bad}\n')
            with self.assertRaises(ValueError): budget.Journal(directory,identity,limit)

    def test_cpu_wall_and_memory_fail_closed(self):
        import budget
        supervisor=object.__new__(budget.Supervisor); supervisor.free=4*1024**3
        supervisor.state={'cpu':48000,'wall':0,'limit':{'cpu_seconds':48000,'wall_seconds':9000}}
        with self.assertRaisesRegex(RuntimeError,'RESOURCE_BLOCKED'): supervisor.check()
        supervisor.state['cpu']=0; supervisor.state['wall']=9000
        with self.assertRaisesRegex(RuntimeError,'RESOURCE_BLOCKED'): supervisor.check()
        supervisor.state['wall']=0
        with patch.object(budget,'available',return_value=0),self.assertRaisesRegex(RuntimeError,'available memory'): supervisor.check()

    def test_source_adapter_fixture_suite(self):
        # Exact pinned public projection/action mapping fixtures; no simulator pilot.
        import test_adapter,test_student
        suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromModule(m) for m in (test_adapter,test_student)])
        result=unittest.TestResult(); suite.run(result)
        self.assertEqual(result.errors,[]); self.assertEqual(result.failures,[])

if __name__=='__main__': unittest.main()
