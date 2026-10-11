"""Disposable Issue43 complete stage coordinator. Formal launch is closed.

Same command resumes the same outside-worktree directory and budget lineage.
Only a later explicit Planner launch comment admits a real stage.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import urllib.request
from budget import Supervisor,overlay,caps,sha
from formal import CONFIG,Completed,atomic,cohort,validate_splits,keys,targets,evaluation_plan,digest

HERE=Path(__file__).resolve().parent
FROZEN={'action_space.py':'817db4e621b0a12e15f29c24e1dc3378da957bfc',
    'controlled_run.py':'3ad3b57bb6fd433e5cb9ef7d41245d9ba75b4fd8',
    'lightspeed.py':'8a95863fe26506b55ee43bca4196c1af73abc888',
    'non_combat_policy.py':'8079fe274a49a39201e8270a2ddc2a7d2b618568',
    'oracle_search.py':'32926eb70e7c9648046851c5616bea4ff1e8e793'}


def blob(path):
    raw=Path(path).read_bytes().replace(b'\r\n',b'\n')
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def runtime():
    import slaythespire
    import sts_combat_rl.sim.controlled_run as backend
    parent=Path(backend.__file__).parent
    frozen={n:blob(parent/n) for n in FROZEN}
    if frozen!=FROZEN or blob(HERE/'adapter.py')!='3d0c4104043456a3ade4baf739c18704c1b80b7c':
        raise ValueError('frozen source/adapter identity changed')
    extension=hashlib.sha256(Path(slaythespire.__file__).read_bytes()).hexdigest()
    if extension!='ced9941eb1cd042630b27ed3b9b0be700cd8cbcc143156d52bdabf7bf07234cd':
        raise ValueError('unreviewed loaded native extension')
    return {'python':sys.version,'platform':platform.platform(),'frozen_source_blobs':frozen,
        'adapter_git_blob':blob(HERE/'adapter.py'),'extension_sha256':extension,
        'study_source_sha256':{p.name:hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in sorted(HERE.glob('*.py'))}}


def identity(producer_commit,synthetic):
    if len(producer_commit)!=40 or any(c not in '0123456789abcdef' for c in producer_commit):
        raise ValueError('exact producer SHA required')
    return {'schema':'issue43-formal-lineage-v1','producer_commit':producer_commit,
        'synthetic':synthetic,'purpose':'SYNTHETIC_READINESS_ONLY' if synthetic else 'STUDY_ONLY',
        'native_commit':CONFIG['native_commit'],'frozen_stsrl_commit':CONFIG['frozen_stsrl_commit'],
        'config_sha256':sha(CONFIG),'original_config_git_blob':overlay()['original_config_git_blob'],
        'budget_overlay':overlay(),'budget_overlay_sha256':sha(overlay()),**runtime()}


def validate_runtime(expected):
    for name,value in runtime().items():
        if expected[name]!=value: raise ValueError('runtime/code identity drift: '+name)


def authorize(stage,ident,url):
    """Require a real Issue43 comment containing a stage-specific exact-head grant.

    The current readiness amendment contains no launch grant. The future Planner
    comment must include STAGE1_LAUNCH_AUTHORIZED or STAGE2_LAUNCH_AUTHORIZED and
    this exact producer commit. GitHub remains the authority surface.
    """
    prefix='https://github.com/lsmfttb/spire-research/issues/43#issuecomment-'
    if not url or not url.startswith(prefix) or not url[len(prefix):].isdigit():
        raise ValueError('FORMAL_LAUNCH_NOT_AUTHORIZED: explicit subsequent Planner comment required')
    api='https://api.github.com/repos/lsmfttb/spire-research/issues/comments/'+url[len(prefix):]
    with urllib.request.urlopen(urllib.request.Request(api,headers={'User-Agent':'issue43-study'}),timeout=30) as response:
        comment=json.load(response)
    if comment['html_url']!=url or comment['user']['login']!='lsmfttb' or stage.upper()+'_LAUNCH_AUTHORIZED' not in comment['body'] or ident['producer_commit'] not in comment['body']:
        raise ValueError('FORMAL_LAUNCH_NOT_AUTHORIZED: no exact-head Planner grant')
    return {'url':url,'comment_body_sha256':sha(comment['body']),'producer_commit':ident['producer_commit'],'stage':stage}


def immutable(path,value):
    if path.exists():
        if json.loads(path.read_text())!=value: raise ValueError('immutable artifact changed: '+str(path))
    else: atomic(path,value)
    return path


class Pipeline:
    def __init__(self,directory,ident,stage,authorization=None):
        self.directory=Path(directory).resolve(); self.ident=ident; self.stage=stage
        if self.directory.is_relative_to(HERE.parents[1]): raise ValueError('artifacts must be outside worktree')
        self.directory.mkdir(parents=True,exist_ok=True)
        immutable(self.directory/'identity.json',ident)
        if not ident['synthetic']: immutable(self.directory/(stage+'-authorization.json'),authorization)
        self.base=self.directory/stage; self.base.mkdir(exist_ok=True)
        # Synthetic implementation checks have independently bounded readiness
        # caps. They do not consume/reset/stand in for an authentic formal stage.
        limit=({'cpu_seconds':350,'wall_seconds':450} if stage=='stage1' else {'cpu_seconds':100,'wall_seconds':120}) if ident['synthetic'] else caps(stage)
        self.supervisor=Supervisor(self.base/'resources',ident,limit,
            job_cpu=120 if ident['synthetic'] else 6000,job_wall=180 if ident['synthetic'] else 3600)
        self.jobs=Completed(self.base/'jobs',ident)
        self.requests=self.base/'requests'; self.requests.mkdir(exist_ok=True)
    def request(self,kind,payload,key):
        value={'identity':self.ident,'kind':kind,'payload':payload,'key':key,'store':str(self.base/'jobs')}
        return immutable(self.requests/(digest(key)+'.json'),value)
    def run(self,requests):
        pending=[p for p in requests if self.jobs.get(json.loads(p.read_text())['key']) is None]
        if pending:
            kind=json.loads(pending[0].read_text())['kind']
            if any(json.loads(p.read_text())['kind']!=kind for p in pending): raise ValueError('mixed job lease classes')
            self.supervisor.job_cpu=({'train':120,'heldout':60,'fresh_report':60}.get(kind,4) if self.ident['synthetic']
                else {'source':600,'fresh':3000,'train':6000,'heldout':6000,'fresh_report':6000}.get(kind,6000))
            self.supervisor.run(pending,CONFIG['workers'])
        return [self.jobs.get(json.loads(p.read_text())['key']) for p in requests]
    def boundary(self,name,**value):
        self.supervisor.tick(); self.jobs.event({'boundary':name,**value})
    def freeze(self,name,value):
        self.supervisor.tick(); return immutable(self.base/name,value)
    def complete(self,value):
        path=self.base/'complete.json'
        if path.exists():
            previous=json.loads(path.read_text())
            if {k:v for k,v in previous.items() if not k.startswith('resource_')}!={k:v for k,v in value.items() if not k.startswith('resource_')}:
                raise ValueError('complete stage identity changed')
            return path
        return self.freeze('complete.json',value)
    def stage1(self):
        roots={}; scans={}
        for split in ('train','validation','heldout'):
            collected=[]; observed=[]; low,high=CONFIG['source_ranges'][split]
            for first in range(low,high+1,CONFIG['workers']):
                requests=[self.request('source',{'owner':owner,'split':split},['source',split,owner])
                    for owner in range(first,min(high+1,first+CONFIG['workers']))]
                rows=self.run(requests)
                for row in rows:
                    collected.extend(row['roots']); observed.append(row['source_cost'])
                try: selected=cohort(collected,split)
                except ValueError as error:
                    if str(error)!='SOURCE_COVERAGE_INSUFFICIENT': raise
                    continue
                break
            else: raise ValueError('SOURCE_COVERAGE_INSUFFICIENT')
            roots[split]=selected; scans[split]=observed
            self.freeze('cohort-'+split+'.json',{'identity':self.ident,'roots':selected,'scanned_sources':observed})
        validate_splits(roots); self.boundary('cohorts_frozen',sha256=sha(roots))
        for split in roots:
            owners=sorted({r['owner'] for r in roots[split]}); requests=[]
            for owner in owners:
                selected=[r for r in roots[split] if r['owner']==owner]
                directory=self.base/'branches'/split/str(owner)
                requests.append(self.request('labels',{'roots':selected,'branch_store':str(directory)},['labels',split,owner,sha(selected)]))
            self.run(requests)
        tables={}
        for split in ('train','validation'):
            tables[split]=self.tables(roots[split])
            self.freeze(split+'-targets.json',tables[split])
        training=self.run([self.request('train',{'train':str(self.base/'train-targets.json'),
            'validation':str(self.base/'validation-targets.json')},['train',sha(tables)])])[0]
        checkpoints={a:v['checkpoint'] for a,v in training['models'].items()}
        model_path=self.freeze('models.json',{'identity':self.ident,'checkpoints':checkpoints,'training_job_key':training['key']})
        self.boundary('models_frozen_before_heldout_open',checkpoint_sha256={a:c['checkpoint_sha256'] for a,c in checkpoints.items()})
        held=self.tables(roots['heldout']); table_path=self.freeze('heldout-targets.json',held)
        report=self.run([self.request('heldout',{'models':str(model_path),'tables':str(table_path),'roots':roots['heldout']},
            ['heldout',sha(held),sha(checkpoints)])])[0]
        self.freeze('heldout-report.json',{'identity':self.ident,**report['report'],
            'status':'SYNTHETIC_READINESS_ONLY' if self.ident['synthetic'] else report['report']['status']})
        self.boundary('stage1_complete')
        return self.complete({'identity':self.ident,'stage':'stage1','valid':True,
            'roots_sha256':sha(roots),'checkpoint_sha256':{a:c['checkpoint_sha256'] for a,c in checkpoints.items()},
            'heldout_report_sha256':sha(report['report']),'heldout_job_key':report['key'],
            'scanned_sources':scans,'resource_cpu':self.supervisor.state['cpu'],'resource_wall':self.supervisor.state['wall']})
    def tables(self,roots):
        out=[]
        for root in roots:
            store=Completed(self.directory/'stage1'/'branches'/root['split']/str(root['owner']),self.ident)
            branches=[store.get(k) for k in keys(root)]
            if any(b is None for b in branches): raise ValueError('INCOMPLETE_LABELS')
            out.append(targets(root,branches)); self.supervisor.tick()
        return out
    def stage2(self):
        stage1=self.directory/'stage1'
        complete=json.loads((stage1/'complete.json').read_text())
        if complete['identity']!=self.ident or not complete['valid']: raise ValueError('Stage1 gate invalid')
        roots={s:json.loads((stage1/('cohort-'+s+'.json')).read_text())['roots'] for s in CONFIG['quotas']}
        validate_splits(roots)
        if sha(roots)!=complete['roots_sha256']: raise ValueError('Stage1 cohort hash changed')
        # Recheck every original label checksum, not just the summary's boolean.
        for split in roots:
            immutable(stage1/(split+'-targets.json'),self.tables(roots[split]))
        models=stage1/'models.json'; frozen=json.loads(models.read_text())
        if frozen['identity']!=self.ident or {a:c['checkpoint_sha256'] for a,c in frozen['checkpoints'].items()}!=complete['checkpoint_sha256']:
            raise ValueError('Stage1 model identity changed')
        jobs=Completed(stage1/'jobs',self.ident)
        training=jobs.get(frozen['training_job_key']); held=jobs.get(complete['heldout_job_key'])
        if training is None or held is None or {a:m['checkpoint'] for a,m in training['models'].items()}!=frozen['checkpoints'] or sha(held['report'])!=complete['heldout_report_sha256']:
            raise ValueError('Stage1 completed model/heldout result changed')
        plan=evaluation_plan(frozen['checkpoints']); self.freeze('evaluation-plan.json',{'identity':self.ident,'plan':plan})
        requests=[self.request('fresh',{'models':str(models),'plan':p,'eval_store':str(self.base/'evaluation'/p['key'])},
            ['fresh',p['key']]) for p in plan]
        self.run(requests); runs=[]
        for p in plan:
            store=Completed(self.base/'evaluation'/p['key'],self.ident); row=store.get([p['key'],p['seed'],p['arm']])
            if row is None: raise ValueError('INCOMPLETE_FRESH')
            runs.append(row)
        runs_path=self.freeze('runs.json',runs)
        report=self.run([self.request('fresh_report',{'models':str(models),'runs':str(runs_path),'train_roots':roots['train']},
            ['fresh_report',sha(runs),sha(frozen)])])[0]
        self.freeze('fresh-report.json',{'identity':self.ident,**report['report']})
        self.boundary('stage2_complete')
        return self.complete({'identity':self.ident,'stage':'stage2','valid':True,'report_sha256':sha(report['report']),
            'resource_cpu':self.supervisor.state['cpu'],'resource_wall':self.supervisor.state['wall']})
    def close(self): self.supervisor.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['stage1','stage2','synthetic'])
    parser.add_argument('--directory',required=True); parser.add_argument('--producer-commit',required=True)
    parser.add_argument('--planner-comment'); args=parser.parse_args()
    synthetic=args.stage=='synthetic'; ident=identity(args.producer_commit,synthetic)
    for stage in ('stage1','stage2') if synthetic else (args.stage,):
        auth=None if synthetic else authorize(stage,ident,args.planner_comment)
        pipeline=Pipeline(args.directory,ident,stage,auth)
        try: getattr(pipeline,stage)()
        except BaseException as error:
            pipeline.jobs.event({'boundary':'stage_stopped','stage':stage,'error':repr(error)}); raise
        finally: pipeline.close()


if __name__=='__main__': main()
