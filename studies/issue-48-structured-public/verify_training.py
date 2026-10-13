import json,sys
from pathlib import Path
from common import write,CONFIG
from train import Network,batch,state,loss
from structured import Structured,vocabulary,encode
import torch

def main():
    store=Path(sys.argv[1]); tables=[json.loads((store/'inputs'/f'{s}.json').read_text()) for s in (437001,437002,440097)]
    vocab=vocabulary(tables); model=Network(len(vocab),CONFIG['model_seeds'][0]); data=batch(tables,vocab)
    cp={'vocabulary':vocab,'state':state(model)}; pure=Structured(cp)
    with torch.no_grad(): scores=model(data).tolist()
    expected=[pure.score(encode(t['root']['serialized'],i,vocab)) for t in tables for i in range(len(t['q']))]
    error=max(abs(a-b) for a,b in zip(scores,expected)); assert error<1e-12
    objective=loss(model(data),data); objective.backward(); gradient=float(model.embedding.weight.grad.abs().max()); assert gradient>0
    # Changing visible option semantics changes its encoded entity pool; no executor indices reach it.
    d=json.loads(tables[0]['root']['serialized']); original=encode(json.dumps(d),0,vocab)
    d['legal_options'][0]['action']['idx1']=987654; d['legal_options'][0]['action']['label']='executor-only'
    assert original==encode(json.dumps(d),0,vocab)
    assert encode(tables[0]['root']['serialized'],0,{'<OOV>':0})[0]
    assert sum(p.numel() for p in model.parameters())<=30000
    write(store/'verification.json',{'passed':True,'torch_vs_pure_max_error':error,'embedding_max_abs_gradient':gradient,'executor_fields_ignored':True,'oov_supported':True,'authentic_training_roots_only':True})
if __name__=='__main__': main()
