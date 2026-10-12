"""Single-task bounded source/label sharding, settled plus live CPU accounting."""
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
from common import CONFIG,sha,write,owner_plan

STORE=Path(sys.argv[1]); STORE.mkdir(parents=True,exist_ok=True)
SCRIPT=Path(__file__).parent
OLD=Path('/home/lsmft/.cache/issue45-potion-paired/reviewed-native-18bcd565')
EXT=Path('/home/lsmft/.cache/issue45-review-build-18bcd565/slaythespire.cpython-314-x86_64-linux-gnu.so')
START=time.process_time(); WALL=time.monotonic()

def filehash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024**2),b''): h.update(block)
    return h.hexdigest()

def livecpu(pid):
    try:
        fields=Path(f'/proc/{pid}/stat').read_text().split(') ')[1].split()
        return (int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK')
    except FileNotFoundError: return 0.

def main():
    ledger_path=STORE/'execution.json'
    resume='--resume' in sys.argv[2:]
    if ledger_path.exists() and not resume: raise ValueError('Use --resume; task CPU never resets')
    if resume:
        ledger=json.loads(ledger_path.read_text())
        if ledger['status']!='COLLECTING' or ledger['cap']!=CONFIG['cpu_cap']: raise ValueError('unexpected resume boundary')
        # Only resume after the old process tree has disappeared. A new WSL
        # boot can reuse PIDs, so inspect command lines as well as PID presence.
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit() or int(proc.name)==os.getpid(): continue
            try: command=(proc/'cmdline').read_bytes()
            except (FileNotFoundError,PermissionError,ProcessLookupError): continue
            if b'issue-46-source-capacity/collect.py' in command or b'issue-46-source-capacity/finish.py' in command or b'issue-46-source-capacity/launch_collect.py' in command: raise ValueError('old task process still active')
        stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
        receipt={'previous_ledger':ledger,'resume_producer':os.environ['ISSUE46_LAUNCHER_PRODUCER'],'interrupted':[],'reason':'WSL restarted; previous worker and supervisors absent; observed new /proc uptime'}
        archive=STORE/'interrupted-attempts'/stamp; archive.mkdir(parents=True)
        completed=set(ledger['completed'])
        for owner in completed:
            directory=STORE/str(owner); record=json.loads((directory/'complete.json').read_text())
            if record['owner']!=owner or record['table_sha256']!=filehash(directory/'table.json'): raise ValueError('completed table checksum mismatch')
        for pid,data in ledger['active'].items():
            owner=data['owner']; directory=STORE/str(owner)
            progress=json.loads((directory/'progress.json').read_text()) if (directory/'progress.json').exists() else {}
            # Settled wait4 was lost on reboot. Charge this ONE interrupted
            # process its enforced10000 CPU hard limit plus1-second signal
            # granularity. This is an upper bound, never a measured CPU claim.
            upper=10001.; observed=max(data['cpu'],progress.get('cpu',0.))
            receipt['interrupted'].append({'owner':owner,'last_live_cpu':data['cpu'],'observed_cpu_lower':observed,'hard_limit_cpu_upper':upper,'unfinished_boundary':progress,'archived_path':str(archive/str(owner))})
            ledger['settled_worker_cpu']+=upper
        for plan_row in owner_plan():
            owner=plan_row['owner']; directory=STORE/str(owner)
            if owner not in completed and directory.exists():
                if directory.resolve().parent!=STORE.resolve(): raise ValueError('archive outside task store')
                directory.rename(archive/str(owner))
                log=STORE/f'{owner}.log'
                if log.exists(): log.rename(archive/f'{owner}.log')
        receipt_path=archive/'interruption.json'; write(receipt_path,receipt)
        ledger.setdefault('interruption_receipts',[]).append(str(receipt_path))
        ledger['cpu_accounting']='settled wait4 and live CPU, plus explicit hard-limit upper debit for interrupted worker whose wait4 was lost; not all measured'
        ledger['administrative_cpu_charge']+=10.
        ledger['active']={}
    else:
        ledger={'status':'VERIFYING_OLD45','cap':CONFIG['cpu_cap'],'administrative_cpu_charge':100.,'settled_worker_cpu':0.,'parent_cpu':0.,'completed':[],'failed':[],'active':{},'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'purpose':'STUDY_ONLY','store':str(STORE),'retention_consumer':'Issue46 independent Reviewer and Planner','review_delete_date':'2026-11-12'}
        write(ledger_path,ledger)
    previous_parent=ledger['parent_cpu']; previous_wall=ledger.get('wall',0.)
    if filehash(EXT)!=CONFIG['native_extension_sha256']: raise ValueError('native extension identity changed')
    manifest=OLD/'manifest.json'
    if filehash(manifest)!='624b656dee2afe33cf0e0410dd4cdf1a198c456d3d050a3ac061cb1aec18c160': raise ValueError('sealed old45 manifest identity changed')
    entries=json.loads(manifest.read_text())
    for entry in entries:
        path=Path(entry['path'])
        if path.stat().st_size!=entry['bytes'] or filehash(path)!=entry['sha256']: raise ValueError('sealed old45 artifact changed: '+str(path))
    source={'producer':os.environ['ISSUE46_PRODUCER'],'native_commit':CONFIG['native_commit'],'native_extension_sha256':filehash(EXT),'stsrl_commit':CONFIG['stsrl_commit'],'old45_store':str(OLD),'old45_manifest_sha256':filehash(manifest),'old45_verified_files':len(entries),'config_sha256':sha(CONFIG),'code_sha256':{p.name:filehash(p) for p in SCRIPT.glob('*.py')}}
    if resume:
        original=json.loads((STORE/'source.json').read_text())
        if original['producer']!=source['producer'] or original['config_sha256']!=source['config_sha256']: raise ValueError('source identity changed')
        for name in ('collect.py','common.py','adapter.py','student.py'):
            canonical=hashlib.sha256((SCRIPT/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
            if canonical!=original['code_sha256'][name]: raise ValueError('collection worker changed: '+name)
        write(STORE/'resume-source.json',{'launcher':source,'original_source_sha256':filehash(STORE/'source.json'),'worker_same_bytes_after_LF_normalization':True})
    else:
        write(STORE/'source.json',source); write(STORE/'owner-plan.json',owner_plan())
    plan={p['owner']:p for p in owner_plan()}
    first=[437001,437002,440001,440002,440097,440098,440289,440290]
    pending=[owner for owner in first+[p['owner'] for p in owner_plan() if p['owner'] not in first] if owner not in ledger['completed']]
    active={}; status='COLLECTING'; first_report=(STORE/'first-fixed-key-costs.json').exists()
    while pending or active:
        mem=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
        slots=max(1,min(8,max(0,mem-4*1024**3)//(8*1024**3)))
        # Eight-job initial fixed-key wave supplies real source/label costs before bulk admission.
        while pending and len(active)<slots and (first_report or pending[0] in first):
            owner=pending.pop(0); log=(STORE/f'{owner}.log').open('w')
            child=subprocess.Popen([sys.executable,str(SCRIPT/'collect.py'),str(STORE),str(owner)],stdout=log,stderr=log,start_new_session=True)
            active[child.pid]=(owner,child,log)
        total=ledger['administrative_cpu_charge']+ledger['settled_worker_cpu']+previous_parent+(time.process_time()-START)+sum(livecpu(pid) for pid in active)
        if total>=CONFIG['cpu_cap']-200:
            status='RESOURCE_BLOCKED'
            pending=[]
            for pid in active: os.killpg(pid,signal.SIGTERM)
        for pid in list(active):
            waited,wait_status,usage=os.wait4(pid,os.WNOHANG)
            if not waited: continue
            owner,child,log=active.pop(pid); child.returncode=os.waitstatus_to_exitcode(wait_status); log.close()
            ledger['settled_worker_cpu']+=usage.ru_utime+usage.ru_stime
            if child.returncode!=0:
                ledger['failed'].append({'owner':owner,'exit':child.returncode,'cpu':usage.ru_utime+usage.ru_stime})
                if status!='RESOURCE_BLOCKED': status='INCOMPLETE'
                pending=[]
                for other in active: os.killpg(other,signal.SIGTERM)
            else: ledger['completed'].append(owner)
        if not first_report and not active and all(o in ledger['completed'] for o in first):
            first_report=True
            observed=[]
            for o in first:
                x=json.loads((STORE/str(o)/'complete.json').read_text()); table=json.loads((STORE/str(o)/'table.json').read_text())
                observed.append({**plan[o],'total_cpu':x['cpu'],'source_cpu':table['source_cpu'],'label_cpu':table['label_cpu'],'branches':table['branch_count'],'act':table['root']['act'],'floor':table['root']['floor']})
            write(STORE/'first-fixed-key-costs.json',{'keys':observed,'notice':'Measured first prescribed keys, not a separate diagnostic study; no sample thinning or favorable selection.'})
        ledger.update(status=status,active={str(pid):{'owner':data[0],'cpu':livecpu(pid)} for pid,data in active.items()},pending_count=len(pending),parent_cpu=previous_parent+time.process_time()-START,wall=previous_wall+time.monotonic()-WALL)
        ledger['cumulative_cpu']=ledger['administrative_cpu_charge']+ledger['settled_worker_cpu']+ledger['parent_cpu']+sum(x['cpu'] for x in ledger['active'].values())
        write(ledger_path,ledger)
        if status!='COLLECTING' and not active: break
        if active: time.sleep(1)
    if status=='COLLECTING': ledger['status']='SOURCE_LABELS_COMPLETE'
    ledger['active']={}; ledger['parent_cpu']=previous_parent+time.process_time()-START
    ledger['cumulative_cpu']=ledger['administrative_cpu_charge']+ledger['settled_worker_cpu']+ledger['parent_cpu']
    write(ledger_path,ledger)

if __name__=='__main__':
    try: main()
    except BaseException as exc:
        write(STORE/'launcher-failure.json',{'error':repr(exc),'cpu':time.process_time()-START}); raise
