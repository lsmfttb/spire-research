"""Run only the existing T041 occurrence/target fixtures without a pytest install."""
import ast
import json
from pathlib import Path
import resource
import sys
import time

begin=time.monotonic(); cpu=time.process_time()
source=Path(sys.argv[1])/'tests/test_oracle_search.py'
tree=ast.parse(source.read_text())
names={'_actions','_context','_row','_raw_search',
       'test_oracle_root_mapping_preserves_potion_identities',
       'test_oracle_root_mapping_allows_reported_unmapped_search_edge_visits'}
nodes=[n for n in tree.body if isinstance(n,(ast.Import,ast.ImportFrom)) and not (isinstance(n,ast.Import) and any(a.name=='pytest' for a in n.names))]
nodes += [n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
scope={}; exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),scope)
for name in sorted(names):
    if name.startswith('test_'):scope[name]()
print(json.dumps({'status':'PASS','fixtures':2,'tests':sorted(n for n in names if n.startswith('test_')),
                  'cpu':time.process_time()-cpu,'wall':time.monotonic()-begin}))
