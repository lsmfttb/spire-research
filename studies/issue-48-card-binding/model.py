"""Exact frozen A plus B whose sole difference is extra deck composite token."""
import copy
from common import sha,CONFIG
from student import Scorer as Original
from binding_student import Scorer as Binding

def load(checkpoint):
    value=copy.deepcopy(checkpoint); digest=value.pop('sha256')
    if sha(value)!=digest: raise ValueError('checkpoint identity invalid')
    if value['cell']=='A':
        if digest!=CONFIG['frozen_A_sha256']: raise ValueError('Comparator changed')
        model=Original(value['seed'])
    elif value['cell']=='B': model=Binding(value['seed'])
    else: raise ValueError('Unknown arm')
    for name in ('w','b','v','c'): setattr(model,name,value['state'][name])
    return model
