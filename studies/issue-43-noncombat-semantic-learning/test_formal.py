import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from formal import Completed, atomic, cohort, validate_splits, keys, targets, evaluation_plan, clustered_interval, LearnedNonCombat
from learning import fit, load, examples
from stage0 import CONFIG, digest
from test_student import fixture


def root(split,owner,family='REWARDS',step=10):
    serialized=json.dumps(fixture(family))
    return {'split':split,'owner':owner,'family':family,'step':step,'source_floor':4,'legal_count':3,
            'serialized_public_input':serialized,'public_input_sha256':digest(json.loads(serialized)),
            'candidate_sha256':[digest(x) for x in json.loads(serialized)['legal_options']],
            'privileged_checkpoint_fingerprint_sha256':'synthetic_hidden_identity'}


class FormalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.synthetic_models=fit([{'split':'train','owner':430001,'root_id':'synthetic_train',
                                   'serialized':json.dumps(fixture()),'q':[0.,10.,20.]}],
                                [{'split':'validation','owner':431001,'root_id':'synthetic_val',
                                   'serialized':json.dumps(fixture()),'q':[0.,10.,20.]}])

    def test_exact_original_cohort_and_all_keys(self):
        cohorts={}
        for split in ('train','validation','heldout'):
            low=CONFIG['source_ranges'][split][0]; quota=CONFIG['quotas'][split]
            rows=[root(split,low+i) for i in range(quota['REWARDS'])]
            rows += [root(split,low+i,'MAP_SCREEN',5) for i in range(quota['MAP_SCREEN'])]
            cohorts[split]=cohort(rows,split)
            self.assertEqual(len(cohorts[split]),quota['MAP_SCREEN']+quota['REWARDS'])
            # Two families can share owners; no duplicate family/root replacement.
            self.assertEqual(len({r['owner'] for r in cohorts[split]}),quota['REWARDS'])
            all_keys=[key for r in cohorts[split] for key in keys(r)]
            self.assertEqual(len(all_keys),len(cohorts[split])*3*len(CONFIG['continuation_seeds'][split]))
            self.assertEqual(len({digest(k) for k in all_keys}),len(all_keys))
        self.assertTrue(validate_splits(cohorts))
        with self.assertRaisesRegex(ValueError,'COVERAGE'):
            cohort(cohorts['train'][:-1],'train')
        wrong=copy.deepcopy(cohorts['validation']); wrong[0]['owner']=430001
        with self.assertRaisesRegex(ValueError,'split'):
            cohort(wrong,'validation')

    def test_targets_are_complete_terminal_curriculum_proxy(self):
        r=root('train',430001)
        rows=[{'key':key,'terminal':True,'problems':[],'terminal_floor':2+key[4]*4} for key in keys(r)]
        result=targets(r,rows)
        self.assertEqual(result['q'],[0.,2.,6.])
        with self.assertRaises(ValueError): targets(r,rows[:-1])
        with self.assertRaises(ValueError): targets(r,rows+[rows[0]])
        rows[0]['terminal']=False
        with self.assertRaises(ValueError): targets(r,rows)

    def test_durable_completed_manifest_and_crash_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity={'config_sha':digest(CONFIG),'native':'fixed','producer':'synthetic'}
            store=Completed(tmp,identity); key=['synthetic_branch',0]
            self.assertIsNone(store.get(key))
            result={'terminal':True,'problems':[],'terminal_floor':9}
            saved=store.publish(key,result)
            self.assertEqual(Completed(tmp,identity).get(key),saved)
            orphan_key=['synthetic_orphan',1]
            atomic(Path(tmp)/(digest(orphan_key)+'.json'),{**result,'identity':identity,'key':orphan_key})
            self.assertEqual(Completed(tmp,identity).get(orphan_key)['terminal_floor'],9)
            events=[json.loads(s) for s in (Path(tmp)/'completed.jsonl').read_text().splitlines()]
            self.assertTrue(events[-1]['recovered_orphan'])
            with self.assertRaises(ValueError): Completed(tmp,{'native':'changed'})
            with self.assertRaises(ValueError): store.publish(['invalid'],{'terminal':False,'problems':['cap']})
            path=Path(tmp)/(digest(key)+'.json'); path.write_text(path.read_text().replace('9','8'))
            with self.assertRaises(ValueError): Completed(tmp,identity).get(key)

    def test_training_freeze_no_heldout_and_equal_targets(self):
        outputs=self.synthetic_models
        for arm in ('visible','masked'):
            checkpoint=outputs[arm]['checkpoint']; load(checkpoint)
            self.assertEqual(len(outputs[arm]['optimization']),200)
            self.assertEqual({x['seed'] for x in outputs[arm]['optimization']},set(CONFIG['model_seeds']))
            tampered=copy.deepcopy(checkpoint); tampered['state']['c']+=1
            with self.assertRaises(ValueError): load(tampered)
        self.assertEqual(outputs['visible']['checkpoint']['train_targets_sha256'],outputs['masked']['checkpoint']['train_targets_sha256'])
        with self.assertRaises(ValueError): fit([{'split':'heldout'}],[{'split':'validation'}])
        table={'serialized':json.dumps(fixture()),'q':[0.,10.,20.]}
        before=examples([table],'visible')
        table.update(seed=123,private_state='SECRET',runtime=999,teacher_bits=456)
        self.assertEqual(before,examples([table],'visible'))

    def test_384_frozen_evaluation_keys_and_cluster_bootstrap(self):
        checkpoints={arm:value['checkpoint'] for arm,value in self.synthetic_models.items()}
        plan=evaluation_plan(checkpoints)
        self.assertEqual(len(plan),384); self.assertEqual(len({r['key'] for r in plan}),384)
        self.assertEqual({r['seed'] for r in plan},set(range(433001,433129)))
        values={433001:[2.],433002:[2.]}
        self.assertEqual(clustered_interval(values,433091)['lower'],2.)
        self.assertEqual(clustered_interval(values,433091),clustered_interval(values,433091))

    def test_supported_bad_input_never_calls_expert(self):
        from types import SimpleNamespace
        checkpoint=self.synthetic_models['visible']['checkpoint']
        controller=LearnedNonCombat(checkpoint)
        context=SimpleNamespace(screen_state='MAP_SCREEN',public_run_context={})
        with patch('formal.prepared',side_effect=ValueError('PUBLIC_INPUT_INVALID')), \
             patch.object(type(controller.fallback),'select_action',side_effect=AssertionError('silent fallback')):
            with self.assertRaisesRegex(ValueError,'PUBLIC_INPUT_INVALID'):
                controller.select_action(None,None,[],context,0)


if __name__=='__main__':
    unittest.main()
