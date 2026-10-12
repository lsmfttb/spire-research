"""One small durable task manifest; observed CPU, never full unused leases."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time
import resource
from worker import write

HERE=Path(__file__).resolve().parent
def proc_cpu(pid):
    try:
        fields=Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()
        return (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
    except FileNotFoundError: return 0.

def report(base,state):
    rows=[json.loads(p.read_text()) for p in base.glob('437*-o*.json') if '-progress' not in p.name]
    valid={(r['seed'],r['arm']):r for r in rows if r['valid']}
    result={'classification':state.get('stop','INCOMPLETE'),'expected_runs':192,'completed':len(rows),'valid':len(valid),
            'cpu':state['cpu'],'wall':time.time()-state['started_utc'],'producer':state['producer'],
            'source':state['source'],'rows':rows,'driver_seed':433042,'limitations':'Privileged Battle; fixed Expert potion management. No optimal retention or normal-public/Heart strength claim.'}
    if any('POTION_ACTION_MAPPING_INVALID' in str(r['problems']) for r in rows): result['classification']='POTION_ACTION_MAPPING_INVALID'
    result['secondary']={}
    for arm in ('off','on'):
        arm_rows=[r for r in rows if r['arm']==arm and r['valid']]
        metrics={}
        for r in arm_rows:
            for k,v in r['metrics'].items(): metrics[k]=metrics.get(k,0)+v
        result['secondary'][arm]={'valid_runs':len(arm_rows),'act_reach':{str(a):sum(r['max_act']>=a for r in arm_rows) for a in (2,3,4)},
            'outcomes':{o:sum(r['outcome']==o for r in arm_rows) for o in ('PLAYER_WIN','PLAYER_LOSS')},
            'terminal_hp':[r['final_raw'].get('cur_hp') for r in arm_rows], 'metrics':metrics,
            'potion_available_stratum':{label:{'n':len(group),'floors':[r['floor'] for r in group]} for label,group in
                [('observed',[r for r in arm_rows if r['metrics'].get('battle_potion_eligible_decisions',0)>0]),
                 ('unobserved',[r for r in arm_rows if r['metrics'].get('battle_potion_eligible_decisions',0)==0])]}}
    if len(valid)==192:
        ds=[valid[s,'on']['floor']-valid[s,'off']['floor'] for s in range(437001,437097)]
        rng=random.Random(437099); bs=sorted(sum(rng.choice(ds) for _ in ds)/96 for _ in range(5000))
        def pct(q):
            index=(len(bs)-1)*q; lo=int(index); return bs[lo]+(bs[min(lo+1,len(bs)-1)]-bs[lo])*(index-lo)
        lower,upper=pct(.025),pct(.975)
        result.update(mean=sum(ds)/96,ci95=[lower,upper],paired_differences=ds,
                      signs={name:sum(test(d) for d in ds) for name,test in [('positive',lambda d:d>0),('negative',lambda d:d<0),('tie',lambda d:d==0)]})
        result['classification']='POTION_ENABLED_A20_FLOOR_GAIN' if lower>0 else 'POTION_ENABLED_A20_FLOOR_HARM' if upper<0 else 'POTION_EFFECT_NOT_ESTABLISHED'
    write(base/'report.json',result)

def main():
    base=Path(sys.argv[1]); state=json.loads((base/'execution.json').read_text())
    lock=(base/'run.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if state['active']: raise ValueError('Inspect active PID ownership before resume')
    if state.get('stop'): raise ValueError('Stopped task: no automatic incomplete-run replacement')
    state['producer']=sys.argv[2]; state['source']=json.loads((base/'source.json').read_text())
    os.environ['ISSUE45_PRODUCER']=state['producer']
    began_cpu=time.process_time(); original_cpu=state['cpu']; live={}; observed={}
    pending=[(s,a) for s in range(437001,437097) for a in ('off','on') if f'{s}-{a}' not in state['completed']]
    def sample():
        state['cpu']=original_cpu+time.process_time()-began_cpu+sum(observed.values())+sum(proc_cpu(p.pid) for p in live.values())
        state['wall']=time.time()-state['started_utc']
        state['active']={k:{'pid':p.pid,'cpu':proc_cpu(p.pid)} for k,p in live.items()}
        write(base/'execution.json',state)
    def settle(name,p,blocking=False):
        pid,status,usage=os.wait4(p.pid,0 if blocking else os.WNOHANG)
        if not pid:return None
        p.returncode=os.waitstatus_to_exitcode(status); observed[name]=usage.ru_utime+usage.ru_stime
        del live[name]; state['completed'][name]={'exit':p.returncode,'cpu':observed[name]}
        return p.returncode
    try:
        while pending or live:
            sample()
            if state['cpu']>=11970 or state['wall']>=3570: raise RuntimeError('RESOURCE_BLOCKED')
            available=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024
            while pending and len(live)<8 and available>=4*1024**3:
                s,a=pending.pop(0); name=f'{s}-{a}'
                with (base/(name+'.log')).open('ab') as log:
                    live[name]=subprocess.Popen([sys.executable,str(HERE/'worker.py'),str(base),str(s),a],start_new_session=True,stdout=log,stderr=subprocess.STDOUT)
                sample()
            if pending and not live: raise RuntimeError('RESOURCE_BLOCKED: available memory')
            for name,p in list(live.items()):
                code=settle(name,p)
                if code is not None and code!=0: raise RuntimeError('INCOMPLETE: '+name+' exit '+str(code))
            if live: time.sleep(.5)
    except BaseException as error:
        state['stop']=str(error)
        for name,p in list(live.items()):
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            settle(name,p,True)
    finally:
        sample(); report(base,state); sample()
        state['finished_utc']=time.time(); write(base/'execution.json',state)

if __name__=='__main__': main()
