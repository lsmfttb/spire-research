"""Focused verification on the first actual fixed source table, no fresh scores."""
import json
from pathlib import Path
import sys
import torch
from common import sha,write,CONFIG
from student import Scorer,features,treatment
from train import initialize,state
from model import Bigger

store=Path(sys.argv[1]); table=json.loads((store/'437001/table.json').read_text())
value=treatment(table['root']['serialized'],'visible')
xs=[features(value,i) for i in range(len(table['q']))]
x=torch.tensor(xs,dtype=torch.float64)
y=torch.tensor([[q/60] for q in table['q']],dtype=torch.float64)
tiny=Scorer(CONFIG['model_seeds'][0]); t=initialize('T',CONFIG['model_seeds'][0])
with torch.no_grad():
    error=max(abs(tiny.forward(v)[0]-float(t(x)[i,0])) for i,v in enumerate(xs))
assert error<1e-12
tiny.update([(v,q/60,1/len(xs)) for v,q in zip(xs,table['q'])],lr=.01)
optimizer=torch.optim.SGD(t.parameters(),lr=.01)
optimizer.zero_grad(); ((t(x)-y).square().mean()).backward(); optimizer.step()
with torch.no_grad():
    update_error=max(abs(tiny.forward(v)[0]-float(t(x)[i,0])) for i,v in enumerate(xs))
assert update_error<1e-12
b=initialize('B',CONFIG['model_seeds'][0]); pure=Bigger(state(b,'B'))
with torch.no_grad(): bigger_error=max(abs(pure.forward(v)-float(b(x)[i,0])) for i,v in enumerate(xs))
assert bigger_error<1e-12
assert sum(p.numel() for p in t.parameters())==2081
assert sum(p.numel() for p in b.parameters())==99073
before=sha(state(b,'B')); loss=(b(x)-y).square().mean(); loss.backward()
assert sum(float(p.grad.abs().sum()) for p in b.parameters())>0
torch.optim.SGD(b.parameters(),lr=.01).step()
assert sha(state(b,'B'))!=before
write(store/'model-verification.json',{'actual_owner':437001,'tiny_forward_error':error,'tiny_sgd_error':update_error,'bigger_forward_error':bigger_error,'parameter_counts':[2081,99073],'bigger_nonzero_gradient_and_weight_update':True,'fresh_evaluation_opened':False})
