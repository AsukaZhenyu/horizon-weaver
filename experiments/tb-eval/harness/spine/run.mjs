import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {mkdir,readFile,writeFile,appendFile,access,readdir} from 'node:fs/promises';
import {createWriteStream} from 'node:fs';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {paths,resolvedPaths} from './paths.mjs';
const base=dirname(fileURLToPath(import.meta.url));
const round=paths.roundDir;
const root=resolve(paths.runs,'spine','cases');
const taskRoot=paths.taskRoot;
const yaml=(await import('yaml')).default;
const tasks=JSON.parse((await readFile(resolve(round,'protocol.json'),'utf8')).replace(/^﻿/,'')).tasks;
const hash=s=>createHash('sha256').update(s).digest('hex');
const dockerEnv={...process.env,DOCKER_CONFIG:paths.dockerConfig,DOCKER_HOST:process.env.DOCKER_HOST||'npipe:////./pipe/dockerDesktopLinuxEngine',DOCKER_CLI_HINTS:'false'};
const python=paths.python;
const modelBase=(process.env.HW_MODEL_URL||'http://127.0.0.1:8080').replace(/\/$/,'');
const save=(p,v)=>writeFile(p,JSON.stringify(v,null,2));
const imageManifest=JSON.parse((await readFile(resolve(round,'base-images.json'),'utf8')).replace(/^\uFEFF/,''));
function execute(cmd,args,{limit=45000,outputFile,env=dockerEnv}={}){return new Promise((r,j)=>{
 const start=Date.now();const p=spawn(cmd,args,{env,stdio:['ignore','pipe','pipe']});let out='',err='',timedOut=false;
 const f=outputFile?createWriteStream(outputFile):null;
 p.stdout.on('data',c=>{if(f)f.write(c);else out+=c.toString();});p.stderr.on('data',c=>err+=c.toString());
 const timer=setTimeout(()=>{timedOut=true;p.kill();},limit);
 p.on('error',e=>{clearTimeout(timer);j(e);});p.on('exit',async code=>{clearTimeout(timer);if(f)await new Promise(x=>f.end(x));r({code,stdout:out,stderr:err,timedOut,seconds:(Date.now()-start)/1000});});
});}
const docker=(args,opts)=>execute('docker',args,opts);
function requireOk(v,msg){if(v.code!==0)throw new Error(msg+': '+JSON.stringify(v));return v;}
async function hashes(td){const files={};async function visit(p){for(const e of await readdir(p,{withFileTypes:true})){const f=resolve(p,e.name);if(e.isDirectory())await visit(f);else if(e.isFile())files[f.slice(td.length+1).replaceAll('\\','/')]=hash(await readFile(f));}}await visit(resolve(td,'tests'));files['run-tests.sh']=hash(await readFile(resolve(td,'run-tests.sh')));return files;}
await mkdir(root,{recursive:true});
const preflight={at:new Date().toISOString(),modelCalls:0,protocolSha256:hash(await readFile(resolve(round,'protocol.json'))),tasks:[]};
for(const id of tasks){const td=resolve(taskRoot,id);const y=await readFile(resolve(td,'task.yaml'),'utf8');const pinned=imageManifest.find(x=>x.task===id);if(!pinned)throw new Error('Missing pinned image ID');const info=JSON.parse(requireOk(await docker(['image','inspect',pinned.id]),'image ID preflight')['stdout'])[0];if(info.Id!==pinned.id)throw new Error('Image ID mismatch');preflight.tasks.push({task:id,image:pinned.id,listedTag:pinned.tag,imageId:info.Id,taskYamlSha256:hash(y),testHashes:await hashes(td),instruction:yaml.parse(y).instruction});}
preflight.runtimeVolume=requireOk(await docker(['volume','inspect',paths.spineVolume]),'runtime volume');
await save(resolve(base,'preflight.json'),preflight);
if(process.argv.includes('--preflight')){console.log(JSON.stringify({preflight:true,modelCalls:0,tasks:preflight.tasks.map(({task,imageId})=>({task,imageId}))}));process.exit(0);}
const selectedIndex=process.argv.indexOf('--tasks');
const selected=selectedIndex<0?tasks:process.argv[selectedIndex+1].split(',');
if(selected.some(x=>!tasks.includes(x)))throw new Error('Unknown task');
const recoveryIndex=process.argv.indexOf('--recovery-task');
const recoveryTask=recoveryIndex<0?null:process.argv[recoveryIndex+1];
if(recoveryTask&&!tasks.includes(recoveryTask))throw new Error('Unknown recovery task');
const recoveryLabelIndex=process.argv.indexOf('--recovery-label');
const recoveryLabel=recoveryLabelIndex<0?'execution-recovery':process.argv[recoveryLabelIndex+1];
for(const id of selected){
 const spec=preflight.tasks.find(x=>x.task===id);const caseName=id===recoveryTask?id+'-'+recoveryLabel:id;const dir=resolve(root,caseName);await mkdir(dir,{recursive:true});
 try{await access(resolve(dir,'agent-started.json'));throw new Error('Refusing to repeat an Agent attempt: '+caseName);}catch(e){if(e.code!=='ENOENT')throw e;}
 if(id===recoveryTask){const original=resolve(root,id);let priorRequests=0,priorResponses=0,priorActions=0;
  try{for(const line of (await readFile(resolve(original,'http-trace.jsonl'),'utf8')).split(/\r?\n/)){if(!line)continue;const r=JSON.parse(line);if(r.kind==='request')priorRequests++;if(r.kind==='response')priorResponses++;}}catch{}
  try{for(const line of (await readFile(resolve(original,'events.jsonl'),'utf8')).split(/\r?\n/)){if(!line)continue;const r=JSON.parse(line);if(r.type==='tool_execution_start')priorActions++;}}catch{}
  if(priorResponses>0||priorActions>0)throw new Error('Recovery requires zero completed model responses and zero executed actions; found '+priorResponses+' responses and '+priorActions+' actions');
  await save(resolve(original,'execution-recovery.json'),{caseDir:dir,sourceCase:original,reason:'External low-memory kill before any completed model response or executed action; original partial evidence retained',priorRequests,priorResponses,priorActions,recoveryLabel});
  await save(resolve(dir,'infrastructure-recovery-source.json'),{sourceCase:original,priorRequests,priorResponses,priorActions,reason:'external interruption by harness memory pressure reaper; same pinned image and protocol',recoveryLabel});}
 const name='week8r3-spine-'+caseName;let proxy;let agentResult;
 const caseStart=Date.now();
 try{
  await save(resolve(dir,'source.json'),{...spec,benchmarkCommit:'d28711d0da2675d0bb1d56de45ae5df6082438a3',spineCommit:'b599498be489d31b9356d77af678bfd3c7220fc0',pi:'1.1.0',plugin:'0.1.3',scope:'round4 stratified 24-task development set: 8 historical + 16 new, fresh single attempt',model:'qwen3.5-9b',context:16384,temperature:0.2,maxOutputTokens:2048,agentBudgetSeconds:300,paths:resolvedPaths});
  const started=await docker(['run','-d','--name',name,'--mount','type=volume,src='+paths.spineVolume+',dst=/opt/spine,readonly','--workdir','/app',spec.image,'sleep','infinity']);await save(resolve(dir,'container-start.json'),started);
  if(started.code!==0){const probe=await docker(['inspect',name]);await save(resolve(dir,'start-timeout-inspection.json'),probe);if(probe.code!==0||JSON.parse(probe.stdout)[0].Image!==spec.imageId)throw new Error('Container startup failed');}
  const identity=JSON.parse(requireOk(await docker(['inspect',name]),'runtime identity').stdout)[0];await save(resolve(dir,'initial-container-identity.json'),identity);if(identity.Image!==spec.imageId)throw new Error('Initial image changed');
  await save(resolve(dir,'initial-app-archive.json'),await docker(['cp',name+':/app','-'],{outputFile:resolve(dir,'initial-app.tar'),limit:90000}));
  let requestCount=0,upstreamCount=0,acceptRequests=true;const responseRecords=[];const wire=resolve(dir,'http-trace.jsonl');await writeFile(wire,'');
  proxy=createServer(async(req,res)=>{
   const index=++requestCount,t=Date.now();const chunks=[];for await(const c of req)chunks.push(c);const raw=Buffer.concat(chunks).toString();await appendFile(wire,JSON.stringify({kind:'request',index,time:new Date().toISOString(),path:req.url,body:JSON.parse(raw||'null')})+'\n');
   if(!acceptRequests){res.writeHead(409,{'content-type':'application/json'});res.end(JSON.stringify({error:{message:'evaluation deadline reached'}}));await appendFile(wire,JSON.stringify({kind:'deadline-rejection',index,forwardedToModel:false})+'\n');return;}
   upstreamCount++;
   const cancel=new AbortController();res.on('close',()=>{if(!res.writableEnded)cancel.abort();});const pieces=[];
   try{const u=await fetch(modelBase+req.url,{method:req.method,headers:{'content-type':'application/json'},body:req.method==='GET'?undefined:raw,signal:cancel.signal});res.writeHead(u.status,{'content-type':u.headers.get('content-type')||'application/json'});for await(const c of u.body){pieces.push(Buffer.from(c));res.write(c);}res.end();const record={kind:'response',index,status:u.status,seconds:(Date.now()-t)/1000,body:Buffer.concat(pieces).toString()};responseRecords.push(record);await appendFile(wire,JSON.stringify(record)+'\n');}
   catch(e){if(!res.headersSent)res.writeHead(502);if(!res.writableEnded)res.end(String(e));await appendFile(wire,JSON.stringify({kind:'error',index,error:String(e),partialBody:Buffer.concat(pieces).toString()})+'\n');}
  });await new Promise(r=>proxy.listen(0,'127.0.0.1',r));
  const cfg=resolve(dir,'config');await mkdir(cfg,{recursive:true});
  const models={providers:{local:{baseUrl:'http://host.docker.internal:'+proxy.address().port+'/v1',api:'openai-completions',apiKey:'local-only',models:[{id:'qwen3.5-9b',reasoning:false,input:['text'],contextWindow:16384,maxTokens:2048,cost:{input:0,output:0,cacheRead:0,cacheWrite:0},samplingParams:{temperature:0.2},compat:{supportsStore:false,supportsDeveloperRole:false,supportsReasoningEffort:false}}]}}};
  await save(resolve(cfg,'models.json'),models);await save(resolve(cfg,'settings.json'),{defaultProvider:'local',defaultModel:'qwen3.5-9b',defaultThinkingLevel:'off',defaultProjectTrust:'never',cacheWarming:'off',compaction:{enabled:true,reserveTokens:2048,keepRecentTokens:4096},retry:{enabled:false}});
  requireOk(await docker(['exec',name,'mkdir','-p','/tmp/week8r3-pi-agent']),'config directory');requireOk(await docker(['cp',cfg+'/.',name+':/tmp/week8r3-pi-agent/']),'copy config');
  const argv=['exec','--workdir','/app','--env','PI_CODING_AGENT_DIR=/tmp/week8r3-pi-agent','--env','PI_OFFLINE=1','--env','LD_LIBRARY_PATH=/opt/spine/runtime/lib','--env','PATH=/opt/spine/runtime:/usr/local/bin:/usr/bin:/bin','--env','HTTP_PROXY=','--env','HTTPS_PROXY=','--env','ALL_PROXY=','--env','NO_PROXY=host.docker.internal,127.0.0.1,localhost',name,'/opt/spine/runtime/node','/opt/spine/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js','--mode','json','--offline','--provider','local','--model','qwen3.5-9b','--thinking','off','--no-extensions','--extension','/opt/spine/packages/plugin/dist/pi/extension.js','--no-mcp','--no-skills','--no-prompt-templates','--no-context-files','--no-approve','--session-dir','/tmp/week8r3-pi-sessions',spec.instruction];
  await save(resolve(dir,'run-config.json'),{argv,models,taskSpecificHintsAdded:false,hiddenTestsVisibleDuringAgent:false});
  await save(resolve(dir,'agent-started.json'),{at:new Date().toISOString(),attempt:1});
  const t=Date.now();const out=createWriteStream(resolve(dir,'events.jsonl'));const err=createWriteStream(resolve(dir,'stderr.log'));
  const agent=spawn('docker',argv,{env:dockerEnv,stdio:['ignore','pipe','pipe']});agent.stdout.pipe(out);agent.stderr.pipe(err);let deadlineReached=false,deadlinePausePromise=Promise.resolve(null),freezeElapsedSeconds=null;
  const timer=setTimeout(()=>{deadlineReached=true;acceptRequests=false;proxy?.closeAllConnections();deadlinePausePromise=(async()=>{const paused=await docker(['pause',name]);freezeElapsedSeconds=(Date.now()-t)/1000;await save(resolve(dir,'deadline-pause.json'),{...paused,freezeElapsedSeconds,overrunSeconds:Math.max(0,freezeElapsedSeconds-300),newModelRequestsRejectedAfterDeadline:true});agent.kill();return paused;})();},300000);
  const code=await new Promise((r,j)=>{agent.on('exit',r);agent.on('error',j);});clearTimeout(timer);
  if(deadlineReached)await deadlinePausePromise;
  else {acceptRequests=false;await save(resolve(dir,'normal-stop-pause.json'),await docker(['pause',name]));freezeElapsedSeconds=(Date.now()-t)/1000;}
  await Promise.all([new Promise(r=>out.end(r)),new Promise(r=>err.end(r))]);const agentWallSeconds=(Date.now()-t)/1000;
  proxy.closeAllConnections();await new Promise(r=>proxy.close(r));proxy=null;
  const events=(await readFile(resolve(dir,'events.jsonl'),'utf8')).split(/\r?\n/).flatMap(l=>{try{return [JSON.parse(l)];}catch{return [];}});
  const messages=events.filter(e=>e.type==='message_end'&&e.message?.role==='assistant').map(e=>e.message);const final=messages.at(-1);
  const toolCalls=events.filter(e=>e.type==='tool_execution_start');const toolCounts=Object.fromEntries([...new Set(toolCalls.map(x=>x.toolName))].map(k=>[k,toolCalls.filter(x=>x.toolName===k).length]));
  const normalizedCommands=toolCalls.filter(x=>x.toolName==='bash').map(x=>String(x.args?.command||'').trim()).filter(Boolean);const repeatedExactCommands=normalizedCommands.length-new Set(normalizedCommands).size;
  const V=Number(code===0&&!deadlineReached&&events.some(e=>e.type==='agent_settled'&&!e.aborted)&&final?.stopReason==='stop');
  agentResult={task:id,exitCode:code,deadlineReached,freezeElapsedSeconds,deadlineFreezeOverrunSeconds:deadlineReached?Math.max(0,freezeElapsedSeconds-300):0,V,A:null,Y:null,agentWallSeconds,setupSeconds:(t-caseStart)/1000,modelRequests:upstreamCount,apiAttempts:requestCount,completedHttpResponses:responseRecords.length,httpStatuses:responseRecords.map(x=>x.status),toolCounts,toolErrors:events.filter(e=>e.type==='tool_execution_end'&&(e.isError||e.result?.isError)).length,exactBashCommands:normalizedCommands.length,repeatedExactBashCommands:repeatedExactCommands,repeatMeaning:'exact proposed Bash strings only; does not establish semantic waste',finalAnswer:final?.content?.filter(x=>x.type==='text').map(x=>x.text).join('\n')||'',finalStopReason:final?.stopReason||null};
  await save(resolve(dir,'agent-result.json'),agentResult);
  await save(resolve(dir,'session-archive.json'),await docker(['cp',name+':/tmp/week8r3-pi-sessions','-'],{outputFile:resolve(dir,'sessions.tar'),limit:60000}));
  const captured=await execute(python,[resolve(round,'grade_original.py'),'--task',id,'--method','spine','--case-dir',dir,'--agent-container',name,'--snapshot-only'],{limit:240000});await save(resolve(dir,'snapshot-helper.json'),captured);requireOk(captured,'snapshot helper');
  const graded=await execute(python,[resolve(round,'grade_original.py'),'--task',id,'--method','spine','--case-dir',dir,'--grade-only'],{limit:960000});await save(resolve(dir,'grading-helper.json'),graded);
  let result;try{result=JSON.parse(await readFile(resolve(dir,'grading/result.json'),'utf8'));}catch{result={valid:false,A:null,error:'grader did not produce result'};}
  const combined={...agentResult,A:result.A,Y:result.A===null?null:result.A*V,grading:result,fullTrialSeconds:(Date.now()-caseStart)/1000};await save(resolve(dir,'result.json'),combined);console.log(JSON.stringify(combined));
 }catch(e){await save(resolve(dir,'infrastructure-failure.json'),{error:String(e),agentResult:agentResult||null,at:new Date().toISOString()});console.log(JSON.stringify({task:id,error:String(e),modelAttemptStarted:!!agentResult}));}
 finally{if(proxy){proxy.closeAllConnections();await new Promise(r=>proxy.close(r));}await save(resolve(dir,'cleanup.json'),await docker(['rm','-f',name]));}
}
