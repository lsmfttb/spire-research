from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
import torch
ap=argparse.ArgumentParser();ap.add_argument('--pilot-root',type=Path,required=True);ap.add_argument('--arm-out',type=Path,required=True);a=ap.parse_args()
pilot=a.pilot_root.resolve(strict=True);arm='B'; out=a.arm_out.resolve(strict=True);snap=out/'snapshots.pt';metrics=out/'metrics.json'
if metrics.exists(): raise SystemExit('metrics file already exists; refusing overwrite')
blob=torch.load(snap,map_location='cpu',weights_only=False)
if blob.get('schema_id')!='issue85-offline-performance-baseline-v1' or blob.get('arm')!=arm: raise SystemExit('snapshot identity mismatch')
seed=json.loads((pilot/'pilot-seed-table.json').read_text())
rows=sorted([r for r in seed['rows'] if r.get('phase')=='training' and r.get('formal') is False and r.get('issue85_arm')==arm],key=lambda r:int(r['owner_seed']))
package='0297d60382e5dfe64d8de93f6ca383e96b655f40'; train=pilot.parent/package/'training'/arm; inputs=[]
for row in rows:
 key=''.join(c if c.isalnum() or c in '-_' else '-' for c in row['unit_id']); d=train/'units'/key
 results=[]
 for rp in sorted(d.glob('attempt-*/result.json')):
  result=json.loads(rp.read_text())
  if result.get('status')=='SUCCEEDED' and result.get('package_commit')==package and result.get('unit_id')==row['unit_id'] and result.get('owner_seed')==row['owner_seed']:
   results.append((result,rp))
 if len(results)!=1: raise SystemExit(f'expected one successful saved receipt for {row["unit_id"]}, got {len(results)}')
 result,rp=results[0];feat=result['training_features'];fp=train/feat['path']
 if hashlib.sha256(fp.read_bytes()).hexdigest()!=feat['sha256']: raise SystemExit('feature SHA mismatch')
 inputs.append({'unit_id':row['unit_id'],'owner_seed':row['owner_seed'],'package_commit':package,'result_sha256':hashlib.sha256(rp.read_bytes()).hexdigest(),'training_features_sha256':feat['sha256'],'training_features_uncompressed_sha256':feat['uncompressed_sha256']})
reuse=json.loads((pilot/'pilot-actor-update-reuse.json').read_text());receipt=reuse['actor_update_receipt']
def stat(v):
 if not v:return None
 v=sorted(v);n=len(v)
 return {'count':n,'min':v[0],'median':v[round((n-1)*.5)],'p90':v[round((n-1)*.9)],'max':v[-1]}
def tofloat(x):return float(x.item()) if torch.is_tensor(x) else x
steps=[]; candidate_counts=[]
for s in blob['steps']:
 counts=[int(x.shape[0]) for x in s['inputs']];candidate_counts=counts
 steps.append({'step':s['step'],'total_loss':tofloat(s['total_loss']),'grad_norm_before_clip':tofloat(s['grad_norm_before_clip']),'owner_count':len(s['owner_losses']),
  'decision_input_count':len(s['inputs']),'encoded_input_count':len(s['encoded_features']),'rss_bytes_after_step':s.get('rss_bytes_after_step'),
  'timings':s['timings']})
report={'schema_id':'issue85-offline-performance-baseline-v1','scope':'existing pilot data only; no games/replays/formal training','runner_commit':'5cd70c00d03f1104728efdc4b66c764592e5acf8',
 'runner_source_lock_sha256':'a25c25cd4144cbe362c7c241460e9d71f4554627c9bb129407ef69ec8366404a','issue84_commit':'556128df66797bf979d36b56b6920ac88bab5b35','issue84_source_lock_sha256':'58ce736dbab1ac750e6e5771f644391610930ec4e9a87876f013bfd28838ef52',
 'pilot_package_commit':'5cd70c00d03f1104728efdc4b66c764592e5acf8','arm':arm,'mode':'uncached_baseline','python_version':'3.13.13','torch_version':'2.12.0+cpu','numpy_version':'2.4.6','torch_num_threads':1,
 'input_receipts':inputs,'actor_checkpoint_sha256':receipt['checkpoint_sha256'],'optimizer_checkpoint_sha256':receipt['optimizer_sha256'],'actor_model_digest_start':receipt['model_digest'],
 'owner_count':16,'decision_count':len(blob['steps'][0]['inputs']),'candidate_count_distribution':stat(candidate_counts),'candidate_context_cardinality':{'context_token_count':None,'candidate_token_count':None,'candidate_context_cross_products':None},
 'rss_bytes_before_updates':None,'ru_maxrss_before_updates':None,'steps':steps,'interpretation':'Recovered the complete two-step snapshot saved before a metrics JSON serialization error; update was not rerun. Step RSS is sampled after each step; process peak RSS was not persisted in that process. Step 1 is first measured update after source/model/data loading; step 2 is repeated update, not a long-run steady-state estimate.',
 'report_recovery':'metrics.json assembled from the immutable snapshots.pt and reverified source receipts; no training update repeated.'}
report['snapshot_sha256']=hashlib.sha256(snap.read_bytes()).hexdigest();report['snapshot_bytes']=snap.stat().st_size
with metrics.open('xb') as f:f.write((json.dumps(report,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode())
print(json.dumps({'arm':arm,'metrics':str(metrics),'snapshot_sha256':report['snapshot_sha256'],'steps':steps},sort_keys=True))
