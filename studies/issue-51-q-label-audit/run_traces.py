"""One read-only trace pass and durable measured execution receipt."""
import time
from audit import OUT, S46, load, write
from trace_audit import audit_traces

if __name__=='__main__':
    started=time.process_time()
    ledger=load(OUT/'execution.json'); ledger['stage']='TRACES_RUNNING'; write(OUT/'execution.json',ledger)
    summary=audit_traces(load(OUT/'owner_metrics.json'),S46,OUT)
    ledger['charges']['trace_wrapper_cpu']=time.process_time()-started
    ledger['stage']='TRACES_COMPLETE' if summary['complete'] and not summary['violations'] else 'TRACES_PARTIAL_OR_INVALID'
    write(OUT/'execution.json',ledger)
    print({k:summary.get(k) for k in ('complete','failure','measured_cpu_seconds')})
    print({'receipts':len(summary['receipts']),'violations':len(summary['violations']), 'orphan_files':len(summary['orphan_files'])})
