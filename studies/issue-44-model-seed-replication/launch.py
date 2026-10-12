"""Exact-head admission and one append-only lineage for all formal work."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from replication import CONTRACT,HERE,dependencies,sha,read,manifest

def git_tree(commit):
    root=HERE.parents[1]; gd=root/'.git'
    if gd.is_file():
        value=gd.read_text().strip().removeprefix('gitdir: ')
        if os.name!='nt' and len(value)>2 and value[1]==':': value='/mnt/'+value[0].lower()+value[2:].replace('\\','/')
        gd=Path(value)
    prefix=HERE.relative_to(root).as_posix()+'/'
    result=subprocess.run(['git','--git-dir='+str(gd),'ls-tree','-r',commit+'^{commit}','--',prefix],check=True,capture_output=True,text=True)
    expected={}
    for line in result.stdout.splitlines():
        meta,path=line.split('\t'); name=path.removeprefix(prefix)
        if name.endswith('.py') or name=='contract.json': expected[name]=meta.split()[2]
    return expected

def source_binding(commit):
    expected=git_tree(commit)
    from launcher import blob
    actual={p.name:blob(p) for p in HERE.glob('*.py')}; actual['contract.json']=blob(HERE/'contract.json')
    if not expected or actual!=expected: raise ValueError('exact producer tree differs from executed wrapper')
    return expected

def comment(url):
    prefix='https://github.com/lsmfttb/spire-research/issues/44#issuecomment-'
    if not url or not url.startswith(prefix) or not url[len(prefix):].isdigit(): raise ValueError('Issue44 comment required')
    api='https://api.github.com/repos/lsmfttb/spire-research/issues/comments/'+url[len(prefix):]
    with urllib.request.urlopen(urllib.request.Request(api,headers={'User-Agent':'issue44-study'}),timeout=30) as response: value=json.load(response)
    if value['html_url']!=url or value['user']['login']!='lsmfttb': raise ValueError('comment ownership/URL mismatch')
    return value

def authorize(commit,directory,review_url,planner_url,resume_original=None):
    reviewer=comment(review_url); planner=comment(planner_url)
    if 'ISSUE44_READINESS_PASS' not in reviewer['body'].splitlines() or commit not in reviewer['body']: raise ValueError('independent exact-head readiness PASS required')
    if 'ISSUE44_FRESH_LAUNCH_AUTHORIZED' not in planner['body'].splitlines() or commit not in planner['body'] or review_url not in planner['body']:
        raise ValueError('explicit subsequent exact-head Planner grant required')
    if planner['created_at']<=reviewer['created_at']: raise ValueError('grant must follow review')
    if 'LINEAGE_DIRECTORY='+str(directory) not in planner['body'].splitlines(): raise ValueError('grant must bind sole new lineage directory')
    result={'producer_commit':commit,'directory':str(directory),'review_url':review_url,'review_body_sha256':sha(reviewer['body']),'planner_url':planner_url,'planner_body_sha256':sha(planner['body'])}
    if resume_original:
        if 'RESUME_LINEAGE_PRODUCER='+resume_original not in planner['body'].splitlines():
            raise ValueError('replacement grant must explicitly bind original lineage producer')
        result['resume_original_producer']=resume_original
    return result

def resume_identity(expected,stored,original,old_blobs):
    if stored['synthetic'] or stored['producer_commit']!=original or stored['wrapper_blobs']!=old_blobs:
        raise ValueError('original authentic lineage binding differs')
    if set(old_blobs)!=set(expected['wrapper_blobs']): raise ValueError('replacement executable set differs')
    # Only orchestration, worker admission and fixtures can change in this repair.
    allowed={'launch.py','worker.py','test_replication.py'}
    if any(expected['wrapper_blobs'][n]!=oid for n,oid in old_blobs.items() if n not in allowed):
        raise ValueError('scientific source/config changed during operational resume')
    comparison={**expected,**{n:stored[n] for n in ('producer_commit','wrapper_blobs','authorization_sha256')}}
    if comparison!=stored: raise ValueError('source/contract/retention lineage changed')
    return stored

def validate_execution(ident,execution,base):
    if source_binding(execution['producer_commit'])!=execution['wrapper_blobs']:
        raise ValueError('current execution tree differs')
    old=read(base/'authorization.json')
    if sha(old)!=ident['authorization_sha256'] or old['producer_commit']!=ident['producer_commit'] or old['directory']!=str(base):
        raise ValueError('original lineage authority changed')
    authority=read(base/'runtime-authorizations'/(execution['producer_commit']+'.json'))
    if sha(authority)!=execution['authorization_sha256'] or authority['producer_commit']!=execution['producer_commit'] or authority['directory']!=str(base):
        raise ValueError('current execution authority changed')
    if execution['producer_commit']!=ident['producer_commit']:
        if authority.get('resume_original_producer')!=ident['producer_commit']:
            raise ValueError('explicit original producer resume authority missing')
        resume_identity({**ident,'producer_commit':execution['producer_commit'],'wrapper_blobs':execution['wrapper_blobs']},ident,ident['producer_commit'],git_tree(ident['producer_commit']))

def frozen_requests(plan,models_path,freeze_sha):
    # The 1.4MB model/curve manifest is hashed once, never once per fresh key.
    return [{'plan':p,'models':str(models_path),'model_freeze_sha256':freeze_sha} for p in plan]

def prepare_requests(kind,payloads,ident,execution,base,jobs,supervisor,immutable):
    requests=[]; keys=[]
    for payload in payloads:
        supervisor.tick()
        key=[kind,sha(payload)]; keys.append(key)
        if jobs.get(key) is not None: continue
        req={'identity':ident,'execution':execution,'kind':kind,'payload':payload,'key':key,'store':str(base/'jobs'),'directory':str(base)}
        path=immutable(base/'requests'/(execution['producer_commit']+'-'+sha(key)+'.json'),req)
        requests.append(path)
    return requests,keys

def collect_results(keys,jobs,supervisor):
    results=[]
    for key in keys:
        supervisor.tick(); results.append(jobs.get(key))
    return results

def main():
    begin=time.monotonic(); cpu=time.process_time()
    parser=argparse.ArgumentParser(); parser.add_argument('mode',choices=['readiness','formal'])
    parser.add_argument('--source-code',required=True); parser.add_argument('--directory',required=True)
    parser.add_argument('--producer-commit',required=True); parser.add_argument('--review-comment'); parser.add_argument('--planner-comment')
    parser.add_argument('--resume-original-producer')
    args=parser.parse_args(); dep,tables,roots=dependencies(args.source_code)
    binding=source_binding(args.producer_commit)
    base=Path(args.directory).resolve()
    if base.is_relative_to(HERE.parents[1]) or base.is_relative_to(Path(CONTRACT['source_directory'])):
        raise ValueError('new independent outside-worktree lineage required')
    ident={'schema':'issue44-lineage-v1','producer_commit':args.producer_commit,'wrapper_blobs':binding,
           'contract_sha256':sha(CONTRACT),'source':dep,'synthetic':args.mode=='readiness','purpose':'STUDY_ONLY',
           'consumers':'Issue44 independent Reviewer and Planner','review_delete':CONTRACT['review_delete']}
    authorization=None
    if args.resume_original_producer and args.mode!='formal': raise ValueError('authentic resume only')
    if args.mode=='formal': authorization=authorize(args.producer_commit,base,args.review_comment,args.planner_comment,args.resume_original_producer)
    ident['authorization_sha256']=sha(authorization)
    if args.resume_original_producer:
        ident=resume_identity(ident,read(base/'identity.json'),args.resume_original_producer,git_tree(args.resume_original_producer))
    from launcher import immutable
    from formal import Completed
    import budget
    budget.HERE=HERE # Reviewed launcher integration: reuse guard, dispatch #44 worker.
    os.environ['ISSUE44_SOURCE_CODE']=str(Path(args.source_code).resolve())
    os.environ['PYTHONPATH']=str(HERE)+os.pathsep+os.environ.get('PYTHONPATH','')
    base.mkdir(parents=True,exist_ok=True)
    (base/'requests').mkdir(exist_ok=True)
    immutable(base/'identity.json',ident)
    execution={'producer_commit':args.producer_commit,'wrapper_blobs':binding,'authorization_sha256':sha(authorization)}
    if authorization:
        if not args.resume_original_producer: immutable(base/'authorization.json',authorization)
        (base/'runtime-authorizations').mkdir(exist_ok=True)
        immutable(base/'runtime-authorizations'/(args.producer_commit+'.json'),authorization)
        validate_execution(ident,execution,base)
    caps=CONTRACT['readiness_caps' if args.mode=='readiness' else 'formal_caps']
    supervisor=budget.Supervisor(base/'resources',ident,caps,job_cpu=300 if ident['synthetic'] else 6000,
        job_wall=300 if ident['synthetic'] else 3600,rss=CONTRACT['job_rss_bytes'],free=CONTRACT['free_memory_bytes'],
        startup_cpu=time.process_time()-cpu+1,startup_wall=time.monotonic()-begin+1)
    jobs=Completed(base/'jobs',ident)
    def run(kind,payloads):
        requests,keys=prepare_requests(kind,payloads,ident,execution,base,jobs,supervisor,immutable)
        supervisor.run(requests,CONTRACT['workers'])
        return collect_results(keys,jobs,supervisor)
    try:
        if ident['synthetic']:
            results=run('readiness',[{}]); immutable(base/'readiness.json',results[0]); return
        results=run('train',[{'block':b} for b in range(6)])
        blocks={b:{a:r['models'][a]['checkpoint'] for a in ('visible','masked')} for b,r in enumerate(results)}
        frozen={'identity':ident,'blocks':blocks,'optimization':{b:r['models'] for b,r in enumerate(results)}}
        models_path=immutable(base/'models.json',frozen)
        supervisor.tick(); freeze_sha=sha(frozen)
        plan=manifest(blocks); immutable(base/'evaluation-plan.json',{'identity':ident,'model_freeze_sha256':freeze_sha,'plan':plan})
        jobs.event({'boundary':'all12_models_frozen_before_any_fresh','model_freeze_sha256':freeze_sha,'execution':execution})
        run('fresh',frozen_requests(plan,models_path,freeze_sha))
        result=run('report',[{'models':str(models_path),'model_freeze_sha256':freeze_sha}])[0]
        immutable(base/'report.json',result)
        jobs.event({'boundary':'formal_complete','report_sha256':sha(result)})
    finally: supervisor.close()

if __name__=='__main__': main()
