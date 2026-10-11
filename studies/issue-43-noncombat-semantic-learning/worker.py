"""Single leased Issue43 job. Limits are installed before study/native imports."""
import json
import os
from pathlib import Path
import resource
import sys
import time


def fixture(family):
    return {'public_state':{'screen':family,'persistent_resources':{'hp':50,'deck':[{'id':1,'upgraded':False}]},
        'visible_act_boss':'HEXAGHOST','map_graph':{'nodes':[{'x':1,'y':0,'room_symbol':'M'}],
        'edges':[{'from_x':1,'to_x':2,'from_y':0,'to_y':1}]},'current_map_node':{'x':1,'y':0},
        'screen_context':{'coverage_status':'supported'}},
        'legal_options':[{'candidate_index':i,'action':{'kind':'reward_card' if family=='REWARDS' else 'map','idx1':i},
            'semantics':{'card':{'id':i+5,'upgraded':False},'destination':{'x':i,'y':1,'room_symbol':'M'}}} for i in range(2)]}


def synthetic_source(owner,split):
    from formal import CONFIG,digest
    roots=[]
    for step,family in enumerate(('MAP_SCREEN','REWARDS')):
        public=fixture(family); serialized=json.dumps(public,sort_keys=True)
        roots.append({'split':split,'owner':owner,'family':family,'step':step,'source_floor':step+1,
            'legal_count':2,'serialized_public_input':serialized,'public_input_sha256':digest(public),
            'candidate_sha256':[digest(v) for v in public['legal_options']],
            'privileged_checkpoint_fingerprint_sha256':digest(['SYNTHETIC',owner,step]),
            'source_driver_seed':CONFIG['source_driver_seed']})
    return roots,{'owner':owner,'terminal':True,'problems':[],'screen_counts':{'unsupported_other':1,'supported_single_action':0}}


def fault(payload):
    """Only synthetic negative tests can request these bounded failure fixtures."""
    if payload['mode']=='exit': os._exit(17)
    if payload['mode']=='sleep': time.sleep(payload['seconds'])
    if payload['mode']=='cpu':
        until=time.process_time()+payload['seconds']
        while time.process_time()<until: pass
    if payload['mode']=='child':
        pid=os.fork()
        if pid==0:
            while True: pass
        Path(payload['pid_file']).write_text(str(pid)); time.sleep(payload['seconds'])
    if payload['mode']=='rss':
        blocks=[]
        while True: blocks.append(bytearray(1024**2))


def execute(request):
    from budget import overlay,sha
    from formal import Completed,CONFIG,capture_owner,execute_owner,keys,targets,evaluate_one
    from learning import fit,load
    from reporting import heldout,fresh
    identity=request['identity']; synthetic=identity['synthetic']
    if identity['config_sha256']!=sha(CONFIG) or identity['budget_overlay_sha256']!=sha(overlay()):
        raise ValueError('worker config/budget identity changed')
    from launcher import validate_runtime
    validate_runtime(identity)
    kind=request['kind']; payload=request['payload']; store=Completed(request['store'],identity)
    if store.get(request['key']) is not None: return
    store.event({'boundary':'job_started','key':request['key'],'kind':kind,'pid':os.getpid()})
    begin=time.monotonic(); cpu=time.process_time(); adapter=None
    def progress(snapshot,actions,context,index):
        if index%25==0:
            store.event({'boundary':'native_progress','kind':kind,'key':request['key'],'step':index,
                'cpu':time.process_time()-cpu,'wall':time.monotonic()-begin})
    try:
        if kind=='fault':
            if not synthetic: raise ValueError('negative test only')
            fault(payload); result={}
        elif kind=='source':
            if synthetic: roots,cost=synthetic_source(payload['owner'],payload['split'])
            else:
                from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
                adapter=LightSpeedAdapter(ascension=20)
                captured,_,cost=capture_owner(adapter,payload['owner'],payload['split'],progress); roots=list(captured.values())
            result={'roots':roots,'source_cost':cost}
        elif kind=='labels':
            branch_store=Completed(payload['branch_store'],identity); roots=payload['roots']
            if synthetic:
                for root in roots:
                    for key in keys(root):
                        if branch_store.get(key) is None:
                            branch_store.publish(key,{'terminal':True,'problems':[],'terminal_floor':root['source_floor']+10+key[4],
                                'outcome':'PLAYER_LOSS','max_act':1,'heart_encountered':False,'heart_win':False,
                                'final_raw_privileged':{},'steps':1,'cpu':0,'wall':0})
            else:
                from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
                adapter=LightSpeedAdapter(ascension=20); execute_owner(adapter,roots,branch_store,progress)
            result={'branch_keys':[k for r in roots for k in keys(r)]}
        elif kind=='train':
            # This request contains only train/validation files. Heldout is opened
            # by a later reporting job after this immutable model result exists.
            if set(payload)!= {'train','validation'}: raise ValueError('train request boundary')
            train=json.loads(Path(payload['train']).read_text()); validation=json.loads(Path(payload['validation']).read_text())
            result={'models':fit(train,validation)}
        elif kind=='heldout':
            checkpoints=json.loads(Path(payload['models']).read_text())['checkpoints']
            result={'report':heldout(json.loads(Path(payload['tables']).read_text()),payload['roots'],checkpoints)}
        elif kind=='fresh':
            checkpoints=json.loads(Path(payload['models']).read_text())['checkpoints']; plan=payload['plan']
            eval_store=Completed(payload['eval_store'],identity); key=[plan['key'],plan['seed'],plan['arm']]
            if synthetic:
                previous=eval_store.get(key)
                if previous is None:
                    choices=[]
                    for step,family in enumerate(('MAP_SCREEN','REWARDS')):
                        public=fixture(family); serialized=json.dumps(public,sort_keys=True)
                        ordinal=load(checkpoints[plan['arm']]).choose(serialized,plan['arm']) if plan['arm']!='expert_source' else 0
                        choices.append({'family':family,'step':step,'ordinal':ordinal,'public_sha256':sha(public),
                            'serialized_public_input':serialized,'route':'expert' if plan['arm']=='expert_source' else 'learned'})
                    eval_store.publish(key,{'terminal':True,'problems':[],'seed':plan['seed'],'arm':plan['arm'],
                        'floor':10,'outcome':'PLAYER_LOSS','max_act':1,'heart_encountered':False,'heart_win':False,
                        'choices':choices,'final_raw_privileged':{},'steps':2,'cpu':0,'wall':0,'battle_decisions':0})
            else:
                from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
                adapter=LightSpeedAdapter(ascension=20); evaluate_one(adapter,plan,checkpoints,eval_store,progress)
            result={'evaluation_key':key}
        elif kind=='fresh_report':
            checkpoints=json.loads(Path(payload['models']).read_text())['checkpoints']
            result={'report':fresh(json.loads(Path(payload['runs']).read_text()),checkpoints,payload['train_roots'],synthetic)}
        else: raise ValueError('unknown job kind')
        store.publish(request['key'],{'terminal':True,'problems':[],**result,
            'cpu':time.process_time()-cpu,'wall':time.monotonic()-begin,
            'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'purpose':'SYNTHETIC_READINESS_ONLY' if synthetic else 'STUDY_ONLY'})
    except BaseException as error:
        store.event({'boundary':'job_failed','kind':kind,'key':request['key'],'error':repr(error),
            'cpu':time.process_time()-cpu,'wall':time.monotonic()-begin}); raise
    finally:
        if adapter is not None: adapter.close()


if __name__=='__main__':
    request_path,cpu,rss,token=sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_CPU,(int(cpu),int(cpu)))
    resource.setrlimit(resource.RLIMIT_AS,(int(rss),int(rss)))
    execute(json.loads(Path(request_path).read_text()))
