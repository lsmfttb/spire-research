"""Public-only frozen scorers. No executor, native snapshot, or provenance input."""
import copy
from student import Scorer
from common import sha

def load(checkpoint):
    value=copy.deepcopy(checkpoint); digest=value.pop('sha256')
    if sha(value)!=digest: raise ValueError('checkpoint identity invalid')
    if value['capacity']=='T':
        model=Scorer(value['seed'])
        for name in ('w','b','v','c'): setattr(model,name,value['state'][name])
        return model
    raise ValueError('Issue47 only permits the frozen tiny scorer')
