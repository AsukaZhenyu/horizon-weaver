"""Repeatable real-model exercises; this is acceptance work, not a frozen baseline.

python -m scripts.v1_model_acceptance --results-dir runs/model-exercises --repeats 3
Every run starts from new files. No scripted policy or model-output repair is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.core import Budget, Check, TaskContract, Termination
from horizon_weaver.evaluation import Evaluator
from horizon_weaver.harness import DockerHarness


@dataclass(frozen=True)
class Case:
    name: str
    goal: str
    files: tuple[tuple[str, str], ...]
    artifacts: tuple[str, ...]
    check_code: str
    immutable_inputs: tuple[str, ...] = ()

    def contract(self, max_steps: int, wall_time_s: float) -> TaskContract:
        checks = [Check(id="functional", kind="command",
                        command=shlex.join(["python", "-c", self.check_code]))]
        source = dict(self.files)
        for path in self.immutable_inputs:
            sha = hashlib.sha256(source[path].encode("utf-8")).hexdigest()
            code = ("import hashlib; from pathlib import Path; "
                    f"assert hashlib.sha256(Path({path!r}).read_bytes()).hexdigest() == {sha!r}")
            checks.append(Check(id="input:" + path, kind="command",
                                command=shlex.join(["python", "-c", code])))
        return TaskContract(task_id=self.name, goal=self.goal,
            constraints=("Use only this workspace and the Python standard library.",
                         "Keep the supplied data inputs unchanged. Acceptance is read-only."),
            expected_artifacts=self.artifacts, acceptance_checks=tuple(checks),
            budget=Budget(max_steps=max_steps, wall_time_s=wall_time_s, tool_timeout_s=10,
                          max_model_tokens=60_000, context_tokens=12_000,
                          output_tokens=2048, no_progress_limit=5))


CSV_CHECK = '''
import csv, json, math, random, statistics
from pathlib import Path
from summarize import summarize
def oracle(rows):
    groups = {}
    for row in rows:
        group = row.get('group', '').strip()
        try: value = float(row.get('value', ''))
        except (ValueError, TypeError): continue
        if group and math.isfinite(value): groups.setdefault(group, []).append(value)
    return {key: {'count':len(values), 'mean':round(statistics.mean(values),6),
                  'sample_std':round(statistics.stdev(values),6) if len(values)>1 else None}
            for key,values in groups.items()}
rng = random.Random(71)
for size in [0,1,4,20,100]:
    rows = [{'group':rng.choice(['A','B','C',' ']),
             'value':rng.choice(['','bad','nan',str(rng.uniform(-100,100))])} for _ in range(size)]
    original = json.dumps(rows,sort_keys=True)
    assert summarize(rows) == oracle(rows), 'group statistics differ'
    assert json.dumps(rows,sort_keys=True) == original, 'input rows were mutated'
with Path('metrics.csv').open(newline='') as stream: rows = list(csv.DictReader(stream))
assert json.loads(Path('summary.json').read_text()) == oracle(rows), 'summary artifact differs'
'''

WEIGHTS_CHECK = '''
import math, random
from weights import normalize_weights
rng = random.Random(83)
for values in [[],[0.0],[0,0,0],[2,3,5],[1e-30,2e-30]] + [[rng.random() for _ in range(n)] for n in range(1,25)]:
    original = values.copy()
    actual = normalize_weights(values)
    assert isinstance(actual,list) and len(actual)==len(values)
    assert values == original, 'input was mutated'
    if not values: assert actual == []; continue
    total = math.fsum(values)
    expected = [v/total for v in values] if total else [1/len(values)]*len(values)
    assert all(math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12) for a,b in zip(actual,expected))
for values in [[-1,2],[float('nan')],[float('inf')],[1,float('-inf')]]:
    try: normalize_weights(values)
    except ValueError: pass
    else: raise AssertionError('invalid weights must raise ValueError')
'''

SELECTION_CHECK = '''
import copy, json, math, random
from pathlib import Path
from select_run import select_run
def oracle(records):
    valid=[]
    for record in records:
        metrics=record.get('metrics',{})
        accuracy=metrics.get('accuracy'); latency=metrics.get('latency_ms')
        if record.get('status')!='completed': continue
        if not isinstance(accuracy,(int,float)) or not isinstance(latency,(int,float)): continue
        if not math.isfinite(accuracy) or not math.isfinite(latency): continue
        if not 0<=accuracy<=1 or latency<0: continue
        valid.append(record)
    if not valid: return None
    best=min(valid,key=lambda r:(-r['metrics']['accuracy'],r['metrics']['latency_ms'],r['run_id']))
    return {'run_id':best['run_id'],'params':best['params']}
rng=random.Random(97)
for size in [0,1,5,30]:
    records=[{'run_id':f'run-{i:03}', 'status':rng.choice(['completed','failed']),
              'params':{'seed':i}, 'metrics':{'accuracy':rng.choice([.7,.8,.9,None]),
              'latency_ms':rng.choice([10,20,30,-1])}} for i in range(size)]
    original=copy.deepcopy(records)
    assert select_run(records)==oracle(records), 'selection or tie-breaking differs'
    assert records==original, 'input records were mutated'
records=json.loads(Path('runs.json').read_text())
assert json.loads(Path('selection.json').read_text())==oracle(records), 'selection artifact differs'
'''

LOG_CHECK = r'''
import json, math, random, re
from pathlib import Path
from parse_log import parse_log
def oracle(text):
    records={}
    for line in text.splitlines():
        fields=dict(re.findall(r'\b(step|loss|val)=([^\s]+)',line))
        try: step=int(fields['step']); loss=float(fields['loss']); val=float(fields['val'])
        except (KeyError,ValueError): continue
        if step<0 or not math.isfinite(loss) or not math.isfinite(val): continue
        records[step]=(loss,val)
    if not records: return {'steps':0,'last_step':None,'last_loss':None,'best_step':None,'best_val':None}
    last=max(records); best=min(records,key=lambda step:(records[step][1],step))
    return {'steps':len(records),'last_step':last,'last_loss':records[last][0],
            'best_step':best,'best_val':records[best][1]}
rng=random.Random(109)
for size in [0,1,10,50]:
    lines=[f'INFO step={rng.randrange(15)} loss={rng.uniform(.1,2):.4e} val={rng.choice([.2,.3,.4])}' for _ in range(size)]
    lines+=['noise','step=3 loss=nan val=.1','step=4 loss=.5','step=x loss=.3 val=.2']
    rng.shuffle(lines); text='\n'.join(lines)
    assert parse_log(text)==oracle(text), 'log parsing differs'
assert json.loads(Path('report.json').read_text())==oracle(Path('training.log').read_text()), 'report artifact differs'
'''

GRID_CHECK = '''
import copy, itertools, json, random
from pathlib import Path
from build_manifest import build_runs
def oracle(config):
    return [{'id':f'b{batch}-lr{lr:g}-s{seed}','batch_size':batch,'learning_rate':lr,'seed':seed}
            for batch,lr,seed in itertools.product(sorted(set(config['batch_sizes'])),
                sorted(set(config['learning_rates'])),sorted(set(config['seeds'])))]
rng=random.Random(127)
for _ in range(15):
    config={'batch_sizes':[rng.choice([8,16,32]) for _ in range(rng.randrange(5))],
            'learning_rates':[rng.choice([.001,.0003,.01]) for _ in range(rng.randrange(5))],
            'seeds':[rng.randrange(4) for _ in range(rng.randrange(5))]}
    original=copy.deepcopy(config)
    assert build_runs(config)==oracle(config), 'grid content, ordering or IDs differ'
    assert config==original, 'configuration was mutated'
config=json.loads(Path('config.json').read_text())
assert json.loads(Path('manifest.json').read_text())==oracle(config), 'manifest artifact differs'
'''


CASES = (
    Case('csv-summary',
         'Create summarize.py exporting summarize(rows), where rows are dictionaries with group and value strings. '
         'Strip group names; skip empty groups and blank, nonnumeric or non-finite values. Return each remaining '
         'group mapped to count, mean and sample_std; round mean/std to 6 decimals, use None for std of one value. '
         'Do not mutate rows. CLI: python summarize.py metrics.csv summary.json. Generate summary.json.',
         (('metrics.csv','group,value\nA,2.5\nA,3.5\nA,\nB,8\nB,nan\nC,-1\nC,1\nD,bad\n'),),
         ('summarize.py','summary.json'), CSV_CHECK, ('metrics.csv',)),
    Case('normalize-weights',
         'Fix weights.py normalize_weights(weights) for scientific sampling. Return a new list of probabilities. '
         'Empty input returns []; all-zero weights return uniform probabilities. Finite nonnegative weights '
         'are normalized by their sum. Negative or non-finite values must raise ValueError. '
         'Do not mutate the supplied list; handle tiny positive weights.',
         (('weights.py','def normalize_weights(weights):\n    total = sum(weights)\n    return [w / total for w in weights]\n'),),
         ('weights.py',), WEIGHTS_CHECK),
    Case('experiment-selection',
         'Create select_run.py exporting select_run(records). Each record has run_id, status, params and metrics. '
         'Only completed records with finite numeric accuracy in [0,1] and finite nonnegative latency_ms are valid. '
         'Choose highest accuracy, then lowest latency_ms, then lexicographically smallest run_id. Return '
         '{"run_id":chosen_id,"params":chosen_params}, or None if no valid records. Do not mutate input. '
         'CLI: python select_run.py runs.json selection.json. Generate selection.json.',
         (('runs.json',json.dumps([
             {'run_id':'z','status':'completed','params':{'seed':1},'metrics':{'accuracy':.9,'latency_ms':20}},
             {'run_id':'a','status':'completed','params':{'seed':2},'metrics':{'accuracy':.9,'latency_ms':20}},
             {'run_id':'failed','status':'failed','params':{'seed':3},'metrics':{'accuracy':.99,'latency_ms':5}},
             {'run_id':'missing','status':'completed','params':{},'metrics':{'accuracy':None,'latency_ms':1}}
         ],indent=2)+'\n'),), ('select_run.py','selection.json'), SELECTION_CHECK, ('runs.json',)),
    Case('training-log',
         'Create parse_log.py exporting parse_log(text). From each line extract whitespace-separated step=, loss= '
         'and val= fields, in any order and with optional other text. Step must be a nonnegative integer; loss '
         'and val must be finite floats (including scientific notation). Ignore incomplete/invalid lines. '
         'For duplicate steps keep the last valid line. Return steps (number of unique steps), last_step '
         '(maximum step), last_loss, best_step (minimum val, ties smaller step) and best_val. Empty data has '
         'steps=0 and other values None. CLI: python parse_log.py training.log report.json. Generate report.json.',
         (('training.log','INFO step=0 loss=1.2 val=.9\nstep=2 val=4e-1 loss=.6\nnoise\n'
                         'step=1 loss=.8 val=.5\nstep=2 loss=5e-1 val=.3\nstep=3 loss=nan val=.1\n'),),
         ('parse_log.py','report.json'), LOG_CHECK, ('training.log',)),
    Case('experiment-grid',
         'Create build_manifest.py exporting build_runs(config). Config contains lists batch_sizes, '
         'learning_rates and seeds. Deduplicate each list, sort numerically, then enumerate the Cartesian '
         'product in batch_size, learning_rate, seed order. Each record has id, batch_size, learning_rate, seed; '
         'id uses Python format f"b{batch}-lr{lr:g}-s{seed}". An empty axis gives an empty list. '
         'Do not mutate config. CLI: python build_manifest.py config.json manifest.json. Generate manifest.json.',
         (('config.json',json.dumps({'batch_sizes':[16,8,16],'learning_rates':[.001,.0003],
                                   'seeds':[2,1]},indent=2)+'\n'),),
         ('build_manifest.py','manifest.json'), GRID_CHECK, ('config.json',)),
)


def save_report(path: Path, report: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.replace(path)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results-dir',type=Path,required=True)
    parser.add_argument('--case',choices=['all',*(case.name for case in CASES)],default='all')
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--image',default='python:3.12-slim')
    parser.add_argument('--model',default='qwen3.5-9b')
    parser.add_argument('--base-url',default='http://127.0.0.1:8080/v1')
    parser.add_argument('--temperature',type=float,default=.2)
    parser.add_argument('--max-steps',type=int,default=12)
    parser.add_argument('--wall-time',type=float,default=240)
    args=parser.parse_args()
    if args.repeats<1: parser.error('--repeats must be positive')
    root=args.results_dir.resolve()
    root.mkdir(parents=True,exist_ok=True)
    report_path=root/'summary.json'
    if report_path.exists(): parser.error('summary.json already exists; use a fresh results directory')
    cases=[case for case in CASES if args.case=='all' or args.case==case.name]
    report={'kind':'real-model engineering acceptance exercises',
            'started_at':datetime.now(timezone.utc).isoformat(),
            'model':args.model,'base_url':args.base_url,'temperature':args.temperature,
            'image':args.image,'repeats':args.repeats,'runs':[]}
    save_report(report_path,report)
    for case in cases:
        task=case.contract(args.max_steps,args.wall_time)
        for repetition in range(1,args.repeats+1):
            label=f'{case.name}-{repetition}'
            workspace=root/'workspaces'/label
            run_dir=root/'tasks'/label
            workspace.mkdir(parents=True)
            for name,text in case.files:
                (workspace/name).write_bytes(text.encode('utf-8'))
            (root/(label+'-contract.json')).write_text(task.model_dump_json(indent=2),encoding='utf-8')
            print(json.dumps({'event':'start','case':case.name,'repeat':repetition}),flush=True)
            result=NodeAgent(backend='docker',image=args.image,model=args.model,
                             base_url=args.base_url,temperature=args.temperature).run(task,workspace,run_dir)
            graph=read_run(run_dir)
            audit_passed=None
            audit_error=None
            if result.termination==Termination.SUCCESS:
                harness=DockerHarness(workspace,args.image)
                try:
                    harness.start(task,30)
                    audit_passed=Evaluator(harness).assess(task,30).verdict.passed
                except Exception as error:
                    audit_error=f'{type(error).__name__}: {error}'
                finally:
                    try:
                        harness.close()
                    except Exception as error:
                        detail=f'cleanup {type(error).__name__}: {error}'
                        audit_error=(audit_error+'; ' if audit_error else '')+detail
            row={'case':case.name,'repeat':repetition,'run_id':result.run_id,
                 'termination':result.termination.value,'reason':result.reason,
                 'steps':result.steps,'elapsed_s':round(result.elapsed_s,3),
                 'usage':result.usage.model_dump(),'verdict_passed':result.verdict.passed,
                 'post_audit_passed':audit_passed,'audit_error':audit_error,
                 'actions':dict(Counter(attempt.action.kind.value for attempt in graph.attempts)),
                 'result_file':str(run_dir/'result.json')}
            report['runs'].append(row)
            report['completed']=len(report['runs'])
            report['successes']=sum(row['termination']=='success' and row['post_audit_passed'] is True
                                    and row['usage']['input_tokens']+row['usage']['output_tokens']>0
                                    for row in report['runs'])
            report['false_completions']=sum(row['termination']=='success' and row['post_audit_passed'] is False
                                            for row in report['runs'])
            save_report(report_path,report)
            print(json.dumps({'event':'result',**row},ensure_ascii=False),flush=True)
            if audit_error or result.termination in (Termination.MODEL_ERROR,Termination.HARNESS_ERROR):
                print('Stopping batch on an external failure; completed results are preserved.',flush=True)
                return 1
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    save_report(report_path,report)
    return 0 if report['successes']==len(report['runs']) else 1


if __name__=='__main__':
    raise SystemExit(main())
