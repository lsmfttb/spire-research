"""Constructive old-equal/new-unequal deck binding witness, not native labels."""
import copy,json,sys,time
from pathlib import Path
from collections import Counter
import torch
from common import write,CONFIG
import student as old
import binding_student as new
from train import initialize,state

def main():
    start=time.process_time(); store=Path(sys.argv[1]); table=json.loads((store/'inputs/437001.json').read_text()); public=old.treatment(table['root']['serialized'],'visible')
    cards=public['public_state']['persistent_resources']['deck']['value']; first=copy.deepcopy(next(c for c in cards if c['id']==25)); second=copy.deepcopy(next(c for c in cards if c['id']==104))
    first.update(deck_index=0,upgraded=True,upgrade_count=1,misc=0); second.update(deck_index=1,upgraded=False,upgrade_count=0,misc=0)
    left=copy.deepcopy(public); left['public_state']['persistent_resources']['deck']['value']=[first,second]
    right=copy.deepcopy(left); right['public_state']['persistent_resources']['deck']['value'][0].update(upgraded=False,upgrade_count=0); right['public_state']['persistent_resources']['deck']['value'][1].update(upgraded=True,upgrade_count=1)
    old_left=old.features(left,0); old_right=old.features(right,0); new_left=new.features(left,0); new_right=new.features(right,0)
    assert left!=right and old_left==old_right and new_left!=new_right
    assert Counter(old._tokens({'state':left['public_state']}))==Counter(old._tokens({'state':right['public_state']}))
    moved=copy.deepcopy(first); moved['deck_index']=9; assert new.deck_token(moved)==new.deck_token(first)
    duplicate=copy.deepcopy(left); duplicate['public_state']['persistent_resources']['deck']['value']=[first,first]
    tokens=list(new._tokens({'state':duplicate['public_state']})); assert sum(t==new.deck_token(first) for t,w in tokens)==2
    assert new.features(left,0)==new.features(copy.deepcopy(left),0)
    # Initializer and one full-batch root-balanced SGD step match pure Scorer.
    rows=[(new.features(new.treatment(table['root']['serialized'],'visible'),i),q/60,1/len(table['q'])) for i,q in enumerate(table['q'])]
    pure=new.Scorer(CONFIG['model_seeds'][0]); m=initialize(CONFIG['model_seeds'][0]); assert state(m)==pure.state(); assert sum(p.numel() for p in m.parameters())==2081
    x=torch.tensor([r[0] for r in rows],dtype=torch.float64); y=torch.tensor([r[1] for r in rows],dtype=torch.float64); weight=torch.tensor([r[2] for r in rows],dtype=torch.float64)
    objective=((m(x).flatten()-y).square()*weight).sum(); objective.backward(); gradient=float(m[0].weight.grad.abs().max()); assert gradient>0
    optimizer=torch.optim.SGD(m.parameters(),lr=.01,weight_decay=0); optimizer.step(); pure.update(rows,lr=.01)
    error=max(abs(pure.forward(r[0])[0]-float(m(torch.tensor(r[0],dtype=torch.float64)).detach()[0])) for r in rows); assert error<1e-12
    write(store/'verification.json',{'passed':True,'witness_left':left,'witness_right':right,'raw_unequal':True,'old_tokens_equal':True,'old_features_max_difference':max(abs(a-b) for a,b in zip(old_left,old_right)),'new_features_max_difference':max(abs(a-b) for a,b in zip(new_left,new_right)),'position_independent_composite':True,'duplicates_preserved':True,'deterministic':True,'exact_initializer':True,'parameters':2081,'torch_pure_SGD_max_error':error,'nonzero_gradient_max_abs':gradient,'cpu':time.process_time()-start,'witness_not_native_source_or_new_labels':True})
if __name__=='__main__': main()
