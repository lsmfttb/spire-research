"""Issue43 Linux job supervisor; durable budget leases survive supervisor death.

Known native jobs execute in one process (native threads count as process CPU).
Every job has a separate process group, inherited CPU/address-space limits and
an external GNU timeout. Unknown crash exposure is charged at the full lease,
never at the last sampled tick. Live supervision also counts child processes.
"""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import resource
import subprocess
import sys
import time

HERE=Path(__file__).parent


def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def overlay(path=None):
    config=json.loads((HERE/'config.json').read_text())
    value=json.loads((Path(path) if path else HERE/'planner_budget_amendment.json').read_text())
    expected={'schema':'issue43-planner-budget-amendment-v1',
        'authority':'https://github.com/lsmfttb/spire-research/issues/43#issuecomment-6108594676',
        'original_config_git_blob':'12a1119933beecb72e52922444b387c9d9eaa0c2',
        'original_config_sha256':'d069965e85880dd6af83e70de7c8b4c44111cb1b42ee887cf09f957384f99b7b',
        'native_commit':'7fef4e19c7ee13f049ee0fb45c9967e7e349fcfb',
        'stage1':{'cpu_seconds':180000,'wall_seconds':27000}}
    raw=(HERE/'config.json').read_bytes().replace(b'\r\n',b'\n')
    blob=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    if value!=expected or sha(config)!=expected['original_config_sha256'] or blob!=expected['original_config_git_blob']:
        raise ValueError('budget overlay or frozen scientific config changed')
    return value


def caps(stage):
    amended=overlay()
    if stage=='stage1': return amended['stage1']
    if stage=='stage2': return {'cpu_seconds':36000,'wall_seconds':12000}
    raise ValueError('unknown formal stage')


def available():
    return int(next(s.split()[1] for s in Path('/proc/meminfo').read_text().splitlines()
                    if s.startswith('MemAvailable:')))*1024


def processes(group):
    rows=[]
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields=path.read_text().split(') ',1)[1].split()
            if int(fields[2])!=group: continue
            rows.append({'pid':int(path.parent.name),'start':int(fields[19]),
            'cpu':(int(fields[11])+int(fields[12])+int(fields[13])+int(fields[14]))/os.sysconf('SC_CLK_TCK'),
                'own_cpu':(int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK'),
                'rss':max(0,int(fields[21]))*os.sysconf('SC_PAGE_SIZE')})
        except (FileNotFoundError,ProcessLookupError): pass
    return rows


class Journal:
    """Append-only hash chain. Corruption/truncation/identity drift fails closed."""
    def __init__(self,directory,identity,limit):
        self.directory=Path(directory); self.directory.mkdir(parents=True,exist_ok=True)
        self.lock=(self.directory/'supervisor.lock').open('a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.path=self.directory/'resource.jsonl'; self.previous='0'*64
        self.state={'identity':identity,'limit':limit,'cpu':0.,'wall':0.,'leases':{},'attempts':0}
        try:
            if self.path.exists():
                for line in self.path.read_text().splitlines():
                    row=json.loads(line); checksum=row.pop('sha256')
                    if row['previous']!=self.previous or sha(row)!=checksum:
                        raise ValueError('resource journal checksum/chain corruption')
                    self.previous=checksum; self.state=row['state']
                if self.state['identity']!=identity or self.state['limit']!=limit:
                    raise ValueError('resource lineage identity/cap changed')
            else: self.record('created')
        except BaseException:
            self.close(); raise

    def record(self,boundary):
        row={'boundary':boundary,'previous':self.previous,'state':self.state}
        checksum=sha(row); row['sha256']=checksum
        with self.path.open('a') as stream:
            stream.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n'); stream.flush(); os.fsync(stream.fileno())
        self.previous=checksum

    def close(self):
        fcntl.flock(self.lock,fcntl.LOCK_UN); self.lock.close()


class Supervisor:
    def __init__(self,directory,identity,limit,poll=.1,job_cpu=6000.,job_wall=3600.,rss=8*1024**3,free=4*1024**3,startup_cpu=0.,startup_wall=0.):
        self.journal=Journal(directory,identity,limit); self.state=self.journal.state
        self.poll=poll; self.job_cpu=job_cpu; self.job_wall=job_wall; self.rss=rss; self.free=free
        self.live={}; self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.parent_reserve=max(1,min(10,math.floor(self.state['limit']['cpu_seconds']*.1)))
        # A kernel CPU soft limit closes the sampling gap for the supervisor
        # itself. SIGXCPU's default termination applies even inside a C call.
        # Hard limit stays unchanged so each sampled window can raise the soft
        # limit. A crash debits the entire outstanding parent CPU window.
        self.original_cpu_limit=resource.getrlimit(resource.RLIMIT_CPU)
        self.original_alarm=signal.getitimer(signal.ITIMER_REAL)
        self.original_cpu_signal=signal.getsignal(signal.SIGXCPU)
        self.original_alarm_signal=signal.getsignal(signal.SIGALRM)
        if self.state.get('attempt_open'):
            self.state['cpu']+=self.state.get('parent_cpu_lease',self.parent_reserve)
            previous=self.state.get('last_sample',{})
            if previous.get('boot')==self.boot:
                exposure=max(0,time.monotonic()-previous['monotonic'])
            else:
                exposure=time.time()-previous.get('utc',time.time())
                if exposure<0: exposure=self.state['limit']['wall_seconds']
            # Paused/crashed elapsed time counts conservatively against the
            # same wall lineage, including intervals without active workers.
            self.state['wall']+=exposure
        # Recover supervisor death conservatively. Leases cover the entire
        # unobserved job lifetime, including failed starts and native blockage.
        if self.state['leases']:
            wall_exposure=0.
            for lease in self.state['leases'].values():
                # Covers the fork-to-PID-publication crash window and orphaned
                # descendants. Exact random token is an argv field, not a
                # substring inferred from a recycled numeric process group.
                if lease.get('boot')==self.boot:
                    for path in Path('/proc').glob('[0-9]*/cmdline'):
                        try:
                            args=path.read_bytes().split(b'\0')
                            if lease.get('token','').encode() in args and str(HERE/'worker.py').encode() in args:
                                self.kill(os.getpgid(int(path.parent.name)))
                        except (FileNotFoundError,ProcessLookupError): pass
                if lease.get('boot')==self.boot and lease.get('pid'):
                    group=processes(lease['pid'])
                    if any(r['pid']==lease['pid'] and r['start']==lease['pid_start'] for r in group):
                        self.kill(lease['pid'])
                    elif group:
                        # PID leader vanished: only kill known surviving PIDs,
                        # verifying start ticks, never a possibly reused group.
                        for row in group:
                            if str(row['pid']) in lease.get('members',{}) and lease['members'][str(row['pid'])]==row['start']:
                                try: os.kill(row['pid'],signal.SIGKILL)
                                except ProcessLookupError: pass
                self.state['cpu']+=max(0,lease['cpu_cap']-lease['charged_cpu'])
                wall_exposure=max(wall_exposure,max(0,lease['wall_cap']-lease['elapsed']))
            # Crash clock already includes elapsed exposure. For a reboot or
            # changed clock, the outstanding external timeout is a lower bound.
            self.state['wall']+=max(0,wall_exposure-exposure) if self.state.get('attempt_open') else wall_exposure
            self.state['leases']={}
            self.journal.record('abandoned_leases_charged')
        self.last=time.monotonic(); self.parent_cpu=time.process_time()
        self.state['cpu']+=startup_cpu; self.state['wall']+=startup_wall
        self.state['attempts']+=1; self.journal.record('attempt_started')
        try: self.check()
        except BaseException:
            self.journal.close(); raise
        self.state['attempt_open']=True
        try:
            self.guard_parent(); self.journal.record('parent_guard_armed')
        except BaseException:
            self.journal.close(); raise

    def guard_parent(self):
        committed=sum(max(0,l['cpu_cap']-l['charged_cpu']) for l in self.state['leases'].values())
        window=min(self.parent_reserve,math.floor(self.state['limit']['cpu_seconds']-self.state['cpu']-committed))
        if window<1: raise RuntimeError('RESOURCE_BLOCKED: supervisor CPU reservation')
        self.state['parent_cpu_lease']=window
        self.state['last_sample']={'boot':self.boot,'monotonic':time.monotonic(),'utc':time.time()}
        # Recovery must see the full renewed window before the kernel allows
        # its use, especially after successful workers release reservations.
        self.journal.record('parent_lease_reserved_before_arm')
        signal.signal(signal.SIGXCPU,signal.SIG_DFL); signal.signal(signal.SIGALRM,signal.SIG_DFL)
        resource.setrlimit(resource.RLIMIT_CPU,(math.floor(time.process_time())+window,self.original_cpu_limit[1]))
        signal.setitimer(signal.ITIMER_REAL,max(.001,self.state['limit']['wall_seconds']-self.state['wall']))

    @staticmethod
    def kill(group):
        try: os.killpg(group,signal.SIGKILL)
        except ProcessLookupError: pass

    def check(self):
        if self.state['cpu']>=self.state['limit']['cpu_seconds'] or self.state['wall']>=self.state['limit']['wall_seconds']:
            raise RuntimeError('RESOURCE_BLOCKED: cumulative stage cap')
        if available()<self.free:
            raise RuntimeError('RESOURCE_BLOCKED: available memory')

    def tick(self):
        now=time.monotonic(); parent=time.process_time()
        self.state['wall']+=now-self.last; self.state['cpu']+=parent-self.parent_cpu
        self.last=now; self.parent_cpu=parent
        for token,process in self.live.items():
            lease=self.state['leases'][token]; rows=processes(process.pid)
            # timeout leader's waited CPU includes exited descendants; live
            # descendants' own CPU are added once. The worker owns no processes
            # in production; nested-child test cases are still supervised.
            measured=sum(r['cpu'] for r in rows)
            measured=max(measured,lease['charged_cpu'])
            self.state['cpu']+=measured-lease['charged_cpu']; lease['charged_cpu']=measured
            lease['elapsed']=now-lease['began']; lease['members']={str(r['pid']):r['start'] for r in rows}
            if sum(r['rss'] for r in rows)>self.rss or measured>=lease['cpu_cap'] or lease['elapsed']>=lease['wall_cap']:
                raise RuntimeError('RESOURCE_BLOCKED: job process-tree cap')
        self.journal.record('sampled'); self.check()
        self.guard_parent()

    def launch(self,request):
        self.tick()
        committed=sum(max(0,l['cpu_cap']-l['charged_cpu']) for l in self.state['leases'].values())
        reserve=self.parent_reserve
        remaining=self.state['limit']['cpu_seconds']-self.state['cpu']-committed-reserve
        cpu_cap=math.floor(min(self.job_cpu,remaining))
        wall_cap=min(self.job_wall,self.state['limit']['wall_seconds']-self.state['wall'])
        if cpu_cap<1 or wall_cap<=self.poll: return None
        token=sha([str(request),time.time_ns()]); lease={'cpu_cap':cpu_cap,'wall_cap':wall_cap,'charged_cpu':0.,
            'elapsed':0.,'boot':self.boot,'began':time.monotonic(),'request':str(request),'token':token}
        self.state['leases'][token]=lease; self.journal.record('lease_reserved_before_start')
        # GNU timeout is outside the Python worker: it kills a stuck C++ call
        # even if Python signal handlers cannot run. Worker RLIMIT_CPU is hard.
        command=['timeout','--signal=KILL',str(wall_cap),sys.executable,str(HERE/'worker.py'),
                 str(request),str(cpu_cap),str(self.rss),token]
        log=(self.journal.directory/(token+'.log')).open('wb')
        try:
            process=subprocess.Popen(command,start_new_session=True,stdout=log,stderr=subprocess.STDOUT)
        except BaseException:
            self.state['cpu']+=cpu_cap; self.state['wall']+=wall_cap
            self.state['leases'].pop(token); self.journal.record('failed_start_full_lease_charged'); log.close(); raise
        lease.update(pid=process.pid,pid_start=next(r['start'] for r in processes(process.pid) if r['pid']==process.pid))
        self.live[token]=process; log.close(); self.journal.record('worker_started')
        return token

    def settle(self,token):
        process=self.live[token]
        pid,status,usage=os.wait4(process.pid,os.WNOHANG)
        if not pid: return None
        process.returncode=os.waitstatus_to_exitcode(status)
        lease=self.state['leases'][token]; measured=usage.ru_utime+usage.ru_stime
        # Failed workers may orphan unwaited descendants. Charge their whole
        # lease so sampling loss cannot reset aggregate CPU after a failure.
        charge=measured if process.returncode==0 else max(measured,lease['cpu_cap'])
        self.state['cpu']+=max(0,charge-lease['charged_cpu'])
        self.kill(process.pid)
        del self.live[token]; del self.state['leases'][token]
        self.journal.record('completed' if process.returncode==0 else 'worker_failed_full_lease_charged')
        return process.returncode

    def run(self,requests,workers=8):
        pending=list(requests); active={}; finished=[]
        try:
            while pending or active:
                self.tick()
                while pending and len(active)<workers:
                    token=self.launch(pending[0])
                    if token is None: break
                    active[token]=pending.pop(0)
                if pending and not active: raise RuntimeError('RESOURCE_BLOCKED: no remaining CPU lease')
                for token in list(active):
                    code=self.settle(token)
                    if code is None: continue
                    request=active.pop(token)
                    if code: raise RuntimeError('INCOMPLETE: worker exit '+str(code))
                    finished.append(request)
                if active: time.sleep(self.poll)
            self.tick(); return finished
        except BaseException:
            self.abort(); raise

    def abort(self):
        wall_exposure=0.
        for token,process in list(self.live.items()):
            lease=self.state['leases'][token]; self.kill(process.pid)
            process.wait()
            self.state['cpu']+=max(0,lease['cpu_cap']-lease['charged_cpu'])
            wall_exposure=max(wall_exposure,max(0,lease['wall_cap']-lease['elapsed']))
        # We just observed and killed the group; actual elapsed wall is charged
        # by tick. Remaining wall exposure is only needed after supervisor death.
        self.live={}; self.state['leases']={}; self.journal.record('aborted_groups_terminated')

    def close(self):
        try:
            if self.live: self.abort()
            self.tick()
        finally:
            resource.setrlimit(resource.RLIMIT_CPU,self.original_cpu_limit)
            signal.setitimer(signal.ITIMER_REAL,*self.original_alarm)
            signal.signal(signal.SIGXCPU,self.original_cpu_signal); signal.signal(signal.SIGALRM,self.original_alarm_signal)
            self.state['attempt_open']=False; self.state['parent_cpu_lease']=0
            self.journal.record('attempt_closed'); self.journal.close()
