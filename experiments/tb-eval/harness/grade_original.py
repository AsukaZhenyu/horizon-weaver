"""Fixed-candidate original TB8 grader shared by round2 methods.

No model calls. Snapshot first; hidden tests are staged only in a new grader
container made from the committed candidate filesystem. Docker commit does not
preserve process memory, open sockets, or runtime mounts.
"""
from pathlib import Path
import argparse, hashlib, json, os, re, subprocess, tarfile, time

HERE=Path(__file__).resolve().parent
import sys
sys.path.insert(0,str(HERE))
import paths
TASKS=paths.require_tasks()
CHECKS={'hello-world':2,'fix-permissions':1,'log-summary':3,'regex-log':1,'jq-data-processing':14,'sqlite-db-truncate':1,'cprofiling-python':2,'cancel-async-tasks':6}
LEGACY=set(CHECKS)
# round4: every task in the frozen protocol is accepted; new tasks carry no
# pre-registered check count and are graded through their own run-tests.sh.
_PROTO=json.loads((paths.ROUND_DIR/'protocol.json').read_text(encoding='utf-8'))
for _t in _PROTO['tasks']:
    CHECKS.setdefault(_t,None)
PROXY=os.environ.get('TB_GRADING_PROXY','http://host.docker.internal:7890')
NET_ARGS=['--add-host','host.docker.internal:host-gateway',
          '--env','HTTP_PROXY='+PROXY,'--env','HTTPS_PROXY='+PROXY,
          '--env','http_proxy='+PROXY,'--env','https_proxy='+PROXY,
          '--env','NO_PROXY=localhost,127.0.0.1']
ENV={**os.environ,'DOCKER_CONFIG':str(paths.DOCKER_CONFIG),'DOCKER_HOST':os.environ.get('DOCKER_HOST','npipe:////./pipe/dockerDesktopLinuxEngine'),'DOCKER_CLI_HINTS':'false'}
ap=argparse.ArgumentParser()
ap.add_argument('--task',required=True,choices=CHECKS)
ap.add_argument('--case-dir',required=True)
ap.add_argument('--agent-container')
ap.add_argument('--snapshot-only',action='store_true')
ap.add_argument('--grade-only',action='store_true')
ap.add_argument('--method',default='spine',choices=['spine','sentry'])
ap.add_argument('--namespace',help='Artifact/container name namespace only; does not change task or algorithm')
ap.add_argument('--recovery-label')
ap.add_argument('--wsl-transport',action='store_true')
ap.add_argument('--python-bootstrap',action='store_true')
args=ap.parse_args()
namespace=args.namespace or args.method
if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}',namespace):raise SystemExit('Invalid artifact namespace')
case=Path(args.case_dir).resolve()
if HERE not in case.parents:raise SystemExit('case directory must stay inside this round workspace')
case.mkdir(parents=True,exist_ok=True)

def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        while b:=f.read(1024*1024):h.update(b)
    return h.hexdigest()

def save(name,obj):
    (case/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')

def command(argv,limit=90,input=None):
    t=time.monotonic()
    try:
        p=subprocess.run(docker_argv(argv),input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=ENV,timeout=limit)
        return {'code':p.returncode,'stdout':p.stdout.decode('utf-8','replace'),'stderr':p.stderr.decode('utf-8','replace'),'seconds':time.monotonic()-t,'timedOut':False}
    except subprocess.TimeoutExpired as e:
        return {'code':None,'stdout':(e.stdout or b'').decode('utf-8','replace'),'stderr':(e.stderr or b'').decode('utf-8','replace'),'seconds':time.monotonic()-t,'timedOut':True}

def require(result,label):
    if result['code']!=0:raise RuntimeError(label+': '+json.dumps(result,ensure_ascii=False))
    return result

def docker_argv(argv):
    if not args.wsl_transport:return ['docker',*argv]
    def linux_path(v):
        if re.match(r'^[A-Za-z]:[\\/]',v):return '/mnt/'+v[0].lower()+'/'+v[3:].replace('\\','/')
        return v
    return ['wsl.exe','-d','Ubuntu','--exec','env','-u','DOCKER_HOST','-u','DOCKER_CONFIG','docker',*[linux_path(v) for v in argv]]

def stream(argv,path,limit=120):
    t=time.monotonic()
    with open(path,'wb') as out:
        p=subprocess.Popen(docker_argv(argv),stdout=out,stderr=subprocess.PIPE,env=ENV)
        try:_,err=p.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            p.kill();_,err=p.communicate();return {'code':None,'timedOut':True,'stderr':err.decode('utf-8','replace')}
    return {'code':p.returncode,'timedOut':False,'stderr':err.decode('utf-8','replace'),'seconds':time.monotonic()-t,'sha256':sha(path),'bytes':path.stat().st_size}

manifest_code="""from pathlib import Path
import hashlib,json,stat,os
r=Path('/app'); records=[]
for p in sorted(r.rglob('*')):
 s=p.lstat(); d={'path':str(p.relative_to(r)),'mode':stat.S_IMODE(s.st_mode),'uid':s.st_uid,'gid':s.st_gid,'type':'symlink' if p.is_symlink() else 'dir' if p.is_dir() else 'file' if p.is_file() else 'other'}
 if p.is_symlink(): d['target']=os.readlink(p)
 elif p.is_file(): d['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
 records.append(d)
print(json.dumps(records,sort_keys=True))
"""

def candidate_tar_manifest(path):
    out=[]
    with tarfile.open(path) as tf:
        for m in tf:
            name=m.name.removeprefix('./')
            if name.rstrip('/')=='app':continue
            d={'path':name.removeprefix('app/').rstrip('/'),'mode':m.mode,'uid':m.uid,'gid':m.gid,'type':'symlink' if m.issym() else 'dir' if m.isdir() else 'file' if m.isfile() or m.islnk() else 'other'}
            if m.issym():d['target']=m.linkname
            elif m.isfile() or m.islnk():d['sha256']=hashlib.sha256(tf.extractfile(m).read()).hexdigest()
            out.append(d)
    return sorted(out,key=lambda x:x['path'])

def snapshot():
    if (case/'snapshot.json').exists():return json.loads((case/'snapshot.json').read_text(encoding='utf-8'))
    if not args.agent_container:raise RuntimeError('--agent-container required for snapshot')
    name=args.agent_container
    if not name.startswith('week8r3-'):raise RuntimeError('Refusing unexpected container prefix')
    identity=require(command(['inspect',name]),'inspect candidate')
    info=json.loads(identity['stdout'])[0];save('candidate-container-identity.json',info)
    pause={'code':0,'stdout':'already paused','stderr':''} if info['State']['Paused'] else command(['pause',name])
    save('candidate-pause.json',pause);require(pause,'pause candidate')
    archive=case/'candidate-app.tar'
    archived=stream(['cp',name+':/app','-'],archive)
    save('candidate-archive.json',archived);require(archived,'archive /app')
    manifest=candidate_tar_manifest(archive);save('candidate-app-manifest.json',manifest)
    image='horizon-weaver-week8r3-'+namespace+'-candidate/'+args.task+':fixed'
    commit=require(command(['commit','--pause=false',name,image],limit=180),'commit fixed candidate')
    save('candidate-image-commit.json',commit)
    record={'task':args.task,'method':args.method,'namespace':namespace,'initialImageId':info['Image'],'candidateImage':image,'candidateImageId':commit['stdout'].strip(),'appArchive':'candidate-app.tar','appArchiveSha256':sha(archive),'mounts':info.get('Mounts',[]),'scope':'container writable filesystem plus separate /app archive; excludes runtime mounts, process memory and sockets; read-only runtime mounts recreated for grading','pausedBeforeSnapshot':True,'newModelCalls':0,'newAgentActions':0}
    save('snapshot.json',record)
    # This stops all candidate processes, including any background writers.
    save('agent-container-cleanup.json',command(['rm','-f',name]))
    return record

def grade_generic(record):
    """Grade a non-legacy task with its own unchanged run-tests.sh.

    Deviation from the legacy path: the task's run-tests.sh may use the network
    to install its test dependencies. The candidate image, the original test
    files and the original script are unchanged, and no model call or agent
    action is added.
    """
    if args.recovery_label:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',args.recovery_label):raise RuntimeError('Invalid recovery label')
        primary=case/'grading'/'result.json'
        if primary.exists() and json.loads(primary.read_text(encoding='utf-8')).get('valid'):raise RuntimeError('Refusing recovery of an already valid primary grade')
    out=case/('grading-recovery-'+args.recovery_label if args.recovery_label else 'grading');out.mkdir(exist_ok=True)
    if (out/'result.json').exists():
        previous=json.loads((out/'result.json').read_text(encoding='utf-8'))
        if previous.get('valid'):raise RuntimeError('Refusing to replace an already valid grade')
        raise RuntimeError('An invalid grade already exists; use a new recovery directory')
    name='week8r3-'+namespace+'-grade-'+args.task
    started=time.monotonic()
    result=None
    try:
        mount_args=[]
        for m in record.get('mounts',[]):
            if not m.get('RW'):
                if m.get('Type')=='volume':mount_args+=['--mount',f"type=volume,src={m['Name']},dst={m['Destination']},readonly"]
                elif m.get('Type')=='bind':mount_args+=['--mount',f"type=bind,src={m['Source']},dst={m['Destination']},readonly"]
        require(command(['run','-d','--name',name,'--workdir','/app',*NET_ARGS,*mount_args,record['candidateImageId'],'sleep','infinity']),'create grader')
        identity=json.loads(require(command(['inspect',name]),'inspect grader')['stdout'])[0]
        if identity['Image']!=record['candidateImageId']:raise RuntimeError('Grader image identity mismatch')
        (out/'container-identity.json').write_text(json.dumps(identity,ensure_ascii=False,indent=2),encoding='utf-8')
        # The task's own run-tests.sh installs uv from astral.sh on every run.
        # That is what the harness does on a normally networked host, but this
        # machine reaches the internet only through the Docker Desktop proxy,
        # which is not injected into containers, and astral.sh answers 403 here.
        # Placing the cached uv0.7.13 binary on PATH first keeps the script
        # byte-identical: its curl|sh line fails, then `uv` still resolves.
        require(command(['cp',str(paths.GRADING_TOOLS/'uv'),name+':/usr/local/bin/uv']),'copy cached uv')
        require(command(['exec',name,'chmod','755','/usr/local/bin/uv']),'uv executable')
        require(command(['exec',name,'uv','--version']),'uv runs')
        manifest=json.loads(require(command(['exec',name,'python3','-c',manifest_code]),'candidate manifest')['stdout'])
        expected=json.loads((case/'candidate-app-manifest.json').read_text(encoding='utf-8'))
        (out/'candidate-manifest-before.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        if manifest!=expected:raise RuntimeError('Committed candidate /app differs in content or Unix metadata')
        td=TASKS/args.task
        hashes={str(p.relative_to(td)).replace('\\','/'):sha(p) for p in (td/'tests').rglob('*') if p.is_file()}
        (out/'original-test-hashes.json').write_text(json.dumps({'tests':hashes,'runTestsSha256':sha(td/'run-tests.sh')},ensure_ascii=False,indent=2),encoding='utf-8')
        require(command(['cp',str(td/'tests'),name+':/tests']),'copy original tests')
        require(command(['cp',str(td/'run-tests.sh'),name+':/tmp/original-run-tests.sh']),'copy original script')
        copied_hash_code="from pathlib import Path; import json,hashlib; r=Path('/tests'); print(json.dumps({'tests/'+str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() for p in r.rglob('*') if p.is_file()},sort_keys=True))"
        copied_hashes=json.loads(require(command(['exec',name,'python3','-c',copied_hash_code]),'hash copied tests')['stdout'])
        (out/'copied-test-hashes.json').write_text(json.dumps(copied_hashes,ensure_ascii=False,indent=2),encoding='utf-8')
        if copied_hashes!=hashes:raise RuntimeError('Actual /tests payload differs from original test sources')
        cmd=['exec','--workdir','/app','--env','TEST_DIR=/tests','--env','PATH=/usr/local/bin:/usr/bin:/bin',name,'timeout','--signal=TERM','--kill-after=10','600','bash','/tmp/original-run-tests.sh']
        (out/'executed-command.json').write_text(json.dumps(cmd,ensure_ascii=False,indent=2),encoding='utf-8')
        executed=command(cmd,limit=660)
        (out/'stdout.log').write_text(executed['stdout'],encoding='utf-8');(out/'stderr.log').write_text(executed['stderr'],encoding='utf-8')
        counts={}
        text=executed['stdout']+'\n'+executed['stderr']
        for key in ['passed','failed','error','errors','skipped']:
            matches=re.findall(r'(\d+) '+key+r'\b',text)
            if matches:counts[key]=int(matches[-1])
        ran=('passed' in counts) or ('failed' in counts)
        valid=executed['code'] in (0,1,5) and ran and not re.search(r'ERROR collecting|INTERNALERROR|No module named',text)
        A=int(executed['code']==0 and not counts.get('failed',0)) if valid else None
        result={'task':args.task,'valid':valid,'A':A,'exitCode':executed['code'],'counts':counts,'expectedChecks':None,'gradingSeconds':time.monotonic()-started,'candidateImageId':record['candidateImageId'],'candidateArchiveSha256':record['appArchiveSha256'],'candidateRestoredExactly':True,'bootstrapCandidateUnchanged':True,'testHashes':hashes,'copiedTestsHashMatched':True,'newModelCalls':0,'newAgentActions':0,'dockerTransport':'Windows Docker CLI with explicit engine','pythonBootstrap':False,'protocol':'generic round4 path: unchanged original run-tests.sh with network; no Agent rerun'}
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
    except Exception as e:
        result={'task':args.task,'valid':False,'A':None,'error':str(e),'gradingSeconds':time.monotonic()-started,'candidateImageId':record['candidateImageId'],'newModelCalls':0,'newAgentActions':0}
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
    finally:
        (out/'cleanup.json').write_text(json.dumps(command(['rm','-f',name]),indent=2),encoding='utf-8')
    return result


def grade(record):
    if args.task not in LEGACY:
        return grade_generic(record)
    if args.recovery_label:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',args.recovery_label):raise RuntimeError('Invalid recovery label')
        primary=case/'grading'/'result.json'
        if primary.exists() and json.loads(primary.read_text(encoding='utf-8')).get('valid'):raise RuntimeError('Refusing recovery of an already valid primary grade')
    out=case/('grading-recovery-'+args.recovery_label if args.recovery_label else 'grading')
    out.mkdir(exist_ok=True)
    if (out/'result.json').exists():
        previous=json.loads((out/'result.json').read_text(encoding='utf-8'))
        if previous.get('valid'):raise RuntimeError('Refusing to replace an already valid grade')
        raise RuntimeError('An invalid grade already exists; use a new recovery directory for an explicit regrade')
    name='week8r3-'+namespace+'-grade-'+args.task
    started=time.monotonic()
    result=None
    try:
        mount_args=[]
        for m in record.get('mounts',[]):
            if not m.get('RW'):
                if m.get('Type')=='volume':mount_args+=['--mount',f"type=volume,src={m['Name']},dst={m['Destination']},readonly"]
                elif m.get('Type')=='bind':mount_args+=['--mount',f"type=bind,src={m['Source']},dst={m['Destination']},readonly"]
        require(command(['run','-d','--name',name,'--workdir','/app',*mount_args,record['candidateImageId'],'sleep','infinity']),'create grader')
        identity=json.loads(require(command(['inspect',name]),'inspect grader')['stdout'])[0]
        if identity['Image']!=record['candidateImageId']:raise RuntimeError('Grader image identity mismatch')
        (out/'container-identity.json').write_text(json.dumps(identity,ensure_ascii=False,indent=2),encoding='utf-8')
        manifest=json.loads(require(command(['exec',name,'python3','-c',manifest_code]),'candidate manifest')['stdout'])
        expected=json.loads((case/'candidate-app-manifest.json').read_text(encoding='utf-8'))
        (out/'candidate-manifest-before.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        if manifest!=expected:raise RuntimeError('Committed candidate /app differs in content or Unix metadata')
        td=TASKS/args.task
        hashes={str(p.relative_to(td)).replace('\\','/'):sha(p) for p in (td/'tests').rglob('*') if p.is_file()}
        (out/'original-test-hashes.json').write_text(json.dumps({'tests':hashes,'runTestsSha256':sha(td/'run-tests.sh')},ensure_ascii=False,indent=2),encoding='utf-8')
        require(command(['cp',str(td/'tests'),name+':/tests']),'copy original tests')
        require(command(['cp',str(td/'run-tests.sh'),name+':/tmp/original-run-tests.sh']),'copy original script')
        copied_hash_code="from pathlib import Path; import json,hashlib; r=Path('/tests'); print(json.dumps({'tests/'+str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() for p in r.rglob('*') if p.is_file()},sort_keys=True))"
        copied_hashes=json.loads(require(command(['exec',name,'python3','-c',copied_hash_code]),'hash copied tests')['stdout'])
        (out/'copied-test-hashes.json').write_text(json.dumps(copied_hashes,ensure_ascii=False,indent=2),encoding='utf-8')
        if copied_hashes!=hashes:raise RuntimeError('Actual /tests payload differs from original test sources')
        require(command(['cp',str(paths.GRADING_TOOLS/'uv'),name+':/usr/local/bin/uv']),'copy cached uv')
        require(command(['exec',name,'chmod','755','/usr/local/bin/uv']),'uv executable')
        require(command(['cp',str(paths.GRADING_TOOLS/'pytest-wheels'),name+':/tmp/pytest-wheels']),'copy wheel cache')
        extra_links=[]
        if args.task in ('jq-data-processing','cprofiling-python'):
            cp313=paths.GRADING_TOOLS/'cp313'
            if not cp313.exists():raise RuntimeError('The required cp313 wheel cache is not ready')
            require(command(['cp',str(cp313),name+':/tmp/pytest-wheels-cp313']),'copy matching cp313 wheel cache')
            extra_links=['--find-links','/tmp/pytest-wheels-cp313']
        runtime=require(command(['exec',name,'sh','-c','uv --version && python3 --version']),'runtime versions')
        (out/'runtime-versions.json').write_text(json.dumps(runtime,indent=2),encoding='utf-8')
        venv_command=['exec',name,'python3','-m','venv','/tmp/week8r3-grading-venv'] if args.python_bootstrap else ['exec',name,'uv','venv','--python','python3','/tmp/week8r3-grading-venv']
        require(command(venv_command),'create grading venv')
        dependencies=['pytest==8.4.1']
        if args.task=='jq-data-processing':dependencies+=['pyyaml==6.0.2']
        if args.task=='cprofiling-python':dependencies+=['Pillow==11.2.1','imagehash==4.3.2']
        install_command=['exec',name,'/tmp/week8r3-grading-venv/bin/python','-m','pip','install','--no-index','--find-links','/tmp/pytest-wheels',*extra_links,*dependencies] if args.python_bootstrap else ['exec',name,'uv','pip','install','--python','/tmp/week8r3-grading-venv/bin/python','--no-index','--find-links','/tmp/pytest-wheels',*extra_links,*dependencies]
        installed=command(install_command,limit=90)
        (out/'dependency-install.json').write_text(json.dumps(installed,ensure_ascii=False,indent=2),encoding='utf-8')
        require(installed,'install fixed offline grading dependencies')
        freeze=command(['exec',name,'/tmp/week8r3-grading-venv/bin/python','-m','pip','freeze'] if args.python_bootstrap else ['exec',name,'uv','pip','freeze','--python','/tmp/week8r3-grading-venv/bin/python'])
        (out/'dependencies.json').write_text(json.dumps(freeze,indent=2),encoding='utf-8')
        after=json.loads(require(command(['exec',name,'python3','-c',manifest_code]),'manifest after bootstrap')['stdout'])
        if after!=expected:raise RuntimeError('Grading bootstrap changed task candidate')
        if args.task=='cancel-async-tasks':require(command(['exec',name,'cp','/tests/test.py','/app/test.py']),'stage original test fixture')
        cmd=['exec','--workdir','/app','--env','TEST_DIR=/tests','--env','VIRTUAL_ENV=/tmp/week8r3-grading-venv','--env','PATH=/tmp/week8r3-grading-venv/bin:/opt/spine/runtime:/usr/local/bin:/usr/bin:/bin','--env','LD_LIBRARY_PATH=/opt/spine/runtime/lib',name,'timeout','--signal=TERM','--kill-after=5','180','uv','run','pytest','/tests/test_outputs.py','-rA','--junitxml=/tmp/week8r3-pytest.xml']
        if args.python_bootstrap:
            cmd=cmd[:cmd.index('uv')]+['/tmp/week8r3-grading-venv/bin/python','-m','pytest','/tests/test_outputs.py','-rA','--junitxml=/tmp/week8r3-pytest.xml']
        (out/'executed-command.json').write_text(json.dumps(cmd,ensure_ascii=False,indent=2),encoding='utf-8')
        executed=command(cmd,limit=210)
        (out/'stdout.log').write_text(executed['stdout'],encoding='utf-8');(out/'stderr.log').write_text(executed['stderr'],encoding='utf-8')
        junit=command(['exec',name,'cat','/tmp/week8r3-pytest.xml'])
        (out/'junit.xml').write_text(junit['stdout'],encoding='utf-8')
        counts={}
        text=executed['stdout']+'\n'+executed['stderr']
        for key in ['passed','failed','error','errors','skipped']:
            matches=re.findall(r'(\d+) '+key+r'\b',text)
            if matches:counts[key]=int(matches[-1])
        valid=executed['code'] in (0,1) and counts.get('passed',0)+counts.get('failed',0)==CHECKS[args.task] and not counts.get('error',counts.get('errors',0)) and not re.search(r'ERROR collecting|Interrupted:|INTERNALERROR',text)
        A=int(executed['code']==0 and not counts.get('failed',0)) if valid else None
        result={'task':args.task,'valid':valid,'A':A,'exitCode':executed['code'],'counts':counts,'expectedChecks':CHECKS[args.task],'gradingSeconds':time.monotonic()-started,'candidateImageId':record['candidateImageId'],'candidateArchiveSha256':record['appArchiveSha256'],'candidateRestoredExactly':True,'bootstrapCandidateUnchanged':True,'testHashes':hashes,'copiedTestsHashMatched':True,'newModelCalls':0,'newAgentActions':0,'dockerTransport':'WSL Ubuntu Linux Docker CLI' if args.wsl_transport else 'Windows Docker CLI with explicit engine','pythonBootstrap':args.python_bootstrap,'protocol':'stdlib venv/pip from fixed offline wheels and python -m pytest original file plus JUnit; no Agent rerun' if args.python_bootstrap else 'fixed cached uv0.7.13 and original pinned dependencies; unchanged original pytest file and command plus JUnit reporting; bootstrap in /tmp outside candidate; writable filesystem restored without process state'}
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        if args.recovery_label and valid and (case/'result.json').exists():
            raw=json.loads((case/'result.json').read_text(encoding='utf-8'))
            effective={**raw,'A':A,'Y':None if raw.get('V') is None else A*raw['V'],'grading':result,'gradingRecovery':{'directory':out.name,'rawPreserved':'result.json','newModelCalls':0,'newAgentActions':0,'sameCandidateImageId':record['candidateImageId']}}
            (case/'effective-result.json').write_text(json.dumps(effective,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
    except Exception as e:
        result={'task':args.task,'valid':False,'A':None,'error':str(e),'gradingSeconds':time.monotonic()-started,'candidateImageId':record['candidateImageId'],'newModelCalls':0,'newAgentActions':0}
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
    finally:
        (out/'cleanup.json').write_text(json.dumps(command(['rm','-f',name]),indent=2),encoding='utf-8')
    return result

record=json.loads((case/'snapshot.json').read_text(encoding='utf-8')) if args.grade_only else snapshot()
if args.snapshot_only:print(json.dumps(record,ensure_ascii=False))
else:grade(record)
