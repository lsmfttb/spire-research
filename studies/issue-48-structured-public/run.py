"""One40k CPU task ledger, fixed inputs/training/384 runs/full audit/report."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from common import CONFIG,write

STORE=Path(sys.argv[1]); SCRIPT=Path(__file__).parent; START=time.process_time()
TRAIN_PY='/home/lsmft/stsrl-spikes/py313-torch/bin/python3'

def livecpu(pid):
    try:
        f=Path(f'/proc/{pid}/stat').read_text().split(') ')[1].split()
        return (int(f[11])+int(f[12]))/os.sysconf('SC_CLK_TCK')
    except FileNotFoundError: return 0.

def main():
    STORE.mkdir(parents=True,exist_ok=True); ledger_path=STORE/'execution.json'
    assert not ledger_path.exists(), 'Existing task ledger must never reset'
    ledger={'purpose':'STUDY_ONLY','producer':os.environ['ISSUE48_PRODUCER'],'cap':40000,'administrative_cpu_charge':60.,'settled_worker_cpu':0.,'fresh_completed':[],'fresh_failed':[],'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'retention_consumer':'Issue48 independent Reviewer and Planner','review_delete_date':'2026-11-14'}
    def publish(status,active=None):
        ledger.update(status=status,active=active or {},parent_cpu=time.process_time()-START)
        ledger['cumulative_cpu']=ledger['administrative_cpu_charge']+ledger['settled_worker_cpu']+ledger['parent_cpu']+sum(r['cpu'] for r in ledger['active'].values())
        write(ledger_path,ledger)
    def single(name,python,script):
        publish(name)
        with (STORE/(name+'.log')).open('w') as log:
            p=subprocess.Popen([python,str(SCRIPT/script),str(STORE)],stdout=log,stderr=log,start_new_session=True)
            while True:
                pid,status,usage=os.wait4(p.pid,os.WNOHANG)
                if pid: break
                publish(name,{str(p.pid):{'stage':name,'cpu':livecpu(p.pid)}})
                if ledger['cumulative_cpu']>=39800: os.killpg(p.pid,signal.SIGTERM)
                time.sleep(1)
            p.returncode=os.waitstatus_to_exitcode(status); ledger['settled_worker_cpu']+=usage.ru_utime+usage.ru_stime
        if p.returncode:
            publish('RESOURCE_BLOCKED' if ledger['cumulative_cpu']>=39800 else 'INCOMPLETE_'+name)
            raise ValueError(name+' failed')
    publish('STARTING')
    single('DERIVING',sys.executable,'derive.py')
    single('VERIFYING',TRAIN_PY,'verify_training.py')
    single('TRAINING',TRAIN_PY,'train.py')
    pending=[(s,a) for s in range(481001,481129) for a in ('S','A','Expert')]; active={}; stopped=False
    while pending or active:
        mem=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
        slots=max(1,min(8,max(0,mem-4*1024**3)//(8*1024**3)))
        while pending and len(active)<slots and not stopped:
            seed,arm=pending.pop(0); log=(STORE/f'{seed}-{arm}.log').open('w')
            p=subprocess.Popen([sys.executable,str(SCRIPT/'evaluate.py'),str(STORE),str(seed),arm],stdout=log,stderr=log,start_new_session=True)
            active[p.pid]=(seed,arm,p,log)
        publish('EVALUATING',{str(pid):{'seed':r[0],'arm':r[1],'cpu':livecpu(pid)} for pid,r in active.items()})
        if ledger['cumulative_cpu']>=39800: stopped=True; pending=[]
        if stopped:
            for pid in active:
                try: os.killpg(pid,signal.SIGTERM)
                except ProcessLookupError: pass
        for pid in list(active):
            done,status,usage=os.wait4(pid,os.WNOHANG)
            if not done: continue
            seed,arm,p,log=active.pop(pid); p.returncode=os.waitstatus_to_exitcode(status); log.close()
            ledger['settled_worker_cpu']+=usage.ru_utime+usage.ru_stime
            if p.returncode: ledger['fresh_failed'].append({'seed':seed,'arm':arm,'exit':p.returncode}); stopped=True; pending=[]
            else: ledger['fresh_completed'].append([seed,arm])
        ledger['fresh_pending']=len(pending)
        if active: time.sleep(1)
    if stopped:
        publish('RESOURCE_BLOCKED' if ledger['cumulative_cpu']>=39800 else 'INCOMPLETE_FRESH'); return
    single('AUDITING',sys.executable,'audit.py'); single('REPORTING',sys.executable,'report.py')
    publish('COMPLETE_PENDING_INDEPENDENT_REVIEW')

if __name__=='__main__':
    try: main()
    except BaseException as exc:
        write(STORE/'failure.json',{'error':repr(exc),'parent_cpu':time.process_time()-START}); raise
