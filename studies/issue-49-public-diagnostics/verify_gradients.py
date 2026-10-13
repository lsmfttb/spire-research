"""Independent central-difference check; small abstract roots, no fitting."""
import json,sys,time
from pathlib import Path
import torch
from diagnose import initialize,objective,grads,DT
def main():
    start=time.process_time();model=initialize('T',446101)
    x=torch.tensor([[((i*7+j*3)%19-9)/10 for j in range(128)] for i in range(5)],dtype=DT)
    groups=[(0,3,{'q':[12,8,12]}),(3,2,{'q':[4,6]})]
    errors={}
    for arm in ('A','P'):
        s=model(x).flatten();g=grads(model,objective(s,groups,arm)[0]);off=0;maxerr=0.
        for p in model.parameters():
            for i in (0,p.numel()-1):
                old=float(p.flatten()[i].detach());eps=1e-6
                with torch.no_grad():p.flatten()[i]=old+eps
                high=float(objective(model(x).flatten(),groups,arm)[0].detach())
                with torch.no_grad():p.flatten()[i]=old-eps
                low=float(objective(model(x).flatten(),groups,arm)[0].detach())
                with torch.no_grad():p.flatten()[i]=old
                maxerr=max(maxerr,abs((high-low)/(2*eps)-float(g[off+i])))
            off+=p.numel()
        assert maxerr<1e-8;errors[arm]=maxerr
        if arm=='P':assert abs(float(g[-1]))<1e-14
    receipt={'passed':True,'central_difference_max_errors':errors,'epsilon':1e-6,'tolerance':1e-8,'preference_output_bias_cancels':True,'cpu_seconds':time.process_time()-start,'no_optimizer_steps':True}
    (Path(sys.argv[1])/'gradient-verification.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':main()
