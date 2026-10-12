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

def source_binding(commit):
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

def authorize(commit,directory,review_url,planner_url):
    reviewer=comment(review_url); planner=comment(planner_url)
    if 'ISSUE44_READINESS_PASS' not in reviewer['body'] or commit not in reviewer['body']: raise ValueError('independent exact-head readiness PASS required')
    if 'ISSUE44_FRESH_LAUNCH_AUTHORIZED' not in planner['body'] or commit not in planner['body'] or review_url not in planner['body']:
        raise ValueError('explicit subsequent exact-head Planner grant required')
    if planner['created_at']<=reviewer['created_at']: raise ValueError('grant must follow review')
    if 'LINEAGE_DIRECTORY='+str(directory) not in planner['body'].splitlines(): raise ValueError('grant must bind sole new lineage directory')
    return {'review_url':review_url,'review_body_sha256':sha(reviewer['body']),'planner_url':planner_url,'planner_body_sha256':sha(planner['body'])}

def main():
    begin=time.monotonic(); cpu=time.process_time()
    parser=argparse.ArgumentParser(); parser.add_argument('mode',choices=['readiness','formal'])
    parser.add_argument('--source-code',required=True); parser.add_argument('--directory',required=True)
    parser.add_argument('--producer-commit',required=True); parser.add_argument('--review-comment'); parser.add_argument('--planner-comment')
    args=parser.parse_args(); dep,tables,roots=dependencies(args.source_code)
    binding=source_binding(args.producer_commit)
    base=Path(args.directory).resolve()
    if base.is_relative_to(HERE.parents[1]) or base.is_relative_to(Path(CONTRACT['source_directory'])):
        raise ValueError('new independent outside-worktree lineage required')
    ident={'schema':'issue44-lineage-v1','producer_commit':args.producer_commit,'wrapper_blobs':binding,
           'contract_sha256':sha(CONTRACT),'source':dep,'synthetic':args.mode=='readiness','purpose':'STUDY_ONLY'}
    authorization=None
    if args.mode=='formal': authorization=authorize(args.producer_commit,base,args.review_comment,args.planner_comment)
    from launcher import immutable
    from formal import Completed
    import budget
    budget.HERE=HERE # Reviewed launcher integration: reuse guard, dispatch #44 worker.
    os.environ['ISSUE44_SOURCE_CODE']=str(Path(args.source_code).resolve())
    os.environ['PYTHONPATH']=str(HERE)+os.pathsep+os.environ.get('PYTHONPATH','')
    immutable(base/'identity.json',ident)
    if authorization: immutable(base/'authorization.json',authorization)
    caps=CONTRACT['readiness_caps' if args.mode=='readiness' else 'formal_caps']
    supervisor=budget.Supervisor(base/'resources',ident,caps,job_cpu=300 if ident['synthetic'] else 6000,
        job_wall=300 if ident['synthetic'] else 3600,rss=CONTRACT['job_rss_bytes'],free=CONTRACT['free_memory_bytes'],
        startup_cpu=time.process_time()-cpu+1,startup_wall=time.monotonic()-begin+1)
    jobs=Completed(base/'jobs',ident)
    def run(kind,payloads):
        requests=[]; keys=[]
        for payload in payloads:
            key=[kind,sha(payload)]; keys.append(key)
            req={'identity':ident,'kind':kind,'payload':payload,'key':key,'store':str(base/'jobs'),'directory':str(base)}
            path=immutable(base/'requests'/(sha(key)+'.json'),req)
            if jobs.get(key) is None: requests.append(path)
        supervisor.run(requests,CONTRACT['workers'])
        return [jobs.get(key) for key in keys]
    try:
        if ident['synthetic']:
            results=run('readiness',[{}]); immutable(base/'readiness.json',results[0]); return
        results=run('train',[{'block':b} for b in range(6)])
        blocks={b:{a:r['models'][a]['checkpoint'] for a in ('visible','masked')} for b,r in enumerate(results)}
        frozen={'identity':ident,'blocks':blocks,'optimization':{b:r['models'] for b,r in enumerate(results)}}
        models_path=immutable(base/'models.json',frozen)
        plan=manifest(blocks); immutable(base/'evaluation-plan.json',{'identity':ident,'model_freeze_sha256':sha(frozen),'plan':plan})
        jobs.event({'boundary':'all12_models_frozen_before_any_fresh','model_freeze_sha256':sha(frozen)})
        run('fresh',[{'plan':p,'models':str(models_path),'model_freeze_sha256':sha(frozen)} for p in plan])
        result=run('report',[{'models':str(models_path),'model_freeze_sha256':sha(frozen)}])[0]
        immutable(base/'report.json',result)
        jobs.event({'boundary':'formal_complete','report_sha256':sha(result)})
    finally: supervisor.close()

if __name__=='__main__': main()
