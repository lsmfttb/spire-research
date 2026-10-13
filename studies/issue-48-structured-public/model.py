"""Frozen S structured policy and exact frozen A hash comparator."""
import copy
from common import sha
from student import Scorer
from structured import Structured

def load(checkpoint):
    value=copy.deepcopy(checkpoint); digest=value.pop('sha256')
    if sha(value)!=digest: raise ValueError('checkpoint identity invalid')
    if value['cell']=='S': return Structured(checkpoint)
    if digest!='0ecb46dd7a39d2aa33a12d50c60be15d438a1c9cb3d9bc893183b9143d55f555':
        raise ValueError('Comparator must be exact frozen Issue47 A')
    model=Scorer(value['seed'])
    for name in ('w','b','v','c'): setattr(model,name,value['state'][name])
    return model
