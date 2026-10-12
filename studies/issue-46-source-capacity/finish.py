"""Continue the SAME CPU ledger after collection, train, then exact640 fresh keys."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from common import CONFIG,write

STORE=Path(sys.argv[1]); SCRIPT=Path(__file__).parent; START=time.process_time()
LEDGER=STORE/'execution.json'; TRAIN_PY='/home/lsmft/stsrl-spikes/py313-torch/bin/python3'

def livecpu(pid):
    try:
        f=Path(f'/proc/{pid}/stat').read_text().split(') ')[1].split()
        return (int(f[11])+int(f[12]))/os.sysconf('SC_CLK_TCK')
    except FileNotFoundError: return 0.

def main():
    while True:
        ledger=json.loads(LEDGER.read_text())
        if ledger['status']!='COLLECTING': break
        time.sleep(15)
    if ledger['status']!='SOURCE_LABELS_COMPLETE': return
    prior=ledger['cumulative_cpu']; charged=0.
    ledger['final_producer']=os.environ['ISSUE46_FINAL_PRODUCER']
    def publish(status,active=None):
        ledger.update(status=status,post_collection_measured_worker_cpu=charged,post_collection_parent_cpu=time.process_time()-START,active=active or {})
        ledger['cumulative_cpu']=prior+charged+time.process_time()-START+sum(v['cpu'] for v in (active or {}).values())
        write(LEDGER,ledger)
    def single(name,python,script):
        nonlocal charged
        publish(name)
        with (STORE/(name+'.log')).open('w') as log:
            p=subprocess.Popen([python,str(SCRIPT/script),str(STORE)],stdout=log,stderr=log,start_new_session=True)
            while True:
                pid,status,usage=os.wait4(p.pid,os.WNOHANG)
                if pid: break
                publish(name,{str(p.pid):{'stage':name,'cpu':livecpu(p.pid)}})
                if ledger['cumulative_cpu']>CONFIG['cpu_cap']-200: os.killpg(p.pid,signal.SIGTERM)
                time.sleep(1)
            p.returncode=os.waitstatus_to_exitcode(status); charged+=usage.ru_utime+usage.ru_stime
        if p.returncode: publish('INCOMPLETE_'+name); raise ValueError(name+' failed')
    single('MODEL_VERIFICATION',TRAIN_PY,'verify_model.py')
    single('TRAINING',TRAIN_PY,'train.py')
    pending=[(s,a) for s in range(441001,441129) for a in ('SxT','SxB','LxT','LxB','Expert')]
    active={}; failed=[]; completed=[]; stopped=False
    while pending or active:
        mem=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
        slots=max(1,min(8,max(0,mem-4*1024**3)//(8*1024**3)))
        while pending and len(active)<slots and not stopped:
            seed,arm=pending.pop(0); log=(STORE/f'{seed}-{arm}.log').open('w')
            p=subprocess.Popen([sys.executable,str(SCRIPT/'evaluate.py'),str(STORE),str(seed),arm],stdout=log,stderr=log,start_new_session=True)
            active[p.pid]=(seed,arm,p,log)
        publish('EVALUATING',{str(pid):{'seed':r[0],'arm':r[1],'cpu':livecpu(pid)} for pid,r in active.items()})
        if ledger['cumulative_cpu']>=CONFIG['cpu_cap']-200: stopped=True; pending=[]
        if stopped:
            for pid in active:
                try: os.killpg(pid,signal.SIGTERM)
                except ProcessLookupError: pass
        for pid in list(active):
            done,status,usage=os.wait4(pid,os.WNOHANG)
            if not done: continue
            seed,arm,p,log=active.pop(pid); p.returncode=os.waitstatus_to_exitcode(status); log.close(); charged+=usage.ru_utime+usage.ru_stime
            if p.returncode: failed.append({'seed':seed,'arm':arm,'exit':p.returncode}); stopped=True; pending=[]
            else: completed.append([seed,arm])
        ledger.update(fresh_completed=completed,fresh_failed=failed,fresh_pending=len(pending))
        if active: time.sleep(1)
    if stopped: publish('RESOURCE_BLOCKED' if ledger['cumulative_cpu']>=CONFIG['cpu_cap']-200 else 'INCOMPLETE_FRESH'); return
    single('AUDITING',sys.executable,'audit.py')
    single('REPORTING',sys.executable,'report.py')
    publish('COMPLETE_PENDING_INDEPENDENT_REVIEW')

if __name__=='__main__':
    try: main()
    except BaseException as exc:
        write(STORE/'finish-failure.json',{'error':repr(exc),'parent_cpu':time.process_time()-START}); raise
