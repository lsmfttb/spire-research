"""Public-only frozen scorers. No executor, native snapshot, or provenance input."""
import copy
import math
from student import Scorer,features,treatment
from common import sha

class Bigger:
    def __init__(self,state): self.state=state
    def forward(self,x):
        for w,b in zip(self.state['weights'][:-1],self.state['biases'][:-1]):
            x=[math.tanh(sum(a*c for a,c in zip(row,x))+bias) for row,bias in zip(w,b)]
        return sum(a*c for a,c in zip(self.state['weights'][-1][0],x))+self.state['biases'][-1][0]
    def choose(self,serialized,arm='visible'):
        value=treatment(serialized,arm)
        return max(range(len(value['legal_options'])),key=lambda i:self.forward(features(value,i)))

def load(checkpoint):
    value=copy.deepcopy(checkpoint); digest=value.pop('sha256')
    if sha(value)!=digest: raise ValueError('checkpoint identity invalid')
    if value['capacity']=='T':
        model=Scorer(value['seed'])
        for name in ('w','b','v','c'): setattr(model,name,value['state'][name])
        return model
    if value['capacity']!='B': raise ValueError('unknown capacity')
    return Bigger(value['state'])
