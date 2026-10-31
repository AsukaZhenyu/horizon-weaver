"""TB8 controlled terminal host around unchanged official Sentry core.

No per-task solution hints. This is an execution smoke adaptation, not the
original AppWorld driver. Actor decoding belongs to this host, while auxiliary
Sentry decoding and all detector/verifier/playbook code remain unchanged.
"""
from pathlib import Path
from collections import Counter
from dataclasses import asdict
import argparse
import base64
import ctypes
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import paths
ROUND = paths.ROUND_DIR
UPSTREAM = paths.SENTRY_SRC
TASK_ROOT = paths.require_tasks()
CASES = paths.RUNS / 'sentry' / 'cases'
sys.path.insert(0, str(UPSTREAM))
import yaml
from Sentry import SentryRunner, SentryConfig, ModelConfig, agent_step_from_parts
from Sentry.clients import ChatCompletionsClient, ModelOutputTruncatedError, _NoRedirect, _json_object
from Sentry.config import PlaybookConfig

PROTOCOL = json.loads((ROUND / 'protocol.json').read_text(encoding='utf-8'))
BASE_IMAGES = {entry['task']:entry for entry in json.loads((ROUND / 'base-images.json').read_text(encoding='utf-8'))}
TASKS = PROTOCOL['tasks']
HOST_VARIANT = 'null-normalized'
CONTAINER_PREFIX = 'week8r3-sentry-normalized-'
DOCKER_ENV = {**os.environ, 'DOCKER_CONFIG': str(paths.DOCKER_CONFIG),
              'DOCKER_HOST': os.environ.get('DOCKER_HOST','npipe:////./pipe/dockerDesktopLinuxEngine'), 'DOCKER_CLI_HINTS': 'false'}
SCHEMA = '''Return exactly one JSON object, with no Markdown fences or surrounding prose.
To execute a shell command in the isolated terminal use:
{"reasoning":"short purpose of the next action","command":"shell command","timeout_seconds":30}
To submit the final result use:
{"reasoning":"why the work is ready","final":"brief completion report"}
reasoning and timeout_seconds are optional. Choose exactly one of command or final.
Commands execute in /app. Each shell command starts a new shell. Use files for persistent state.
The host caps each command at 60 seconds. The whole task has 300 seconds including model calls.
Use the available terminal to inspect and modify the environment. Follow the task instruction.
No hidden evaluator is available during execution.'''


def clock():
    if os.name == 'nt':
        count, freq = ctypes.c_longlong(), ctypes.c_longlong()
        ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(count))
        ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
        return count.value / freq.value
    return time.monotonic()


def save(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def docker(argv, seconds=30):
    start = clock()
    try:
        p = subprocess.run(['docker', *argv], capture_output=True, env=DOCKER_ENV, timeout=seconds)
        return {'code': p.returncode, 'stdout': p.stdout.decode('utf-8', 'replace'),
                'stderr': p.stderr.decode('utf-8', 'replace'), 'seconds': clock()-start, 'timed_out': False,
                'stdout_base64':base64.b64encode(p.stdout).decode(), 'stderr_base64':base64.b64encode(p.stderr).decode()}
    except subprocess.TimeoutExpired as exc:
        return {'code': None, 'stdout': (exc.stdout or b'').decode('utf-8', 'replace'),
                'stderr': (exc.stderr or b'').decode('utf-8', 'replace'),
                'seconds': clock()-start, 'timed_out': True,
                'stdout_base64':base64.b64encode(exc.stdout or b'').decode(),
                'stderr_base64':base64.b64encode(exc.stderr or b'').decode()}


def phase(prompt):
    for head, name in [('[SENTRY DETECTOR]', 'detector'),
                       ('[SENTRY ESCAPE VERIFIER]', 'verifier'),
                       ('[SENTRY PLAYBOOK UPDATE]', 'updater')]:
        if prompt.startswith(head):
            return name
    return 'actor'


class BudgetExpired(RuntimeError):
    pass


class RecordedClient(ChatCompletionsClient):
    """Same official decoding/parser; instrumented bounded HTTP transport only."""
    def attach(self, case, deadline, expired, drain):
        self.case, self.deadline, self.expired = case, deadline, expired
        self.drain = drain
        self.calls = []
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def __call__(self, prompt, **kwargs):
        if self.expired.is_set() or clock() >= self.deadline:
            raise BudgetExpired('total 300 second agent budget exhausted')
        self.drain()
        item = {'index':len(self.calls)+1, 'phase':phase(prompt), 'prompt':prompt,
                'decoding':kwargs, 'started_unix':time.time()}
        self.calls.append(item)
        self.active = item
        save(self.case / 'requests' / f"{item['index']:04d}.json", item)
        started = clock()
        try:
            answer = super().__call__(prompt, **kwargs)
            item.update(text=answer,status='completed')
            return answer
        except Exception as exc:
            item.update(error_type=type(exc).__name__, error=str(exc),status='error')
            if isinstance(exc, ModelOutputTruncatedError):
                item.update(partial_text=exc.partial_text, finish_reason=exc.finish_reason)
            raise
        finally:
            item.update(seconds=clock()-started, usage=self.last_usage,
                        metadata=self.last_response_metadata)
            save(self.case / 'requests' / f"{item['index']:04d}.json", item)
            save(self.case / 'usage-progress.json', {'calls':len(self.calls), 'last_phase':item['phase'],
                'prompt_tokens':sum(c.get('usage',{}).get('prompt_tokens',0) for c in self.calls),
                'completion_tokens':sum(c.get('usage',{}).get('completion_tokens',0) for c in self.calls)})

    def _post(self, suffix, payload, auth_headers):
        remaining = self.deadline-clock()
        if remaining <= 0 or self.expired.is_set():
            raise BudgetExpired('deadline before HTTP request')
        self.active['http_request'] = payload
        save(self.case / 'requests' / f"{self.active['index']:04d}.json", self.active)
        request = Request(self.base_url.rstrip('/')+suffix,
                          data=json.dumps(payload, allow_nan=False).encode('utf-8'),
                          headers={'Content-Type':'application/json','Accept':'application/json',**auth_headers},
                          method='POST')
        try:
            with self.opener.open(request, timeout=remaining) as response:
                body = response.read()
                self.active.update(http_status=response.status, raw_response=body.decode('utf-8','replace'))
        except HTTPError as exc:
            body = exc.read()
            self.active.update(http_status=exc.code, raw_response=body.decode('utf-8','replace'))
            raise RuntimeError(f'HTTP {exc.code}: '+body.decode('utf-8','replace')) from None
        data = _json_object(body)
        if data.get('error'):
            raise RuntimeError('Model endpoint returned API error: '+str(data['error']))
        return data


def parse_action(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Action must be one JSON object')
    # Null means an unused optional field, not an executable action. Preserve
    # the complete raw model text separately; normalize only host routing.
    value = {key:item for key,item in value.items() if item is not None}
    if set(value)-{'reasoning','command','timeout_seconds','final'}:
        raise ValueError('Unknown action fields')
    if 'reasoning' in value and not isinstance(value['reasoning'], str):
        raise ValueError('reasoning must be a string')
    if ('command' in value) == ('final' in value):
        raise ValueError('Choose exactly one of command or final')
    kind = 'command' if 'command' in value else 'final'
    if not isinstance(value[kind], str) or not value[kind].strip():
        raise ValueError(kind+' must be a nonempty string')
    if kind == 'final' and 'timeout_seconds' in value:
        raise ValueError('timeout_seconds applies to commands only')
    limit = value.get('timeout_seconds',30)
    if type(limit) not in (int,float) or not math.isfinite(limit) or limit <= 0:
        raise ValueError('timeout_seconds must be finite and positive')
    return value


def observed_output(result, maximum=12000):
    content = json.dumps({k: result[k] for k in ['code','stdout','stderr','timed_out']}, ensure_ascii=False)
    if len(content)>maximum:
        return content[:6000]+'\n[Host truncated observation; full command output retained in operator logs.]\n'+content[-6000:]
    return content


def one_task(task, infra_retry=False):
    original_case = CASES / task
    if infra_retry:
        previous = json.loads((original_case/'agent-result.json').read_text(encoding='utf-8'))
        if previous.get('steps'):
            raise RuntimeError('Infrastructure-only retry requires zero executed actions')
        prior_model_requests = previous.get('modelRequests', 0)
    case = CASES / (task+'-execution-recovery' if infra_retry else task)
    case.mkdir(parents=True, exist_ok=True)
    if (case/'agent-result.json').exists() or (case/'worker-started.json').exists():
        raise RuntimeError('Refusing to repeat an attempted task')
    (case/'requests').mkdir(exist_ok=True)
    (case/'commands').mkdir(exist_ok=True)
    name = CONTAINER_PREFIX+task+('-execution-recovery' if infra_retry else '')
    image = 'tb__'+task+'__client:latest'
    image_ref = BASE_IMAGES[task]['id']
    task_yaml = (TASK_ROOT/task/'task.yaml').read_bytes()
    instruction = yaml.safe_load(task_yaml)['instruction']
    inspect = docker(['image','inspect',image_ref],60)
    if inspect['code'] != 0:
        raise RuntimeError('Task image unavailable: '+inspect['stderr'])
    image_id = json.loads(inspect['stdout'])[0]['Id']
    save(case/'source.json', {'task':task,'image':image,'imageId':image_id,
         'taskYamlSha256':hashlib.sha256(task_yaml).hexdigest(),
         'benchmarkCommit':'d28711d0da2675d0bb1d56de45ae5df6082438a3',
         'sentryCommit':'1d91bca14fa6a4e17908856cc787eb8e8354395f',
         'label':'historical TB0.2.18 development subset, controlled terminal host',
         'actorTemperature':0.2,'actorMaxTokens':2048,'context':16384,
         'auxTemperature':0,'auxMaxTokens':4096,'agentBudgetSeconds':300,
         'sentryNativeContinuationUnused':{'temperature':0.7,'max_tokens':384},
         'taskActorOwnedByHost':True,'crossTaskExperience':'reset to empty each task'})
    if infra_retry:
        save(case/'infrastructure-recovery-source.json', {'sourceCase':str(original_case),
             'priorModelRequests':prior_model_requests,'priorActions':0,'sameInitialImageId':image_id,
             'reason':'Prior attempt had no executed environment action: model proposed a harmless command, docker exec transport timed out before it ran; original attempt retained separately'})
    save(case/'agent-visible-input.json', {'instruction':instruction,'genericActionSchema':SCHEMA,
         'taskSpecificHints':False,'hiddenTestsAvailable':False})
    save(case/'host-config.json', {
         'protocolSha256':hashlib.sha256((ROUND/'protocol.json').read_bytes()).hexdigest(),
         'actor':{'temperature':0.2,'max_tokens':2048,'owner':'controlled terminal host'},
         'sentryAuxiliary':{'temperature':0,'max_tokens':4096,'structured_outputs':True},
         'unusedNativeSentryContinuation':{'temperature':0.7,'max_tokens':384,
              'reason':'SentryRunner.generate_repair is not called; repair prompts enter the shared host actor history'},
         'commandLimitSeconds':60,'commandKillAfterSeconds':2,
         'dockerCommandTransportAllowanceSeconds':8,
         'visibleObservationLimitCharacters':12000,'truncation':'first 6000 and last 6000 chars with explicit marker',
         'fullCommandLogs':'commands/*.json retain decoded output and exact bytes as base64',
         'taskBudgetSeconds':300,'failsafeMaxSteps':1000,'crossTaskMemory':'empty new playbook each task',
         'deadlineAction':'internal Timer requests docker pause; no additional actions after deadline; pause RPC latency recorded',
         'hostSupervisorSeconds':330,'contextOverflow':'stop and record error; no host compression or retry',
         'submission':'public final request is checked by Sentry before normal host acceptance',
         'hostVariant':HOST_VARIANT,
         'runnerSha256AtWorkerStart':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         'nullNormalization':'Drop null-valued fields before existing action validation; no changes to official Sentry code, action prompt, or task-specific guidance',
         'hiddenGrading':'only after worker stop and full writable filesystem plus /app snapshot'})
    start_container = docker(['run','-d','--name',name,'--workdir','/app',image_ref,'sleep','infinity'],60)
    save(case/'container-start.json',start_container)
    if start_container['code'] != 0:
        save(case/'startup-failure-state.json',docker(['inspect',name],12))
        raise RuntimeError('Cannot start task container; nonzero or unknown transport exit')
    running = docker(['inspect',name,'--format','{{.State.Running}}'],12)
    save(case/'startup-running-verification.json',running)
    if running['code'] != 0 or running['stdout'].strip() != 'true':
        raise RuntimeError('Task container is not verified running before any model request')
    guard = None
    events = []
    def drain():
        if guard is None:
            return
        fresh = guard.guard.drain_runtime_events()
        if fresh:
            events.extend(fresh)
            with (case/'sentry-events.jsonl').open('a',encoding='utf-8') as log:
                for event in fresh:
                    log.write(json.dumps(event,ensure_ascii=False)+'\n')
    started = clock()
    deadline = started+300
    expired = threading.Event()
    pause_lock = threading.Lock()
    paused = False
    def pause(reason):
        nonlocal paused
        with pause_lock:
            if not paused:
                record = docker(['pause',name],12)
                paused = record['code']==0
                save(case/'execution-stop-pause.json',{'reason':reason,'agentSeconds':clock()-started,**record})
    def expire():
        expired.set()
        pause('300 second hard deadline')
    timer = threading.Timer(300,expire)
    timer.daemon = True
    save(case/'worker-started.json',{'pid':os.getpid(),'started_unix':time.time(),'container':name})
    timer.start()
    model = RecordedClient(base_url=os.environ.get('HW_MODEL_URL','http://127.0.0.1:8080')+'/v1',model='qwen3.5-9b',
                           timeout=300,json_mode='native',max_retries=0)
    model.attach(case,deadline,expired,drain)
    config = SentryConfig(model=ModelConfig(model='qwen3.5-9b'),
                           playbook=PlaybookConfig(persist_path=str(case/'playbook.json')))
    guard = SentryRunner(model,config,capture_runtime_events=True)
    save(case/'sentry-config.json',asdict(config))
    result = {'task':task,'method':'sentry','V':0,'A':None,'Y':None,
              'normalSubmission':False,'stop_reason':'running','steps':[],
              'initialPlaybookEntries':len(guard.guard.playbook.entries)}
    repeated = 0
    seen_outputs = set()
    try:
        initial = docker(['exec','--workdir','/app',name,'sh','-lc','pwd; ls -la /app'],12)
        save(case/'initial-observation.json',initial)
        if initial['code'] != 0:
            raise RuntimeError('Initial terminal observation failed')
        guard.start_task(objective=instruction,action_schema=SCHEMA)
        history = [instruction,SCHEMA,'Initial terminal observation:\n'+observed_output(initial)]
        guidance = None
        for step_id in range(1000):
            if expired.is_set() or clock() >= deadline:
                raise BudgetExpired('total agent budget exhausted')
            prompt = '\n\n'.join(history) + ('\n\n'+guidance if guidance else '') + '\n\nNext action:'
            truncated = None
            try:
                raw = model(prompt,temperature=0.2,max_tokens=2048)
            except ModelOutputTruncatedError as exc:
                raw=exc.partial_text
                truncated='Incomplete output reached max_tokens=2048; return one complete next action.'
            if expired.is_set() or clock() >= deadline:
                raise BudgetExpired('budget exhausted after inference')
            parsed=True;error=None;action=None;terminal=False
            try:
                if truncated:
                    raise ValueError(truncated)
                action=parse_action(raw)
            except (ValueError,json.JSONDecodeError) as exc:
                parsed=False;error=str(exc)
            if parsed and 'command' in action:
                command=action['command']
                remaining=deadline-clock()
                seconds=min(60.0,float(action.get('timeout_seconds',30)),max(0.1,remaining))
                limit=str(max(0.1,seconds))
                command_result=docker(['exec','--workdir','/app',name,'timeout','--signal=TERM','--kill-after=2',limit,'sh','-lc',command],seconds+8)
                save(case/'commands'/f'{step_id:04d}.json',{'command':command,'limit_seconds':seconds,**command_result})
                if command_result['timed_out']:
                    pause('docker transport timeout; stop uncertain background execution')
                    result['stop_reason']='command_transport_timeout'
                    raise RuntimeError('Docker exec transport deadline exceeded')
                observation=observed_output(command_result)
                accepted=command_result['code']==0
                repeat_key=(command,hashlib.sha256(observation.encode()).hexdigest())
                if repeat_key in seen_outputs:
                    repeated+=1
                seen_outputs.add(repeat_key)
            elif parsed:
                terminal=True;accepted=True
                observation='Agent requested final submission. No hidden evaluation has been performed.'
            else:
                accepted=False
                observation='Action rejected before execution: '+error
            reasoning=action.get('reasoning','') if action else ''
            item={'step_id':step_id,'raw':raw,'action':action,'parsed_ok':parsed,
                  'observation':observation,'accepted_by_environment':accepted,
                  'submission_requested':terminal,'elapsed_seconds':clock()-started}
            result['steps'].append(item)
            step=agent_step_from_parts(step_id=step_id,reasoning=reasoning,raw_action=raw,
                 observation=observation,parsed_ok=parsed,schema_valid=parsed,
                 parser_error=error,accepted_by_environment=accepted)
            # Submission is a public host request. Sentry may intervene before
            # it is accepted, just as its published AppWorld driver does.
            repair=guard.step(step,terminal=expired.is_set() or clock()>=deadline)
            drain()
            if expired.is_set() or clock() >= deadline:
                raise BudgetExpired('budget exhausted by Sentry auxiliary inference')
            history.extend(['Action: '+raw,'Observation: '+observation])
            guidance=repair.prompt_text if repair else None
            save(case/'progress.json',{'steps':len(result['steps']),'calls':len(model.calls),
                 'elapsed_seconds':clock()-started,'sentryEvents':dict(Counter(e['type'] for e in events))})
            print(json.dumps({'task':task,'step':step_id,'calls':len(model.calls),
                 'seconds':round(clock()-started,2),'intervention':repair.route if repair else None}),flush=True)
            if terminal and repair is None:
                result.update(V=1,normalSubmission=True,stop_reason='submitted',finalAnswer=action['final'])
                break
        else:
            result['stop_reason']='1000_step_failsafe'
    except BaseException as exc:
        message=str(exc)
        if expired.is_set() or clock() >= deadline or isinstance(exc,BudgetExpired):
            result['stop_reason']='wall_timeout'
        elif 'context' in message.lower() and ('exceed' in message.lower() or 'size' in message.lower()):
            result['stop_reason']='context_overflow'
        elif result['stop_reason']=='running':
            result['stop_reason']='model_or_host_error'
        result.update(error_type=type(exc).__name__,error=message,V=0,normalSubmission=False)
    finally:
        pause(result['stop_reason'])
        timer.cancel()
        if guard.guard.task is not None:
            guard.finalize()
        drain()
        guard.guard.playbook.persist()
        result.update(agentWallSeconds=clock()-started,
             modelInvocationAttempts=len(model.calls),
             modelRequests=sum('http_request' in c for c in model.calls),
             completedHttpResponses=sum('http_status' in c for c in model.calls),
             usage={key:sum(c.get('usage',{}).get(key,0) for c in model.calls)
                    for key in ['prompt_tokens','completion_tokens','total_tokens']},
             unknownUsageRequests=sum(not all(key in c.get('usage',{}) for key in ['prompt_tokens','completion_tokens','total_tokens']) for c in model.calls),
             usageInterpretation='Known reported usage only; interrupted or failed requests can have missing usage',
             phaseCounts=dict(Counter(c['phase'] for c in model.calls)),
             sentryEventCounts=dict(Counter(e['type'] for e in events)),
             interventions=sum(e['type']=='rescue_injected' for e in events),
             verifiedRecoveries=sum(e['type']=='escape_result' and e.get('resolved') is True for e in events),
             hardVerifiedRecoveries=sum(e['type']=='escape_result' and e.get('resolved') is True and e.get('failure_type')=='action_validity_failure' for e in events),
             softVerifiedRecoveries=sum(e['type']=='escape_result' and e.get('resolved') is True and e.get('failure_type')!='action_validity_failure' for e in events),
             learnedEntries=len(guard.guard.playbook.entries),
             invalidModelDiagnoses=sum(e['type']=='sentry_diagnosis' and e.get('source')=='invalid_model_output' for e in events),
             repeatedCommandObservationPairs=repeated,
             repetitionDefinition='Exact same command and truncated visible observation hash previously seen within task; diagnostic proxy, not all repetitions are invalid',
             candidatePaused=paused,contextCompressionAddedByHost=False,
             externalApiCostUsd=0,localEnergyCostUsd=None)
        save(case/'agent-result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='steps'},ensure_ascii=False),flush=True)


def preflight():
    HERE.mkdir(exist_ok=True)
    paths.DOCKER_CONFIG.mkdir(parents=True,exist_ok=True)
    save(paths.DOCKER_CONFIG/'config.json',{'cliPluginsExtraDirs':[]})
    values=[]
    for task in TASKS:
        data=(TASK_ROOT/task/'task.yaml').read_bytes()
        instruction=yaml.safe_load(data)['instruction']
        image='tb__'+task+'__client:latest'
        check=docker(['image','inspect',BASE_IMAGES[task]['id']],60)
        values.append({'task':task,'image':image,'available':check['code']==0,
             'imageId':json.loads(check['stdout'])[0]['Id'] if check['code']==0 else None,
             'inspectionError':check['stderr'] if check['code'] else None,
             'instructionSha256':hashlib.sha256(instruction.encode()).hexdigest(),
             'taskYamlSha256':hashlib.sha256(data).hexdigest()})
    cfg=SentryConfig()
    report={'source':str(UPSTREAM),'tasks':values,'SentryConstants':asdict(cfg),
            'hostActor':{'temperature':0.2,'max_tokens':2048},'modelCalls':0,
            'parse_checks':{'command':parse_action('{"command":"pwd"}')=={'command':'pwd'},
                            'final':parse_action('{"final":"done"}')=={'final':'done'}}}
    save(HERE/'preflight.json',report)
    print(json.dumps(report,ensure_ascii=False))


def batch(tasks):
    for task in tasks:
        case=CASES/task
        if (case/'agent-result.json').exists():
            raise RuntimeError('Refusing repeated attempt for '+task)
        case.mkdir(parents=True,exist_ok=True)
        with (case/'worker.stdout.log').open('wb') as stdout, (case/'worker.stderr.log').open('wb') as stderr:
            process=subprocess.Popen([sys.executable,str(Path(__file__)), '--worker', '--task',task],stdout=stdout,stderr=stderr)
            try:
                process.wait(timeout=330)
            except subprocess.TimeoutExpired:
                save(case/'supervisor-pause.json',docker(['pause',CONTAINER_PREFIX+task],12))
                process.kill();process.wait(timeout=10)
                if not (case/'agent-result.json').exists():
                    save(case/'agent-result.json',{'task':task,'V':0,'A':None,'Y':None,
                         'stop_reason':'external_supervisor_timeout','workerExitCode':process.returncode})
        save(case/'worker-exit.json',{'code':process.returncode})
        # Shared helper freezes the complete writable filesystem while paused,
        # then runs unchanged hidden tests only in a separate grader container.
        for mode in ['--snapshot-only','--grade-only']:
            with (case/(mode[2:]+'.stdout.log')).open('wb') as stdout, (case/(mode[2:]+'.stderr.log')).open('wb') as stderr:
                command=[sys.executable,str(HERE.parent/'grade_original.py'),'--task',task,'--method','sentry',
                         '--namespace','sentry-normalized',
                         '--case-dir',str(case),'--agent-container',CONTAINER_PREFIX+task,mode]
                graded=subprocess.run(command,stdout=stdout,stderr=stderr,timeout=900)
            if graded.returncode:
                save(case/'grading-helper-failure.json',{'phase':mode,'code':graded.returncode})
                break
        raw=json.loads((case/'agent-result.json').read_text(encoding='utf-8'))
        grade_path=case/'grading/result.json'
        grade=json.loads(grade_path.read_text(encoding='utf-8')) if grade_path.exists() else {'valid':False,'A':None}
        result={**raw,'A':grade.get('A') if grade.get('valid') else None,
                'Y':grade['A']*raw['V'] if grade.get('valid') else None,'grading':grade}
        save(case/'result.json',result)
        print(json.dumps({'task':task,'A':result['A'],'V':result['V'],'Y':result['Y'],
                           'stop':result.get('stop_reason'),'calls':result.get('modelRequests')},ensure_ascii=False),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--preflight',action='store_true')
    ap.add_argument('--worker',action='store_true')
    ap.add_argument('--infra-retry',action='store_true')
    ap.add_argument('--task',choices=TASKS)
    ap.add_argument('--tasks',default=','.join(TASKS))
    args=ap.parse_args()
    if args.preflight:preflight()
    elif args.worker:one_task(args.task,args.infra_retry)
    else:
        selected=args.tasks.split(',')
        if any(t not in TASKS for t in selected):raise SystemExit('Unknown task')
        batch(selected)
