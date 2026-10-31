"""Aggregate only observed round2 Sentry artifacts, keeping missing grades null."""
from pathlib import Path
from collections import Counter
import json
import hashlib

HERE=Path(__file__).resolve().parent
import sys as _sys
_sys.path.insert(0,str(HERE.parent))
import paths
protocol=json.loads((paths.ROUND_DIR/'protocol.json').read_text(encoding='utf-8'))
tasks=[]
for task in protocol['tasks']:
    case=paths.RUNS/'sentry'/'cases'/task
    recovery_pointer=case/'execution-recovery.json'
    if recovery_pointer.exists():
        pointer=json.loads(recovery_pointer.read_text(encoding='utf-8'))
        case_root=paths.RUNS/'sentry'/'cases'
        recorded=Path(pointer['caseDir'])
        case=(case_root/recorded.name) if (case_root/recorded.name).exists() else recorded.resolve()
        if case_root not in case.parents:raise RuntimeError('Recovery path outside this method workspace')
    path=case/'effective-result.json'
    if not path.exists():path=case/'result.json'
    if not path.exists():path=case/'agent-result.json'
    if not path.exists():
        tasks.append({'task':task,'attempted':(case/'worker-started.json').exists(),'A':None,'V':None,'Y':None})
        continue
    result=json.loads(path.read_text(encoding='utf-8-sig'))
    commands=[step['action']['command'].strip() for step in result.get('steps',[]) if isinstance(step.get('action'),dict) and 'command' in step['action']]
    freq=Counter(commands)
    pause_path=case/'execution-stop-pause.json'
    pause=json.loads(pause_path.read_text(encoding='utf-8')) if pause_path.exists() else None
    result.update(attempted=True,resultFile=str(path),terminalCommands=len(commands),
                  exactRepeatedCommands=sum(max(0,n-1) for n in freq.values()),
                  executionStopPause=pause,
                  knownTokens=result.get('usage'),
                  expectedChecks=protocol.get('expected_checks',{}).get(task),
                  gradingValidityScope='Complete original checks executed; this alone does not prove that each failure is an algorithm error')
    tasks.append(result)
known_A=[r['A'] for r in tasks if r.get('A') is not None]
known_Y=[r['Y'] for r in tasks if r.get('Y') is not None]
known_V=[r['V'] for r in tasks if r.get('V') is not None]
summary={
    'round':protocol['round'],'method':'Sentry official core with controlled terminal host',
    'sourceCommit':'1d91bca14fa6a4e17908856cc787eb8e8354395f',
    'sourceAlgorithmChanged':False,'actorDecodingAdapted':True,
    'hostVariant':'null-normalized',
    'strictDevelopmentRunsExcluded':str(paths.RUNS/'sentry'),
    'protocol':str(paths.ROUND_DIR/'protocol.json'),
    'runnerSha256':hashlib.sha256((HERE/'run_tb8.py').read_bytes()).hexdigest(),
    'paths':paths.resolved(),
    'tasks':tasks,'expectedTasks':len(tasks),'attemptedTasks':sum(bool(r['attempted']) for r in tasks),
    # Uniform counters shared with the Spine summary so one report reads both.
    'n':len(tasks),'attempted':sum(bool(r['attempted']) for r in tasks),
    'A':sum(known_A),'AInvalid':sum(1 for r in tasks if r.get('attempted') and r.get('A') is None),
    'V':sum(known_V),'Y':sum(known_Y),
    'knownArtifactGrades':len(known_A),'artifactPassCount':sum(known_A),
    'normalSubmissionCount':sum(known_V),'knownSubmissionResults':len(known_V),
    'knownEndToEndGrades':len(known_Y),'endToEndPassCount':sum(known_Y),
    'artifactPassRate':sum(known_A)/len(tasks) if len(known_A)==len(tasks) else None,
    'endToEndPassRate':sum(known_Y)/len(tasks) if len(known_Y)==len(tasks) else None,
    'totalModelRequests':sum(r.get('modelRequests',0) for r in tasks),
    'unknownUsageRequests':sum(r.get('unknownUsageRequests',0) for r in tasks),
    'knownTokens':{key:sum(r.get('usage',{}).get(key,0) for r in tasks) for key in ['prompt_tokens','completion_tokens','total_tokens']},
    'totalAgentWallSeconds':sum(r.get('agentWallSeconds',0) for r in tasks),
    'totalInterventions':sum(r.get('interventions',0) for r in tasks),
    'totalHardVerifiedRecoveries':sum(r.get('hardVerifiedRecoveries',0) for r in tasks),
    'totalSoftVerifiedRecoveries':sum(r.get('softVerifiedRecoveries',0) for r in tasks),
    'withinTaskLearnedEntries':sum(r.get('learnedEntries',0) for r in tasks),
    'crossTaskExperience':'Disabled by resetting Runner and empty playbook for every task',
    'repetitionMetrics':{'exactRepeatedCommands':'For each task, trim outer whitespace then sum max(0,count(command)-1) across identical terminal command strings; common subset for cross-method reporting',
                         'repeatedCommandObservationPairs':'Same command plus same visible truncated observation hash; Sentry-specific diagnostic proxy, not all repetitions are invalid'},
    'scope':'Round4 stratified 24-task development set (8 historical + 16 new), one fresh attempt each; not a native Sentry TB implementation, leaderboard result, or RSI learning-efficiency experiment',
    'grading':'Full writable candidate filesystem and /app archive frozen before hidden tests; no model feedback or agent retries in grading',
}
_out=Path(_sys.argv[1]) if len(_sys.argv)>1 else paths.RUNS/'sentry'/'summary.json'
_out.parent.mkdir(parents=True,exist_ok=True)
_out.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k not in ['tasks','repetitionMetrics']},ensure_ascii=False))
