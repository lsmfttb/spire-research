"""Leased worker; kernel limits precede source/native imports."""
import json
import os
from pathlib import Path
import resource
import sys
import time

def execute(path):
    begin=time.monotonic(); cpu=time.process_time()
    from replication import CONTRACT,HERE,dependencies,sha,read,train_block,evaluate,manifest,report
    from launch import source_binding
    request=read(path); ident=request['identity']
    dep,tables,roots=dependencies(os.environ['ISSUE44_SOURCE_CODE'])
    if dep!=ident['source'] or source_binding(ident['producer_commit'])!=ident['wrapper_blobs'] or sha(CONTRACT)!=ident['contract_sha256']:
        raise ValueError('worker runtime/source identity drift')
    from formal import Completed
    store=Completed(request['store'],ident); key=request['key']
    if store.get(key) is not None: return
    payload=request['payload']; kind=request['kind']; base=Path(request['directory'])
    if read(base/'identity.json')!=ident: raise ValueError('lineage identity mismatch')
    if not ident['synthetic']:
        authority=read(base/'authorization.json')
        if sha(authority)!=ident['authorization_sha256'] or authority['producer_commit']!=ident['producer_commit'] or authority['directory']!=str(base):
            raise ValueError('formal authorization binding changed')
    store.event({'boundary':'job_started','key':key,'kind':kind,'pid':os.getpid()})
    adapter=None
    try:
        if kind=='readiness':
            if not ident['synthetic']: raise ValueError('readiness only')
            import unittest,test_replication
            result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_replication))
            if not result.wasSuccessful(): raise ValueError('readiness tests failed')
            value={'status':'SYNTHETIC_READINESS_ONLY','tests':result.testsRun,'formal_models_or_runs':0}
        elif kind=='train':
            if ident['synthetic']: raise ValueError('formal job forbidden in readiness')
            value={'block':payload['block'],'models':train_block(tables,payload['block'])}
        elif kind in ('fresh','report'):
            if ident['synthetic']: raise ValueError('formal job forbidden in readiness')
            frozen=read(payload['models'])
            if frozen['identity']!=ident or sha(frozen)!=payload['model_freeze_sha256']: raise ValueError('model freeze changed')
            blocks={int(b):v for b,v in frozen['blocks'].items()}; plan=manifest(blocks)
            if read(base/'evaluation-plan.json')!={'identity':ident,'model_freeze_sha256':sha(frozen),'plan':plan}: raise ValueError('run manifest changed')
            def eval_store(p): return Completed(base/'evaluation'/str(p['block'])/p['key'],ident)
            if kind=='fresh':
                p=payload['plan']
                if p not in plan: raise ValueError('unexpected fresh key')
                from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
                adapter=LightSpeedAdapter(ascension=20)
                def progress(snapshot,actions,context,index):
                    if index%25==0: store.event({'boundary':'native_progress','key':key,'step':index,'cpu':time.process_time()-cpu,'wall':time.monotonic()-begin})
                evaluate(adapter,p,blocks[p['block']],eval_store(p),progress)
                value={'evaluation_key':[p['block'],p['key'],p['seed'],p['arm']]}
            else:
                rows=[]
                for p in plan:
                    row=eval_store(p).get([p['key'],p['seed'],p['arm']])
                    if row is None: raise ValueError('INCOMPLETE: missing original run')
                    rows.append({**row,'block':p['block']})
                value={'report':report(rows,blocks,roots['train'])}
        else: raise ValueError('unexpected kind')
        store.publish(key,{'terminal':True,'problems':[],**value,'cpu':time.process_time()-cpu,
            'wall':time.monotonic()-begin,'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    except BaseException as error:
        store.event({'boundary':'job_failed','key':key,'error':repr(error),'cpu':time.process_time()-cpu,'wall':time.monotonic()-begin}); raise
    finally:
        if adapter is not None: adapter.close()

if __name__=='__main__':
    path,cpu,rss,token=sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_CPU,(int(cpu),int(cpu)))
    resource.setrlimit(resource.RLIMIT_AS,(int(rss),int(rss)))
    execute(path)
