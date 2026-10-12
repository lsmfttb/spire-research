"""Focused procedure verification on fixed real roots and analytic pair cases."""
import json
import math
from pathlib import Path
import sys
import torch
from common import write
from train import initialize,state,encode,objective,ranking
from student import Scorer

def main():
    store=Path(sys.argv[1]); tables=[json.loads((store/'inputs'/(str(o)+'.json')).read_text()) for o in (437001,437002)]
    data=encode(tables); model=initialize(446101); original=Scorer(446101)
    errors=[abs(float(model(x).detach())-original.forward(x.tolist())[0]) for x in data['x']]
    assert max(errors)<1e-12 and sum(p.numel() for p in model.parameters())==2081
    # Two informative roots with1 and2 pairs must each receive exactly1/2 mass.
    fake={'informative':2,'high':torch.tensor([1,3,4]),'low':torch.tensor([0,2,2]),'pair_weight':torch.tensor([.5,.25,.25],dtype=torch.float64)}
    score=torch.tensor([0.,1.,0.,1.,2.],dtype=torch.float64,requires_grad=True)
    loss=objective(score,fake,'P'); expected=.75*math.log1p(math.exp(-1))+.25*math.log1p(math.exp(-2))
    assert abs(float(loss)-expected)<1e-12; loss.backward()
    assert score.grad[1]<0 and score.grad[0]>0 and score.grad[4]<0
    # Actual pair order follows only unequal authentic Q; singleton/ties give no pair.
    for high,low in zip(data['high'].tolist(),data['low'].tolist()): assert data['target'][high]>data['target'][low]
    assert abs(float(data['weight'].sum())-1)<1e-12
    if data['informative']: assert abs(float(data['pair_weight'].sum())-1)<1e-12
    tied=ranking(torch.zeros(len(data['x']),dtype=torch.float64),data)
    assert all(r['chosen']==0 for r in tied['roots'])
    assert tied['mean_regret']==sum(max(t['q'])-t['q'][0] for t in tables)/len(tables)
    optimizer=torch.optim.SGD(model.parameters(),lr=.01); optimizer.zero_grad(); objective(model(data['x']).flatten(),data,'A').backward(); optimizer.step()
    original.update([(x.tolist(),float(y),float(w)) for x,y,w in zip(data['x'],data['target'],data['weight'])],lr=.01)
    update_error=max(abs(float(model(x).detach())-original.forward(x.tolist())[0]) for x in data['x'])
    assert update_error<1e-12
    assert state(model)!=state(initialize(446101))
    write(store/'verification.json',{'passed':True,'tiny_forward_max_error':max(errors),'tiny_sgd_max_error':update_error,'parameters':2081,'pair_formula_and_gradient':True,'root_pair_weights':True,'native_ordinal_tie_break':True,'actual_return_pair_order':True,'absolute_gradient_update':True,'no_fresh_runs_opened':True})

if __name__=='__main__': main()
