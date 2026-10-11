"""Study-specific public scorer training. No simulator/executor imports.

Q tables are supervised targets only; features consume the serialized public
column, never root IDs, costs, provenance, continuation seeds or Q values.
"""
import copy
import hashlib
import json
from pathlib import Path
from student import Scorer, features, treatment

CONFIG = json.loads(Path(__file__).with_name('config.json').read_text())


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':'), allow_nan=False).encode()).hexdigest()


def examples(tables, arm):
    result = []
    for table in tables:
        value = treatment(table['serialized'], arm)
        if len(table['q']) != len(value['legal_options']) or len(table['q']) < 2:
            raise ValueError('incomplete all-action target table')
        for ordinal, q in enumerate(table['q']):
            result.append((features(value, ordinal), q/60., 1./len(table['q'])))
    if not result:
        raise ValueError('empty target table')
    return result


def mse(model, tables, arm):
    errors = []
    for table in tables:
        scores = model.scores(table['serialized'], arm)
        if len(scores) != len(table['q']):
            raise ValueError('validation all-action mismatch')
        errors.append(sum((p-q/60.)**2 for p,q in zip(scores,table['q']))/len(scores))
    return sum(errors)/len(errors)


def fit(train, validation):
    """Exactly two seeds × 100 updates/arm; heldout has no entry point here."""
    if {r['split'] for r in train} != {'train'} or {r['split'] for r in validation} != {'validation'}:
        raise ValueError('training/validation reservation violated')
    if {r['owner'] for r in train} & {r['owner'] for r in validation}:
        raise ValueError('source owner overlap')
    target_identity = sha([[r['owner'],r['root_id'],r['q']] for r in train])
    output = {}
    for arm in ('visible','masked'):
        rows = examples(train, arm)
        best = None; observations = []
        for seed in CONFIG['model_seeds']:
            model = Scorer(seed)
            for update in range(1,101):
                gradient = model.update(rows, lr=.01)
                loss = mse(model, validation, arm)
                key = (loss, update, seed)
                observations.append({'seed':seed,'update':update,'validation_mse':loss,'gradient_l1':gradient})
                if best is None or key < best[0]:
                    best = (key, model.state())
        checkpoint = {'arm':arm, 'state':best[1], 'validation_mse':best[0][0],
                      'update':best[0][1], 'model_seed':best[0][2], 'config_sha256':sha(CONFIG),
                      'train_targets_sha256':target_identity,
                      'normalizer':'sign*log1p(abs(value))/10; target divide by 60',
                      'normalizer_sha256':sha(CONFIG['model']['normalizer']),
                      'parameters':2081, 'purpose':'STUDY_ONLY'}
        checkpoint['checkpoint_sha256'] = sha(checkpoint)
        output[arm] = {'checkpoint':checkpoint, 'optimization':observations}
    return output


def load(checkpoint):
    value = copy.deepcopy(checkpoint)
    expected = value.pop('checkpoint_sha256')
    if sha(value) != expected or value['config_sha256'] != sha(CONFIG):
        raise ValueError('frozen model identity mismatch')
    model = Scorer(value['model_seed'])
    for name in ('w','b','v','c'):
        setattr(model, name, value['state'][name])
    return model
