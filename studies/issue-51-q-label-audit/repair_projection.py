"""Sanitize only retained derived examples; no trace replay or scientific rerun."""
import copy
import hashlib
import json
import os
from pathlib import Path
import time
from trace_audit import action, assert_public_export

BASE = Path('/home/lsmft/.cache/issue51-q-label-audit')
BASE_SEAL = 'b586f6ac2c9bf6e30b4525941aa2674ae73af2226c8eb5bfd21793bd5b281a3d'


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write(p, value):
    Path(p).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+'\n')


def main():
    started = time.process_time()
    producer = os.environ['ISSUE51_PRODUCER']
    assert len(producer) == 40 and all(x in '0123456789abcdef' for x in producer)
    output = BASE/('projection-repair-'+producer[:8])
    output.mkdir(exist_ok=False)
    assert sha(BASE/'artifact-manifest.json') == BASE_SEAL
    original = json.loads((BASE/'artifact-manifest.json').read_text())
    for r in original['files']:
        assert sha(BASE/r['path']) == r['sha256']
    examples = json.loads((BASE/'public_examples.json').read_text())
    changes = []

    def sanitize(value, path=''):
        if isinstance(value, dict):
            result = {k:sanitize(v,path+'/'+k) for k,v in value.items()}
            if 'kind' in result and 'label' in result:
                new_label = action(result)['label']
                if new_label != result['label']:
                    changes.append(path)
                    result['label'] = new_label
            return result
        if isinstance(value, list):
            return [sanitize(v,path+'/'+str(i)) for i,v in enumerate(value)]
        return value

    fixed = sanitize(examples)
    assert changes and assert_public_export(fixed)
    # Compare all original content after replacing exactly the declared labels.
    expected = copy.deepcopy(examples)
    for path in changes:
        parts = path.strip('/').split('/')
        obj = expected
        for part in parts:
            obj = obj[int(part)] if isinstance(obj,list) else obj[part]
        obj['label'] = action(obj)['label']
    assert fixed == expected
    assert action({'kind':'map','label':'game.map bits=1 idx1=1 idx2=0 idx3=0'})['label'] == 'game.map idx1=1 idx2=0 idx3=0'
    assert action({'kind':'game_potion_discard','label':'game.game_potion_discard bits=3221225472 idx1=0 idx2=0 idx3=0'})['label'] == 'game.game_potion_discard idx1=0 idx2=0 idx3=0'
    assert action({'kind':'reward_card','label':'Take Anger'})['label'] == 'Take Anger'
    for poisoned in ({'nested':[{'rng_state':1}]}, {'nested':'bits=123'}, {'checkpoint':{}}):
        try:
            assert_public_export(poisoned)
        except AssertionError:
            pass
        else:
            raise AssertionError('Private export guard failed')
    write(output/'public_examples.json', fixed)
    elapsed = time.process_time()-started
    prior = json.loads((BASE/'execution.json').read_text())['builder_upper_cpu_seconds']
    receipt = {'producer_commit':producer,'original_handoff_sha':'a5381f256c433f1a10d8f5981a28c8453f67c3c2',
        'original_seal_sha256':BASE_SEAL,'original_all_output_hashes_unchanged':True,
        'original_examples_sha256':sha(BASE/'public_examples.json'),
        'corrected_examples_sha256':sha(output/'public_examples.json'),
        'changed_label_paths':changes,'changed_labels':len(changes),
        'all_non_label_content_identical':True,'recursive_no_bits_no_private_fields':True,
        'focused_unit_checks_passed':True,'trace_replays':0,'new_games':0,'new_Q':0,'training':0,
        'measured_python_cpu_seconds':elapsed,'additional_setup_hashing_failure_publication_cleanup_upper':40,
        'prior_builder_upper_cpu_seconds':prior,'builder_upper_cpu_seconds':prior+elapsed+40,
        'reviewer_reserve_cpu_seconds':1000,'cpu_hard_upper':3000,
        'consumer':'Issue51 independent Reviewer then Planner','review_delete_date':'2026-11-14',
        'original_source_retention_unchanged':True,
        'planner_priority':'6139666995: incumbent-swap outline is unexecuted and not an automatic successor; standing Issue1 evaluation matrix has priority.'}
    assert receipt['builder_upper_cpu_seconds']+1000 <= 3000
    write(output/'receipt.json',receipt)
    study=Path(__file__).parent
    seal={'producer_commit':producer,'purpose':'STUDY_ONLY_ADDITIVE_PUBLIC_PROJECTION_REPAIR',
        'original_seal_sha256':BASE_SEAL,
        'files':[{'path':p.name,'sha256':sha(p)} for p in sorted(output.glob('*.json'))],
        'source_files':[{'path':n,'sha256':sha(study/n)} for n in ('repair_projection.py','trace_audit.py')],
        'consumer':receipt['consumer'],'review_delete_date':'2026-11-14'}
    write(output/'artifact-manifest.json',seal)
    assert sha(BASE/'artifact-manifest.json') == BASE_SEAL
    print(json.dumps({'store':str(output),'seal_sha256':sha(output/'artifact-manifest.json'),
        'changed_labels':len(changes),'builder_upper_cpu_seconds':receipt['builder_upper_cpu_seconds'],'passed':True}))


if __name__ == '__main__':
    main()
