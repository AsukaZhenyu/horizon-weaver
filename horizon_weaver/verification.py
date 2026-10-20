"""Reject recognizable observations offered as executable self-tests.

This is a conservative preflight, not a proof that an admitted test is correct.
Unknown shell commands and test scripts still require the independent grader.
"""
from __future__ import annotations

import ast
import base64
import json
import hashlib
import csv
import io
import re
import shlex
import sys
import time

from .core import (Action, Check, CheckSpec, ControlReceipt, GraphView, StateDelta, StateSnapshot,
                   TaskContract, ToolResult, VerificationReceipt, canonical, digest,
                   json_equal, parse_json, requirement_sources)
from .core import ActionKind, Verdict, current_verdict, GoalCondition


def observation_only(command: str) -> str | None:
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None  # The actual shell reports malformed commands.
    if not words:
        return "An empty command has no behavioral assertion."
    # Counting/printing pipelines also return zero without comparing the
    # candidate. Recognize only chains made entirely of known observations;
    # leave actual predicates and unknown programs to their execution/grader.
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
        lexer.whitespace_split = True
        groups: list[list[str]] = [[]]
        for word in lexer:
            if word in {"|", "&&", ";"}:
                groups.append([])
            elif word in {"||", "&", "(", ")", "<", ">", "<<", ">>"}:
                groups = []
                break
            else:
                groups[-1].append(word)
    except ValueError:
        groups = []
    def reports_values(group: list[str]) -> bool:
        if not group:
            return False
        name = group[0].rsplit("/", 1)[-1]
        if name in {"echo", "true", ":", "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "ls", "pwd", "file"}:
            return True
        if name == "grep":
            flags = [word for word in group[1:] if word.startswith("-")]
            return any(word == "--count" or (word.startswith("-") and not word.startswith("--") and "c" in word)
                       for word in flags) and not any(word == "--quiet" or "q" in word[1:] for word in flags)
        if name == "awk":
            return not any(re.search(r"\bexit\b", word) for word in group[1:])
        return False
    if len(groups) > 1 and all(reports_values(group) for group in groups):
        return "This pipeline counts or prints values but never compares the candidate with the expected result."
    # POSIX splitting treats an unquoted Windows path's backslashes as escapes.
    # Restore only that executable token; keep the normal argument parser.
    raw_first = shlex.split(command, posix=False)[0].strip("\"'")
    executable_word = raw_first if re.match(r"^[A-Za-z]:[\\/]", raw_first) else words[0]
    executable = executable_word.replace("\\", "/").rsplit("/", 1)[-1]
    if executable in {"true", ":", "echo"} and not re.search(r"[|&;<>]", command):
        return "A constant success or printed message does not test behavior."
    # Only diagnose a single observation. Pipelines may include a real predicate.
    if executable in {"ls", "pwd", "cat", "head", "tail", "wc", "file"} and not re.search(r"[|&;<>]", command):
        return "Displaying files or metadata does not compare actual and expected behavior."
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable) or "-c" not in words:
        return None
    index = words.index("-c")
    if index + 1 >= len(words) or any(re.search(r"[|&;<>]", word) for word in words[index + 2:]):
        return None
    try:
        tree = ast.parse(words[index + 1])
    except SyntaxError:
        return None
    nodes = tuple(ast.walk(tree))
    if any(isinstance(node, (ast.Assert, ast.Raise)) for node in nodes):
        return None
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name.startswith("assert") or name in {"check_call", "check_output"}:
            return None
        if name in {"exit", "quit"} and node.args:
            if not isinstance(node.args[0], ast.Constant) or node.args[0].value != 0:
                return None
        if name == "run" and any(key.arg == "check" and isinstance(key.value, ast.Constant)
                                  and key.value.value is True for key in node.keywords):
            return None
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
           and node.func.id == "print" for node in nodes):
        return "This inline Python check only reports values and has no failure assertion."
    return None


# Runs in the target environment. All code and operands are base64 encoded before
# shell transport; policy prints are captured separately from the result protocol.
PYTHON_CHECK_RUNNER = r'''
import ast, base64, contextlib, io, json, math, operator, os, shutil, subprocess, sys, tempfile, traceback
from pathlib import Path
request = json.loads(base64.b64decode(sys.argv[1])) if len(sys.argv) > 1 else json.load(sys.stdin)
root = Path.cwd().resolve()
checks = []
invocations = []
class Tail(io.TextIOBase):
    def __init__(self): self.value = ''
    def write(self, text):
        self.value = (self.value + text)[-12000:]
        return len(text)
log = Tail()
class FailedAssertion(AssertionError): pass
def observed(value, depth=0):
    # Do not invoke user-defined repr/iteration just to log a comparison.
    if type(value) in (str, int, float, bool, type(None)):
        if type(value) is float and not math.isfinite(value): return str(value)
        if type(value) is int and value.bit_length() > 256: return {'type': 'int', 'bits': value.bit_length()}
        return value if type(value) is not str or len(value) <= 1500 else value[:750] + '[truncated]' + value[-750:]
    if depth < 2 and type(value) in (list, tuple) and len(value) <= 32:
        return [observed(item, depth+1) for item in value]
    if depth < 2 and type(value) is dict and len(value) <= 16 and all(type(key) is str for key in value):
        return {key:observed(item,depth+1) for key,item in value.items()}
    return {'type': type(value).__name__}
def audit(event, args):
    if event == 'subprocess.Popen' and len(invocations) < 16:
        invocations.append(dict(executable=observed(args[0]), argv=observed(args[1]), cwd=observed(args[2] or os.getcwd())))
sys.addaudithook(audit)
def record(condition, message, expression, operands=None):
    passed = bool(condition)
    checks.append(dict(expression=expression, passed=passed, source='assert', **(operands or {})))
    if not passed:
        raise FailedAssertion(str(message()) if message else expression)
def guard(condition, expression, operands=None):
    failed = bool(condition)
    checks.append(dict(expression='not (' + expression + ')', passed=not failed, source='failure_guard', **(operands or {})))
    return failed
def comparison(left, right, operation, expression, message=None, failure_guard=False):
    functions = dict(Eq=operator.eq, NotEq=operator.ne, Lt=operator.lt, LtE=operator.le,
        Gt=operator.gt, GtE=operator.ge, Is=operator.is_, IsNot=operator.is_not,
        In=lambda a,b: a in b, NotIn=lambda a,b: a not in b)
    value = functions[operation](left, right)
    operands = dict(left=observed(left), right=observed(right), operator=operation)
    if type(left) is str and type(right) is str:
        operands['text_shape'] = dict(actual_length=len(left), expected_length=len(right),
            actual_newlines=left.count('\n'), expected_newlines=right.count('\n'),
            actual_literal_backslash_n=left.count('\\n'), expected_literal_backslash_n=right.count('\\n'))
    if failure_guard: return guard(value, expression, operands)
    return record(value, message, expression, operands)
class Instrument(ast.NodeTransformer):
    def __init__(self, tree):
        self.sys_names = {'sys'}
        self.exit_names = {'exit', 'quit'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.sys_names.update(alias.asname or alias.name for alias in node.names if alias.name == 'sys')
            if isinstance(node, ast.ImportFrom) and node.module == 'sys':
                self.exit_names.update(alias.asname or alias.name for alias in node.names if alias.name == 'exit')
    def failure(self, node):
        value = node.value if isinstance(node, ast.Expr) else None
        if isinstance(value, ast.Call) and value.args:
            function = value.func
            exit_call = (isinstance(function, ast.Name) and function.id in self.exit_names
                or isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
                and function.value.id in self.sys_names and function.attr == 'exit')
            argument = value.args[0]
            if exit_call and isinstance(argument, ast.Constant) and isinstance(argument.value, int) and argument.value != 0:
                return True
        if isinstance(node, ast.Raise):
            exception = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            return isinstance(exception, ast.Name) and exception.id == 'AssertionError'
        return isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and node.test.value is False
    def visit_If(self, node):
        explicit_failure = bool(node.body) and self.failure(node.body[-1]) and not isinstance(node.test, ast.Constant)
        expression = ast.unparse(node.test)
        node = self.generic_visit(node)
        if explicit_failure:
            if isinstance(node.test, ast.Compare) and len(node.test.ops) == 1:
                node.test = ast.copy_location(ast.Call(func=ast.Name(id='__hw_compare', ctx=ast.Load()),
                    args=[node.test.left, node.test.comparators[0], ast.Constant(type(node.test.ops[0]).__name__),
                          ast.Constant(expression), ast.Constant(None), ast.Constant(True)], keywords=[]), node.test)
            else:
                node.test = ast.copy_location(ast.Call(func=ast.Name(id='__hw_guard', ctx=ast.Load()),
                    args=[node.test, ast.Constant(expression)], keywords=[]), node.test)
        return node
    def visit_Assert(self, node):
        message = ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
            kw_defaults=[], defaults=[]), body=node.msg) if node.msg else ast.Constant(None)
        if isinstance(node.test, ast.Compare) and len(node.test.ops) == 1:
            args = [node.test.left, node.test.comparators[0], ast.Constant(type(node.test.ops[0]).__name__),
                    ast.Constant(ast.unparse(node.test)), message]
            name = '__hw_compare'
        else:
            args, name = [node.test, message, ast.Constant(ast.unparse(node.test))], '__hw_record'
        return ast.copy_location(ast.Expr(ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=args, keywords=[])), node)
def run():
    if request.get('kind') == 'process':
        try:
            result = subprocess.run(request['argv'], input=request.get('stdin', ''),
                capture_output=True, text=True, encoding='utf-8', errors='replace')
        except (FileNotFoundError, PermissionError) as error:
            checks.append(dict(expression='process invocation completed', passed=False, source='process',
                               error=type(error).__name__ + ': ' + str(error)))
            return dict(status='fail', error=checks[-1]['error'])
        values = [('exit_code', result.returncode, request.get('expected_exit_code', 0))]
        for name, actual in [('stdout', result.stdout), ('stderr', result.stderr)]:
            if request.get('expected_' + name) is not None:
                values.append((name, actual, request['expected_' + name]))
        for name, actual, expected in values:
            checks.append(dict(expression=name + ' == expected', passed=actual == expected,
                               source='process', actual=observed(actual), expected=observed(expected)))
            if type(actual) is str and type(expected) is str:
                checks[-1]['text_shape'] = dict(actual_length=len(actual), expected_length=len(expected),
                    actual_newlines=actual.count('\n'), expected_newlines=expected.count('\n'),
                    actual_literal_backslash_n=actual.count('\\n'), expected_literal_backslash_n=expected.count('\\n'))
        return dict(status='pass' if all(item['passed'] for item in checks) else 'fail', argv=request['argv'],
                    stdout=observed(result.stdout), stderr=observed(result.stderr), exit_code=result.returncode)
    tree = ast.parse(request['code'])
    tree = ast.fix_missing_locations(Instrument(tree).visit(tree))
    namespace = {'__name__': '__main__', '__hw_record': record, '__hw_guard': guard, '__hw_compare': comparison}
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        exec(compile(tree, '<policy-check>', 'exec'), namespace)
    if not checks:
        return dict(status='inconclusive', error='no_executed_assertions')
    # A caught assertion still remains a recorded failure.
    return dict(status='pass' if all(item['passed'] for item in checks) else 'fail')
try:
    with tempfile.TemporaryDirectory(prefix='hw-verify-') as scratch:
        overrides = request.get('overrides', {})
        if overrides:
            clone = Path(scratch) / 'candidate'
            clone.mkdir()
            total = 0
            for allowed in request['allowed_paths']:
                source = (root / allowed).resolve()
                if not source.is_relative_to(root): raise ValueError('allowed path escapes workspace')
                for file in source.rglob('*'):
                    relative = file.relative_to(root)
                    if '.git' in relative.parts: continue
                    if file.is_symlink(): raise ValueError('control copies do not follow symlinks')
                    destination = clone / relative
                    if file.is_dir(): destination.mkdir(parents=True, exist_ok=True)
                    elif file.is_file():
                        total += file.stat().st_size
                        if total > 64 * 1024 * 1024: raise ValueError('control copy exceeds 64 MiB')
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file, destination)
            for relative, content in overrides.items():
                destination = (clone / relative).resolve()
                if not destination.is_relative_to(clone): raise ValueError('control path escapes copy')
                if not any(destination.is_relative_to((clone / p).resolve()) for p in request['allowed_paths']):
                    raise ValueError('control path outside allowed paths')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding='utf-8')
            os.chdir(clone)
        sys.path.insert(0, str(Path.cwd()))
        try:
            value = run()
        finally:
            # Windows cannot remove the process's current directory. Restore
            # cwd before the temporary-copy context handles any assertion error.
            os.chdir(root)
except FailedAssertion as error:
    value = dict(status='fail', error=str(error))
except BaseException as error:
    value = dict(status='fail' if any(not item['passed'] for item in checks) else 'inconclusive',
                 error=type(error).__name__ + ': ' + str(error),
                 traceback=[dict(file=frame.filename, line=frame.lineno, function=frame.name)
                            for frame in traceback.extract_tb(error.__traceback__)[-8:]])
finally:
    os.chdir(root)
value.update(assertions=len(checks), checks=checks[-64:], output=log.value, invocations=invocations)
print('HW_CHECK_RESULT:' + base64.b64encode(json.dumps(value).encode()).decode(), flush=True)
'''


def execute_python_check(harness, spec: CheckSpec, timeout_s: float, overrides=None):
    payload = canonical(dict(code=spec.code, kind=spec.kind, argv=spec.argv, stdin=spec.stdin,
        expected_exit_code=spec.expected_exit_code, expected_stdout=spec.expected_stdout,
        expected_stderr=spec.expected_stderr, overrides=overrides or {}, allowed_paths=harness.task.allowed_paths))
    result = harness.verify_python(PYTHON_CHECK_RUNNER, payload, timeout_s)
    if result.outcome_unknown or result.error_type or result.exit_code != 0:
        return result, 'inconclusive', 0
    try:
        frames = [line[len('HW_CHECK_RESULT:'):] for line in result.output.splitlines() if line.startswith('HW_CHECK_RESULT:')]
        if frames:
            if len(frames) != 1:
                raise ValueError('ambiguous instrumented result frames')
            value = parse_json(base64.b64decode(frames[0], validate=True).decode('utf-8'))
            noise = '\n'.join(line for line in result.output.splitlines() if not line.startswith('HW_CHECK_RESULT:'))
            if noise:
                value['protocol_noise'] = noise[-2000:]
        else:
            value = parse_json(result.output)  # Compatibility with recorded/custom harness results.
        status, assertions = value['status'], value['assertions']
        if status not in ('pass', 'fail', 'inconclusive') or type(assertions) is not int or assertions < 0:
            raise ValueError('invalid instrumented check result')
        if status in ('pass', 'fail') and not assertions:
            raise ValueError('check did not reach an assertion')
        output = canonical(value)
        return result.model_copy(update=dict(output=output, exit_code=0 if status == 'pass' else 1,
            error_type='check_inconclusive' if status == 'inconclusive' else None)), status, assertions
    except (ValueError, TypeError, KeyError) as error:
        return result.model_copy(update=dict(exit_code=1, error_type='check_protocol_error',
            output=str(error) + '\n' + result.output[-2000:])), 'inconclusive', 0


def execute_check(harness, spec: CheckSpec, timeout_s: float, overrides=None):
    if spec.kind in ('python_assertions', 'process'):
        return execute_python_check(harness, spec, timeout_s, overrides)
    if spec.kind == 'command':
        result = harness.execute(Action(kind='command', command=spec.command), timeout_s)
        status = 'pass' if result.exit_code == 0 and not result.error_type and not result.outcome_unknown else 'inconclusive'
        return result, status, 0
    result = harness.execute(Action(kind='read', path=spec.path), timeout_s)
    if result.exit_code != 0 or result.error_type or result.outcome_unknown:
        return result, 'inconclusive', 0
    actual = (overrides or {}).get(spec.path, result.output)
    try:
        matched = (actual == spec.expected if spec.kind == 'text_equals'
                   else json_equal(parse_json(actual), parse_json(spec.expected)))
    except ValueError as error:
        # Invalid candidate JSON violates a JSON equality check, rather than
        # preventing the runtime-owned comparison from executing.
        matched = False
    result = result.model_copy(update=dict(exit_code=0 if matched else 1,
        output=canonical(dict(actual=actual[:4000], expected=spec.expected[:4000], matched=matched,
            operands_complete=len(actual) <= 4000 and len(spec.expected) <= 4000,
            text_shape=text_shape(actual, spec.expected)))))
    return result, 'pass' if matched else 'fail', 1


def validate_specs(specs, task: TaskContract):
    sources = requirement_sources(task)
    for spec in specs:
        diagnostics = check_goal_diagnostics(spec, task.goal_conditions)
        if diagnostics:
            raise ValueError(f'{spec.id}: ' + ' '.join(row['gap'] for row in diagnostics))
        if any(ref not in sources for ref in spec.target_refs):
            raise ValueError(f'{spec.id}: unknown public target reference')
        for path in (spec.path, *(path for control in spec.controls for path in control.overrides)):
            if path and not any(path == allowed or allowed == '.' or path.startswith(allowed + '/')
                                for allowed in task.allowed_paths):
                raise ValueError(f'{spec.id}: check path outside allowed_paths')


def verification_environment(environment: dict, attempts=()) -> str:
    # Commands can change installed packages or other state outside a file
    # snapshot. Conservatively invalidate reuse after any executed shell action.
    epoch = sum(attempt.action.kind.value == 'command' and attempt.tool_executed is not False for attempt in attempts)
    return digest(canonical(dict(environment=environment, command_epoch=epoch)))


def receipt_is_current(receipt, snapshot, environment_hash=None, measured_snapshot=None):
    if not receipt.workspace_unchanged:
        return False
    if receipt.spec.kind in ('text_equals', 'json_equals') and measured_snapshot is not None:
        file = measured_snapshot.file(receipt.spec.path)
        return file is not None and file == snapshot.file(receipt.spec.path)
    return (receipt.snapshot_hash == snapshot.fingerprint
            and (environment_hash is None or receipt.environment_hash == environment_hash))


def reusable_comparisons(attempts, specs, snapshot: StateSnapshot, environment_hash: str) -> bool:
    if not specs or any(spec.kind not in ('text_equals', 'json_equals') for spec in specs):
        return False
    if not verification_state(attempts, snapshot, environment_hash)['ready']:
        return False
    latest = {receipt.spec.id: (receipt, attempt.before) for attempt in attempts for receipt in attempt.verification}
    return all(spec.id in latest and latest[spec.id][0].spec == spec
        and latest[spec.id][0].status == 'pass'
        and receipt_is_current(latest[spec.id][0], snapshot, environment_hash, latest[spec.id][1]) for spec in specs)


def recorded_comparisons(attempts, specs, snapshot: StateSnapshot, environment_hash: str):
    """Reuse only runtime-owned file comparisons, including measured failures.

    Python/process/shell checks can observe untracked process state, so execute
    them on every request. A reused result retains its original source step.
    """
    if not specs or any(spec.kind not in ('text_equals', 'json_equals') for spec in specs):
        return ()
    latest = {receipt.spec.id: (attempt.step, receipt, attempt.before)
              for attempt in attempts for receipt in attempt.verification}
    result = []
    for spec in specs:
        prior = latest.get(spec.id)
        if (prior is None or prior[1].spec != spec or prior[1].status not in ('pass', 'fail')
                or prior[1].result.outcome_unknown
                or not receipt_is_current(prior[1], snapshot, environment_hash, prior[2])):
            return ()
        result.append(prior[:2])
    return tuple(result)


def resolve_check_ids(attempts, identities):
    latest = {receipt.spec.id: receipt.spec for attempt in attempts for receipt in attempt.verification}
    missing = [identity for identity in identities if identity not in latest]
    if missing:
        raise ValueError('Unknown saved check IDs: ' + ', '.join(missing))
    return tuple(latest[identity] for identity in identities)


def run_checks(harness, specs, task: TaskContract, snapshot: StateSnapshot, deadline: float,
               timeout_s: float, environment_hash: str | None = None) -> tuple[VerificationReceipt, ...]:
    validate_specs(specs, task)
    receipts = []
    environment_hash = environment_hash or verification_environment(harness.describe())
    for spec in specs:
        available = min(timeout_s, deadline - time.monotonic())
        if available <= 0:
            result, status, assertions = ToolResult(exit_code=1, error_type='timeout', output='verification budget exhausted'), 'inconclusive', 0
        else:
            result, status, assertions = execute_check(harness, spec, available)
        controls = []
        for control in spec.controls:
            available = min(timeout_s, deadline - time.monotonic())
            if available <= 0 or result.outcome_unknown:
                break
            observed, control_status, count = execute_check(harness, spec, available, control.overrides)
            controls.append(ControlReceipt(name=control.name, result=observed, status=control_status, assertions=count))
            if observed.outcome_unknown:
                result = result.model_copy(update=dict(outcome_unknown=True, execution_terminated=False,
                    exit_code=None, error_type='control_outcome_unknown'))
                status = 'inconclusive'
                break
        unchanged = snapshot.fingerprint == harness.snapshot().fingerprint
        if not unchanged:
            result = result.model_copy(update=dict(exit_code=1, error_type='check_mutated_candidate'))
            status = 'inconclusive'
        contract_match = any(check.kind == spec.kind and check.path == spec.path and check.expected == spec.expected
                             and check.kind in ('text_equals', 'json_equals') for check in task.acceptance_checks)
        receipts.append(VerificationReceipt(spec=spec, spec_hash=digest(canonical(spec)),
            snapshot_hash=snapshot.fingerprint, environment_hash=environment_hash,
            workspace_unchanged=unchanged, result=result, status=status, assertions=assertions,
            controls=tuple(controls), oracle_validated=contract_match))
        if result.outcome_unknown:
            break
    return tuple(receipts)


def measured_comparison(receipt: VerificationReceipt) -> bool:
    """A reached comparison, without claiming that its oracle is correct.

    Runtime file/process comparators measure actual values. For Python, reject
    constant predicates as submission/revision evidence; semantic relevance is
    still a Policy claim and is not proved by this conservative filter.
    """
    if not receipt.behavioral:
        return False
    if receipt.spec.kind in ('text_equals', 'json_equals', 'process'):
        return True
    try:
        predicates = json.loads(receipt.result.output).get('checks', [])
        for predicate in predicates:
            tree = ast.parse(predicate.get('expression', ''), mode='eval')
            if any(isinstance(node, (ast.Name, ast.Call, ast.Attribute, ast.Subscript))
                   for node in ast.walk(tree)):
                return True
    except (ValueError, TypeError, SyntaxError, AttributeError):
        pass
    return False


def verification_state(attempts, snapshot: StateSnapshot, environment_hash: str | None = None,
                       additional=(), step=0):
    attempts = tuple(attempts)
    latest, failures = {}, {}
    for attempt in attempts:
        for receipt in attempt.verification:
            latest[receipt.spec.id] = (attempt.step, receipt)
            if receipt.status == 'fail':
                predicate = canonical(receipt.spec.model_dump(exclude={'id', 'target_refs', 'oracle', 'basis', 'controls', 'replaces'}))
                failures[(receipt.spec.id, predicate)] = (attempt.step, receipt)
    for receipt in additional:
        latest[receipt.spec.id] = (step, receipt)
        if receipt.status == 'fail':
            predicate = canonical(receipt.spec.model_dump(exclude={'id', 'target_refs', 'oracle', 'basis', 'controls', 'replaces'}))
            failures[(receipt.spec.id, predicate)] = (step, receipt)
    blockers, current, replacements = [], [], []
    measured_snapshots = {(r.spec_hash, r.snapshot_hash, r.environment_hash): a.before
                          for a in attempts for r in a.verification}
    def valid(receipt):
        return receipt_is_current(receipt, snapshot, environment_hash,
            measured_snapshots.get((receipt.spec_hash, receipt.snapshot_hash, receipt.environment_hash)))
    def replacement(identity, old):
        for source_step, candidate in latest.values():
            if (identity not in candidate.spec.replaces or not candidate.spec.basis or old.oracle_validated
                    or candidate.status != 'pass' or not candidate.behavioral or not valid(candidate)):
                continue
            refs = set(candidate.spec.target_refs)
            related = bool(refs & set(old.spec.target_refs)) if old.spec.target_refs else bool(refs)
            # Wrong expected values can be corrected using the public goal and
            # a current measured comparison. A negative control is useful
            # optional evidence, not a mandatory tax on every correction.
            if related and measured_comparison(candidate):
                claim = dict(id=identity, replaced_by=candidate.spec.id, step=source_step,
                    basis=candidate.spec.basis, authority='policy replacement claim', sensitive=candidate.sensitive)
                if claim not in replacements:
                    replacements.append(claim)
                return True
        return False
    for identity, (step, receipt) in latest.items():
        current_valid = valid(receipt)
        if current_valid:
            current.append((step, receipt))
        if receipt.status != 'pass' or (receipt.spec.controls and not receipt.sensitive):
            if not replacement(identity, receipt):
                blockers.append(dict(id=identity, step=step, status=receipt.status, current=current_valid))
    for (identity, predicate), (failed_step, failed) in failures.items():
        resolved = latest.get(identity)
        candidate = resolved[1] if resolved else None
        same = candidate is not None and canonical(candidate.spec.model_dump(
            exclude={'id', 'target_refs', 'oracle', 'basis', 'controls', 'replaces'})) == predicate
        current_valid = candidate is not None and valid(candidate)
        if not (same and current_valid and candidate.status == 'pass') and not replacement(identity, failed):
            blocker = dict(id=identity, step=failed_step, status='unresolved_counterexample', current=current_valid)
            if blocker not in blockers:
                blockers.append(blocker)
    # A new ID cannot erase an old failure. Semantic equivalence/replacement is
    # not inferred from model prose or a changed shell spelling.
    ready = bool(current) and any(measured_comparison(item) and item.status == 'pass'
                                 for _, item in current) and not blockers
    checks = []
    before_by_step = {attempt.step: attempt.before for attempt in attempts}
    replaced = {item['id'] for item in replacements if item['replaced_by'] != item['id']}
    blocked_ids = {item['id'] for item in blockers}
    # Preserve an explicit revision relationship across candidate edits, while
    # still requiring its fresh passing measurement to resolve the old blocker.
    # Otherwise every edit recommends rerunning both a known wrong expectation
    # and its correction. This is a Policy choice, not an oracle upgrade.
    fixed_ids = {r.spec.id for attempt in attempts for r in attempt.verification if r.oracle_validated}
    pending_revision = {}
    for identity, (old_step, old) in latest.items():
        for candidate_id, (candidate_step, candidate) in latest.items():
            refs = set(candidate.spec.target_refs)
            related = bool(refs & set(old.spec.target_refs)) if old.spec.target_refs else bool(refs)
            if (candidate_step > old_step and candidate_id != identity
                    and identity in candidate.spec.replaces and candidate.spec.basis
                    and identity not in fixed_ids and not old.oracle_validated and related
                    and measured_comparison(candidate)):
                pending_revision[identity] = candidate_id
    for identity, (source_step, receipt) in latest.items():
        current_valid = valid(receipt)
        definition_error = None
        if receipt.spec.kind == 'python_assertions':
            try:
                compile(ast.parse(receipt.spec.code), '<verification definition>', 'exec')
            except (SyntaxError, ValueError) as error:
                definition_error = 'Invalid Python syntax: ' + getattr(error, 'msg', str(error))
        invalidated_by = []
        file_comparison = receipt.spec.kind in ('text_equals', 'json_equals')
        if receipt.snapshot_hash != snapshot.fingerprint and (not file_comparison or not current_valid):
            invalidated_by.append('candidate_state_changed')
        if not file_comparison and environment_hash is not None and receipt.environment_hash != environment_hash:
            invalidated_by.append('execution_scope_changed')
        if not receipt.workspace_unchanged:
            invalidated_by.append('check_changed_candidate')
        previous = before_by_step.get(source_step)
        changed_files = (list(StateDelta.between(previous, snapshot).changed_files)
                         if previous is not None else [])
        if identity in replaced:
            phase = 'superseded'
        elif identity in pending_revision:
            phase = 'revision_pending'
        elif definition_error:
            # Changing the candidate cannot repair malformed saved source.
            # Keep old journals readable and request a check correction.
            phase = 'check_inconclusive'
        elif not current_valid:
            phase = 'needs_recheck' if receipt.workspace_unchanged else 'check_inconclusive'
        elif receipt.status == 'inconclusive':
            phase = 'check_inconclusive'
        elif receipt.status == 'fail':
            phase = 'current_failure'
        elif receipt.spec.controls and not receipt.sensitive:
            phase = 'control_inconclusive'
        elif identity in blocked_ids:
            phase = 'unresolved_expectation'
        elif not measured_comparison(receipt):
            phase = 'insufficient_comparison'
        else:
            phase = 'passed'
        checks.append(dict(id=identity, step=source_step, kind=receipt.spec.kind,
            phase=phase, last_status=receipt.status, current=current_valid,
            invalidated_by=invalidated_by, changed_files=changed_files[:8],
            changed_files_total=len(changed_files), target_refs=receipt.spec.target_refs))
        if definition_error:
            checks[-1]['definition_error'] = definition_error
        if identity in pending_revision:
            checks[-1]['revision_check_id'] = pending_revision[identity]
    phases = {row['phase'] for row in checks}
    phase = ('ready_to_submit' if ready else
             'current_failure' if 'current_failure' in phases else
             'check_inconclusive' if phases & {'check_inconclusive', 'control_inconclusive', 'insufficient_comparison'} else
             'unresolved_expectation' if 'unresolved_expectation' in phases else
             'needs_recheck' if 'needs_recheck' in phases else 'needs_check')
    # These are callable saved definitions, not a prescribed repair strategy.
    # A changed candidate has unknown correctness until a new measurement.
    recheck_ids = [row['id'] for row in checks if row['phase'] == 'needs_recheck']
    suggested_action = (dict(kind='done') if ready else
                        dict(kind='verify', check_ids=recheck_ids[:8]) if phase == 'needs_recheck' else None)
    phases_by_id = {row['id']: row['phase'] for row in checks}
    for blocker in blockers:
        blocker['phase'] = phases_by_id[blocker['id']]
    coverage = {}
    for source_step, receipt in current:
        for ref in receipt.spec.target_refs:
            coverage.setdefault(ref, []).append(dict(id=receipt.spec.id, step=source_step,
                status=receipt.status, measured=measured_comparison(receipt),
                expectation_authority='public contract' if receipt.oracle_validated else
                    'Policy derivation from public input' if receipt.spec.oracle == 'public_input' else 'Policy expectation',
                basis=receipt.spec.basis))
    return dict(ready=ready, phase=phase, checks=checks, suggested_action=suggested_action, blockers=blockers,
        current=[dict(id=r.spec.id, step=s, status=r.status, behavioral=r.behavioral,
                      assertions=r.assertions, sensitive=r.sensitive, oracle_validated=r.oracle_validated,
                      measured=measured_comparison(r), target_refs=r.spec.target_refs)
                 for s, r in current], replacements=replacements,
                 goal_evidence=coverage, coverage='Policy relevance claims; full requirement coverage unknown')


def verification_followup(state: dict, limit: int = 6) -> dict:
    """A bounded current-work view; historical outcomes never become new facts."""
    rank = {'current_failure': 0, 'check_inconclusive': 1, 'control_inconclusive': 1,
            'unresolved_expectation': 2, 'needs_recheck': 3, 'insufficient_comparison': 4,
            'revision_pending': 4, 'passed': 5, 'superseded': 6}
    rows = sorted(state['checks'], key=lambda row: (rank[row['phase']], -row['step']))
    phase = state['phase']
    next_step = {
        'needs_check': 'Select key public-goal behaviors and measure them; no usable current comparison exists.',
        'needs_recheck': 'The last result belongs to an older candidate state or execution scope. Current correctness is unknown, not a new failure. '
                         'If the saved definition is still valid, rerun it with check_ids; do not rewrite unchanged files to clear history.',
        'current_failure': 'Inspect the measured mismatch: repair the candidate, or explicitly revise an unsupported expectation from public inputs.',
        'check_inconclusive': 'The check did not establish correctness. Repair the check/prerequisite or explicitly replace it; inconclusive is not a task counterexample.',
        'unresolved_expectation': 'A previous predicate remains unresolved. A new passing definition does not erase it; rerun the original or revise it with public basis/replaces.',
        'ready_to_submit': 'Current self-checks permit done. Review the full public goal; external acceptance remains pending.',
    }[phase]
    return dict(phase=phase, ready=state['ready'], checks=rows[:limit], checks_omitted=max(0, len(rows)-limit),
                suggested_action=state['suggested_action'], next_step=next_step, coverage='unknown')


def comparison_observation(receipt: VerificationReceipt) -> dict:
    """Describe measured operands, not the truth of the Policy's expectation."""
    try:
        detail = parse_json(receipt.result.output)
    except ValueError:
        return dict(output=receipt.result.output[-800:], error=receipt.result.error_type)
    if not isinstance(detail, dict):
        return dict(output=receipt.result.output[-800:])
    rows = detail.get('checks', [])
    selected = [row for row in rows if not row.get('passed', False)] or rows[-2:]
    comparisons = []
    for row in selected[:2]:
        item = {key: row[key] for key in ('expression', 'passed', 'actual', 'expected', 'left', 'right', 'error')
                if key in row}
        actual, expected = row.get('actual', row.get('left')), row.get('expected', row.get('right'))
        if isinstance(actual, str) and isinstance(expected, str):
            item['text_shape'] = row.get('text_shape', text_shape(actual, expected))
        comparisons.append(item)
    if 'actual' in detail and 'expected' in detail:
        actual, expected = detail['actual'], detail['expected']
        item = dict(actual=actual, expected=expected, matched=detail.get('matched'),
                    operands_complete=detail.get('operands_complete', False))
        if isinstance(actual, str) and isinstance(expected, str):
            item['text_shape'] = detail.get('text_shape', text_shape(actual, expected))
        comparisons.append(item)
    for row in comparisons:
        for key in ('actual', 'expected', 'left', 'right'):
            if key in row and len(canonical(row[key])) > 600:
                row[key] = dict(display=canonical(row[key])[:600], complete=False)
    return dict(comparisons=comparisons, error=detail.get('error'),
                diagnostic_output=str(detail.get('output', ''))[-600:])


def text_shape(actual: str, expected: str) -> dict:
    # These counts refer to decoded measured strings, not their JSON rendering.
    return dict(actual_length=len(actual), expected_length=len(expected),
        actual_newlines=actual.count('\n'), expected_newlines=expected.count('\n'),
        actual_literal_backslash_n=actual.count('\\n'), expected_literal_backslash_n=expected.count('\\n'))


def compile_goal_conditions(goal: str, workspace: str = '/app') -> tuple[GoalCondition, ...]:
    """Recognize explicit operands, never task names, hidden tests or example outputs.

    File/interface facts have narrow interpreters. Other directive sentences stay
    visible as behavioral criteria whose truth requires evidence and Policy review.
    Code fences and examples are context, not automatically inferred constraints.
    """
    rows, seen = [], set()
    fenced = False
    schema_example = False
    group = None
    file_re = re.compile(r'(?:/[\w.-]+/)*[\w.-]+\.[A-Za-z][\w.-]*')
    for number, line in enumerate(goal.splitlines(), 1):
        text = line.strip()
        if text.startswith('```'):
            fenced = not fenced
            continue
        if fenced or not text or text.startswith(('http:', 'https:')):
            continue
        ref = f'G{number}'
        if re.search(r'\b(?:Input|Output) Data Schema\b', text, re.I):
            schema_example = True
            group = None
            continue
        if schema_example:
            if re.fullmatch(r'[\s*#]*Requirements[\s*:]*', text, re.I):
                schema_example = False
            else:
                continue
        if text.startswith(('`', '"', "'", '[', '{', '}', ']', '...')) or re.search(r'^(?:For\s+)?Example\b|\bexamples?\s+(?:format|below|above)\b', text, re.I):
            continue
        if re.match(r'^\d+\.', text):
            group = None
        def add(kind, quote, **operands):
            key = (kind, operands.get('path', ''), operands.get('symbol', ''), operands.get('expected', ''),
                   quote if kind == 'behavior' else '')
            if key in seen:
                return
            seen.add(key)
            rows.append(GoalCondition(id='goal:' + digest(canonical(key))[:12], ref=ref,
                quote=quote, kind=kind, **operands))
        # Associate a path with its preceding deliverable phrase, not with a
        # nearby input filename. Reject negation, alternatives and quoted examples.
        paths = []
        for match in file_re.finditer(text):
            raw = match.group().rstrip('.')
            if raw.startswith('/'):
                if not raw.startswith(workspace.rstrip('/') + '/'):
                    continue
                path = raw[len(workspace.rstrip('/')) + 1:]
            else:
                path = raw
            try:
                from .core import validate_relative_path
                validate_relative_path(path)
            except ValueError:
                continue
            prefix = text[max(0, match.start()-170):match.start()]
            prefix = re.split(r'(?<=[.!?])\s+', prefix)[-1]
            if re.search(r'\b(?:from|using|reading)\b(?!.*\b(?:to|at|named|called)\b)[^.;]*$', prefix, re.I):
                continue
            deliver = re.search(r'\b(create|write|save|generate|put|output|echo|store|record|produce)\b', prefix, re.I)
            specified = re.search(r'\b(file|code|command|output)\b.*\b(must|should)\b.*\b(in|to|at)\b', prefix, re.I)
            specified = specified or re.search(r'\b(file|code|command|output)\b.*\b(must|should)\b.*\b(saved|written|created)\b', prefix, re.I)
            if not (deliver or specified) or re.search(r"\b(not|never|don't|avoid|example|e\.g)\b", prefix, re.I):
                continue
            # A shell redirect in a prose request counts; files embedded inside
            # standalone commands/URLs/format examples do not.
            if '://' in prefix or text.startswith(('`jq ', 'jq ', '`python ', 'python ')):
                continue
            paths.append(path)
            add('artifact', line, path=path)
        signature = re.search(r'\b(async\s+(?:def\s+)?|def\s+)?([A-Za-z_]\w*)\(([^\n`]+)\)\s*->\s*([^`\.]+)', text)
        if signature and ('function' in text.lower()) and paths:
            args = signature[3]
            parameters = tuple(re.findall(r'(?:^|,)\s*([A-Za-z_]\w*)\s*:', args))
            for path in paths:
                if path.endswith('.py'):
                    add('python_interface', line, path=path, symbol=signature[2],
                        required_async=bool(signature[1] and 'async' in signature[1]),
                        parameters=parameters, callable_tasks='Callable[' in args and 'Awaitable[' in args)
        # Explicit literal contents, without inventing a trailing-newline rule.
        literal = re.search(r'\bWrite\s+["“]([^"”]+)["”]\s+(?:to|into)\s+(?:it|the file)\b', text, re.I)
        if literal and len(paths) == 1:
            add('text_literal', line, path=paths[0], expected=literal[1])
        directive = re.search(r'\b(must|should|requires?|requirements|task|function|want|recover|analyze|calculate|implement|filter|sort|rename|format|extract|count|include|convert|ensure|keep|cancel|cleanup|fix|at least|at most)\b', text, re.I)
        if directive and not text.startswith(('#', 'For example', 'Example', '```')):
            child = group is not None and (text.startswith('- ') or line.startswith('   '))
            if child:
                rows[group] = rows[group].model_copy(update={'related_refs': (*rows[group].related_refs, ref)})
            else:
                add('behavior', line)
                if rows and rows[-1].kind == 'behavior' and (text.endswith(':') or re.match(r'^\d+\.', text)):
                    group = len(rows)-1
        elif group is not None and text.startswith('- '):
            rows[group] = rows[group].model_copy(update={'related_refs': (*rows[group].related_refs, ref)})
    # Explicit format declarations are constraints, not illustrative values.
    # Resolve a unique declared output of the stated format; ambiguous scopes
    # stay semantic criteria rather than gaining guessed file constraints.
    lines = goal.splitlines()
    csv_paths = list(dict.fromkeys(r.path for r in rows if r.kind=='artifact' and r.path.endswith('.csv')))
    json_paths = list(dict.fromkeys(r.path for r in rows if r.kind=='artifact' and r.path.endswith('.json')))
    for index,line in enumerate(lines):
        if len(csv_paths)==1 and re.search(r'following (?:structure|format)|header(?: row)?',line,re.I):
            header = lines[index+1].strip().strip('`') if index+1<len(lines) else ''
            if re.fullmatch(r'[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)+',header):
                rows.append(GoalCondition(id='goal:'+digest('csv-header:'+csv_paths[0])[:12],
                    ref=f'G{index+2}',quote=lines[index+1],kind='csv_header',path=csv_paths[0],expected=header))
        if len(json_paths)==1 and re.search(r'output.*(?:should|must).*format',line,re.I) and '[{' in line:
            keys=tuple(dict.fromkeys(re.findall(r'"([^"\n]+)"\s*:',line)))
            if keys:
                rows.append(GoalCondition(id='goal:'+digest('json-shape:'+json_paths[0])[:12],
                    ref=f'G{index+1}',quote=line,kind='json_shape',path=json_paths[0],expected='array',required_keys=keys))
    return tuple(rows)


def current_file_text(path, attempts, snapshot):
    """Recover only complete, hash-matching recorded content; no implicit reads."""
    file = snapshot.file(path)
    if file is None or file.kind != 'file':
        return None, None
    for a in reversed(attempts):
        for observed in a.goal_observations:
            if observed.path==path and observed.sha256==file.sha256:
                return observed.result.output,a.step
        if a.tool_executed is False or a.action.path != path or a.after.file(path) != file:
            continue
        text = a.action.content if a.action.kind == ActionKind.WRITE else a.result.output if a.action.kind == ActionKind.READ and a.result.exit_code == 0 else None
        if text is not None and hashlib.sha256(text.encode('utf-8')).hexdigest() == file.sha256:
            return text, a.step
    return None, None


def public_goal_state(task, attempts, snapshot, evidence, notes_history):
    """Persistent public criteria joined with candidate facts and revisable claims."""
    conditions = task.goal_conditions
    if not conditions:
        return [], []
    notes = {}
    for entry in notes_history:
        for note in entry.notes.requirements:
            notes[note.ref] = (entry, note)
    rows, blockers = [], []
    by_step = {a.step: a for a in attempts}
    for condition in conditions:
        status, fact, gap, steps = 'unknown', 'Not yet observed.', '', []
        path = condition.path
        file = snapshot.file(path) if path else None
        text, content_step = current_file_text(path, attempts, snapshot) if path else (None, None)
        if condition.kind == 'artifact':
            status = 'supported' if file is not None and file.kind == 'file' else 'contradicted'
            fact = f'{path}: regular file present.' if status == 'supported' else f'{path}: required regular file missing.'
            gap = '' if status == 'supported' else f'Create the required deliverable {path}; successful related commands do not create this file automatically.'
        elif condition.kind == 'text_literal' and text is not None:
            status = 'supported' if text.rstrip('\r\n') == condition.expected else 'contradicted'
            fact = f'{path}: content {text[:120]!r}; requested literal {condition.expected!r} (no inferred newline condition).'
            gap = '' if status == 'supported' else f'Write the public literal to {path}; inspect decoded contents, not an invented escape sequence.'
            steps = [content_step]
        elif condition.kind == 'python_interface' and text is not None:
            try:
                tree = ast.parse(text)
                fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == condition.symbol), None)
                if fn is not None:
                    params = [p.arg for p in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)]
                    actual_async = isinstance(fn, ast.AsyncFunctionDef)
                    status = 'contradicted' if condition.required_async and not actual_async or any(p not in params for p in condition.parameters) else 'supported'
                    fact = f'{path}: {"async def" if actual_async else "def"} {fn.name}({", ".join(params)}).'
                    gap = '' if status == 'supported' else f'Public interface requires {"async def " if condition.required_async else ""}{condition.symbol}({", ".join(condition.parameters)}). Repair the implementation interface; do not change this public requirement to match a test.'
                else:
                    fact = f'{path}: no direct declaration of {condition.symbol}; it may be imported or assigned.'
                    gap = f'Inspect the exported {condition.symbol} and its signature; static absence alone does not prove an imported symbol is missing.'
            except SyntaxError as error:
                status, fact = 'contradicted', f'{path}: Python syntax error at line {error.lineno}: {error.msg}.'
                gap = 'Repair the syntax before testing behavior.'
            steps = [content_step]
        elif condition.kind == 'csv_header' and text is not None:
            try:
                actual=next(csv.reader(io.StringIO(text)),[])
                expected=next(csv.reader([condition.expected]))
                status='supported' if actual==expected else 'contradicted'
                fact=f'{path}: first CSV row {actual!r}; required header {expected!r}.'
                gap='' if status=='supported' else f'Add or repair the required CSV header in {path}; correct data rows do not replace the header.'
            except csv.Error as error:
                status,fact,gap='contradicted',str(error),'Repair the declared CSV format.'
            steps=[content_step]
        elif condition.kind == 'json_shape' and text is not None:
            try:
                data=parse_json(text)
                correct=isinstance(data,list) and all(isinstance(item,dict) and set(condition.required_keys)<=set(item) for item in data)
                status='supported' if correct else 'contradicted'
                fact=f'{path}: JSON root {type(data).__name__}; required array of objects with keys {list(condition.required_keys)!r}.'
                gap='' if correct else 'Repair the output structure. Error messages or strings are not recovered data objects; inspect the producing command and its stderr.'
            except ValueError as error:
                status,fact,gap='contradicted',str(error),'Repair the required JSON syntax and structure.'
            steps=[content_step]
        elif condition.kind == 'behavior':
            matched = [e for e in evidence if condition.ref in e['target_refs'] and e['current'] and e['measured']]
            entry, note = notes.get(condition.ref, (None, None))
            if note is not None and note.satisfied_when:
                cited = [by_step[s] for s in note.evidence_steps if s in by_step]
                current = [a for a in cited if not a.result.error_type and a.result.exit_code == 0 and not a.result.outcome_unknown and a.after.fingerprint == snapshot.fingerprint]
                chosen = [e for e in evidence if e['id'] in note.evidence_check_ids and e['current'] and e['measured']]
                valid = matched + chosen
                if note.status == 'supported' and (any(e['status'] == 'pass' for e in valid) or current):
                    status, fact = 'supported', 'Policy goal review supported by cited current observations; semantic interpretation remains a claim.'
                elif note.status == 'contradicted':
                    status, fact = 'disputed', 'Policy reports an unmet behavior; inspect its cited evidence.'
                steps = list(note.evidence_steps)
                gap = note.needed_information or note.uncertainty
            if not gap and status != 'supported':
                gap = 'Determine a concrete satisfaction condition for this public criterion; measure it and cite current evidence in the goal review.'
            if matched and status == 'unknown':
                fact = 'Related predicates exist; no current review establishes how they satisfy this criterion.'
        if status == 'unknown' and not gap:
            gap = f'Read {path} to observe its current contents and compare the public condition.' if path and file else f'Create or inspect {path} before checking this condition.'
        row = dict(id=condition.id, ref=condition.ref, related_refs=list(condition.related_refs), quote=condition.quote, kind=condition.kind,
            path=path, symbol=condition.symbol, status=status, fact=fact, gap=gap,
            content_observed=text is not None,
            evidence_steps=steps, authority='explicit public operand' if condition.kind != 'behavior' else 'public criterion; semantic review required')
        rows.append(row)
        if status == 'contradicted':
            blockers.append(dict(id=condition.id, reason=gap, status=status))
    return rows, blockers


def check_goal_diagnostics(spec, conditions):
    """Narrow static contradictions in the test invocation, not a correctness oracle."""
    if spec.kind != 'python_assertions':
        return []
    try:
        tree = ast.parse(spec.code)
    except SyntaxError:
        return []
    issues = []
    for goal in conditions:
        if goal.kind != 'python_interface':
            continue
        names = {goal.symbol}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(a.asname or a.name for a in node.names if a.name == goal.symbol)
        async_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
        coroutine_lists = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.ListComp, ast.List)):
                elements = [node.value.elt] if isinstance(node.value, ast.ListComp) else node.value.elts
                if elements and all(isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id in async_names for e in elements):
                    coroutine_lists.update(t.id for t in node.targets if isinstance(t, ast.Name))
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id in names and goal.required_async:
                issues.append(dict(category='check_definition', ref=goal.ref, line=node.lineno,
                    gap=f'Check calls async {goal.symbol} without awaiting or scheduling its result. Repair the check invocation; preserve the public async interface.'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names and node.args and goal.callable_tasks and isinstance(node.args[0], ast.Name) and node.args[0].id in coroutine_lists:
                issues.append(dict(category='check_definition', ref=goal.ref, line=node.lineno,
                    gap=f'Check passes coroutine objects to {goal.symbol}; public tasks are callables returning awaitables. Pass callables, not already-created coroutines.'))
    return issues


def goal_assessment(task: TaskContract, attempts, snapshot: StateSnapshot,
                    environment_hash: str | None = None, *, notes_history=(),
                    verdict: Verdict | None = None, state: dict | None = None) -> dict:
    """Continuous goal/evidence/gap projection; no tools and no model calls.

    Check outcomes and source bindings are separate. A Policy expectation never
    becomes an authoritative task constraint by failing or by claiming an oracle.
    The original contract, immutable observations and unknowns remain visible.
    """
    attempts = tuple(attempts)
    state = state or verification_state(attempts, snapshot, environment_hash)
    refs = requirement_sources(task)
    latest = {r.spec.id: (a.step, r, a.before) for a in attempts for r in a.verification}
    phases = {row['id']: row['phase'] for row in state['checks']}
    evidence = []
    for identity, (step, receipt, before) in latest.items():
        # File comparators depend on one file, not unrelated writes or a shell
        # epoch. Python/process checks retain conservative whole-scope validity.
        if receipt.spec.kind in ('text_equals', 'json_equals'):
            current = receipt_is_current(receipt, snapshot, environment_hash, before)
            validity = 'same compared file' if current else 'compared file changed or unavailable'
        else:
            current = (receipt.workspace_unchanged and receipt.snapshot_hash == snapshot.fingerprint
                       and (environment_hash is None or receipt.environment_hash == environment_hash))
            if environment_hash is None and any(a.step > step and a.action.kind == ActionKind.COMMAND
                    and a.tool_executed is not False for a in attempts):
                # An exported IR has no live harness description. Recorded
                # later shell actions still prove that scope may have changed.
                current = False
            validity = 'same captured scope' if current else 'captured candidate/environment scope changed'
        fixed = receipt.oracle_validated and any(c.kind == receipt.spec.kind and c.path == receipt.spec.path
            and c.expected == receipt.spec.expected and c.kind in ('text_equals', 'json_equals')
            for c in task.acceptance_checks)
        evidence.append(dict(id=identity, step=step, status=receipt.status, current=current,
            validity=validity, measured=measured_comparison(receipt), target_refs=list(receipt.spec.target_refs),
            authority='fixed public contract' if fixed else 'Policy expectation; derivation unconfirmed',
            basis=receipt.spec.basis, claimed_oracle=receipt.spec.oracle, fixed=fixed,
            observation=comparison_observation(receipt), check_phase=phases.get(identity)))

    notes = {}
    for entry in notes_history:
        for note in entry.notes.requirements:
            # One public line may contain several behaviors. A named
            # interpretation keeps those distinct without inventing obligations.
            key = (note.ref or note.quote, note.interpretation or note.satisfied_when)
            notes[key] = (entry, note)
    requirements = []
    mapped = set()
    for entry, note in notes.values():
        linked = [e for e in evidence if note.ref and note.ref in e['target_refs']]
        cited = list(dict.fromkeys((*note.evidence_steps, *note.check_steps)))
        requirements.append(dict(ref=note.ref, quote=note.quote or refs.get(note.ref, ''),
            interpretation=note.interpretation, satisfied_when=note.satisfied_when,
            interpretation_authority='Policy interpretation; semantic coverage unconfirmed',
            interpretation_step=entry.step, uncertainty=note.uncertainty,
            requested_information=note.needed_information, cited_steps=cited, evidence=linked))
        if note.ref:
            mapped.add(note.ref)
    for ref in dict.fromkeys(ref for e in evidence for ref in e['target_refs']):
        if ref in mapped:
            continue
        requirements.append(dict(ref=ref, quote=refs.get(ref, ''), interpretation='', satisfied_when='',
            interpretation_authority='Policy check binding; semantic coverage unconfirmed',
            uncertainty='', requested_information='', cited_steps=[],
            evidence=[e for e in evidence if ref in e['target_refs']]))
        mapped.add(ref)
    for row in requirements:
        active = [e for e in row['evidence'] if e['current']]
        failed = [e for e in active if e['status'] == 'fail' and e['measured']]
        passed = [e for e in active if e['status'] == 'pass' and e['measured']]
        if failed:
            status = 'contradicted' if any(e['fixed'] for e in failed) else 'disputed'
            gap = 'Measured mismatch; determine whether the public requirement, candidate or expectation explains it.'
        elif passed:
            status = 'supported'
            gap = 'Listed predicates are supported; relevance and remaining cases still need goal review.'
        else:
            status = 'unknown'
            gap = ('Existing evidence is stale; current behavior has not been measured.' if row['evidence'] and not active
                   else 'No completed relevant comparison establishes this behavior.')
        row.update(status=status, gap=gap, needed_information=row.pop('requested_information') or gap,
                   coverage='only the listed predicates; full semantic coverage unknown')

    objectives, hard = public_goal_state(task, attempts, snapshot, evidence, notes_history)
    review_refs = sorted({r['ref'] for r in objectives if r['kind'] == 'behavior'})
    current_steps = {a.step for a in attempts if a.tool_executed is not False and not a.result.outcome_unknown
        and a.after.fingerprint == snapshot.fingerprint}
    current_checks = {e['id'] for e in evidence if e['current']}
    reviewed = {n.ref for entry in notes_history for n in entry.notes.requirements
                if n.satisfied_when and (set(n.evidence_steps) & current_steps or set(n.evidence_check_ids) & current_checks)
                and (n.status != 'unknown' or n.needed_information or n.uncertainty)}
    missing_review = [ref for ref in review_refs if ref not in reviewed]
    if task.require_goal_review:
        # Narrow operands must actually be observed. Semantic unknowns need an
        # explicit evidence review, not a blanket requirement to pass every test.
        hard.extend(dict(id=r['id'], reason=r['gap'], status='unknown') for r in objectives
                    if r['kind'] in ('python_interface', 'text_literal', 'csv_header', 'json_shape') and r['status'] == 'unknown'
                    and not r['content_observed'])
        if missing_review:
            hard.append(dict(id='goal-review', reason='Before submission include done.review for these criteria: ref, status, satisfied_when and current evidence_steps/evidence_check_ids. Ordinary actions need no review.',
                             refs=missing_review, status='unknown'))
    fixed_rows = []
    actual_verdict = current_verdict(task, verdict, snapshot) if verdict is not None else None
    contract_evidence = {e.check_id: e for e in actual_verdict.evidence} if actual_verdict else {}
    for check in task.acceptance_checks:
        if check.kind == 'external':
            continue
        item = contract_evidence.get(check.id)
        status = 'supported' if item and item.passed else 'contradicted' if item else 'unknown'
        fixed_rows.append(dict(id=check.id, status=status, authority='fixed public contract',
            measured=bool(item), gap='' if status == 'supported' else 'Fixed acceptance condition is not established.',
            result=item.result.output[-500:] if item else None))
        if status != 'supported':
            hard.append(dict(id=check.id, reason='fixed public acceptance condition not satisfied', status=status))
    artifacts = []
    for path in task.expected_artifacts:
        file = snapshot.file(path)
        present = file is not None and file.kind == 'file'
        artifacts.append(dict(path=path, present=present, status='supported' if present else 'contradicted',
                              scope='regular-file presence only; content correctness unknown'))
        if not present:
            hard.append(dict(id='artifact:' + path, reason='declared regular-file deliverable missing', status='contradicted'))
    if task.require_self_check and not state['ready']:
        hard.append(dict(id='explicit-self-check-policy', reason='TaskContract explicitly requires current passing self-checks',
                         status='unknown'))

    observations = []
    by_step = {a.step: a for a in attempts}
    source_steps = list(dict.fromkeys([a.step for a in attempts[-2:]] +
                                     [s for row in requirements for s in row['cited_steps']]))
    for number in source_steps:
        a = by_step.get(number)
        if a is None or a.tool_executed is False:
            continue
        if a.action.kind == ActionKind.READ:
            file = a.after.file(a.action.path)
            current = file is not None and file == snapshot.file(a.action.path)
        else:
            current = a.after.fingerprint == snapshot.fingerprint
        observations.append(dict(step=a.step, kind=a.action.kind.value, path=a.action.path or None,
            exit_code=a.result.exit_code, error=a.result.error_type, outcome_unknown=a.result.outcome_unknown,
            current_file_scope=current, process_network_freshness='unknown',
            changed_files=list(a.delta.changed_files), output=a.result.output[-600:] if not a.verification else None))

    issues = []
    for e in evidence:
        if not e['current'] or e['status'] != 'pass' or not e['measured']:
            question = ('What changed in the relevant state, and what current behavior needs remeasurement?' if not e['current']
                        else 'Does the expected value follow from the public goal/input, and did the intended invocation actually run?'
                        if e['status'] == 'fail' else 'What prerequisite or check definition prevented the comparison from completing?')
            issues.append(dict(id=e['id'], step=e['step'], current=e['current'], status=e['status'],
                authority=e['authority'], target_refs=e['target_refs'], observation=e['observation'],
                needed_information=question, blocks_submission=e['fixed'] and e['current'] and e['status'] != 'pass'))
    invocation_issues = [dict(check_id=identity, step=step, **issue)
        for identity, (step, receipt, _) in latest.items()
        for issue in check_goal_diagnostics(receipt.spec, task.goal_conditions)]
    for issue in issues:
        specific = [r['gap'] for r in invocation_issues if r['check_id'] == issue['id']]
        if specific:
            issue['needed_information'] = ' '.join(specific)
            issue['category'] = 'check_definition'
        else:
            receipt = latest[issue['id']][1]
            try:
                detail = parse_json(receipt.result.output)
            except ValueError:
                detail = {}
            frames = detail.get('traceback', [])
            candidate_frames = [f for f in frames if f['file'] not in ('<string>', '<policy-check>')
                and any(f['file'].replace('\\', '/').endswith('/'+g.path) for g in task.goal_conditions if g.path)]
            if candidate_frames:
                issue.update(category='candidate_execution', candidate_frames=candidate_frames,
                    needed_information='The recorded exception includes candidate code: ' + str(detail.get('error', '')) +
                    '. Compare the invocation with the public interface, then locate the candidate failure; do not assume the harness is wrong.')
    unbound = [e['id'] for e in evidence if not e['target_refs']]
    unmapped = [ref for ref in refs if ref not in mapped]
    previous_definitions = []
    for a in attempts:
        for r in a.verification:
            newest = latest.get(r.spec.id)
            if newest and r.spec_hash != newest[1].spec_hash and r.status == 'fail':
                previous_definitions.append(dict(id=r.spec.id, step=a.step, latest_step=newest[0],
                    fact='An older definition failed; it is not a measurement of the latest predicate.',
                    replacement_claimed=r.spec.id in newest[1].spec.replaces))
    return dict(goal_hash=digest(canonical(dict(goal=task.goal, constraints=task.constraints))),
        requirements=requirements, objectives=objectives, invocation_issues=invocation_issues,
        review_refs=review_refs, missing_review_refs=missing_review,
        fixed_requirements=fixed_rows, artifacts=artifacts,
        current_state=dict(snapshot_hash=snapshot.fingerprint, observed_files=len(snapshot.files), observations=observations[-4:]),
        check_issues=issues, historical_definition_changes=previous_definitions[-4:],
        source_review=dict(unmapped_refs=unmapped, unbound_check_ids=unbound,
            meaning='Unmapped text needs review; a text line is not an automatically inferred obligation.'),
        submission=dict(allowed=not hard, hard_blockers=hard, predicate_ready=state['ready'],
            completion_established=bool(actual_verdict and actual_verdict.passed),
            external_acceptance='pending' if task.deferred_verification else 'inline contract',
            meaning='Runtime permission is not goal completion. Policy must review all public requirements before done.'),
        coverage='unknown; source references and Policy interpretations do not certify full goal coverage')


def goal_feedback(report: dict, limit: int = 4) -> dict:
    """Bounded valid rows for every decision, preserving qualifications."""
    rows = sorted(report['requirements'], key=lambda r: ({'contradicted': 0, 'disputed': 1, 'unknown': 2, 'supported': 3}[r['status']]))
    shown = []
    for row in rows[:limit]:
        item = {k: v for k, v in row.items() if k != 'evidence'}
        item['evidence'] = [{k: e[k] for k in ('id', 'step', 'status', 'current', 'authority', 'measured')} for e in row['evidence'][-3:]]
        shown.append(item)
    goals = sorted(report.get('objectives', []), key=lambda r: ({'contradicted': 0, 'disputed': 1, 'unknown': 2, 'supported': 3}[r['status']], r['kind'] == 'behavior'))
    return dict(objectives=goals[:limit], objectives_omitted=max(0, len(goals)-limit),
        invocation_issues=report.get('invocation_issues', [])[-3:],
        review_refs=report.get('review_refs', []), missing_review_refs=report.get('missing_review_refs', []),
        requirements=shown, requirements_omitted=max(0, len(rows)-len(shown)),
        fixed_requirements=report['fixed_requirements'][:4], artifacts=report['artifacts'][:4],
        current_state=report['current_state'], check_issues=report['check_issues'][-3:],
        check_issues_omitted=max(0, len(report['check_issues'])-3),
        historical_definition_changes=report['historical_definition_changes'][-2:],
        source_review=dict(unmapped_refs=report['source_review']['unmapped_refs'][:8],
            unmapped_count=len(report['source_review']['unmapped_refs']),
            unbound_check_ids=report['source_review']['unbound_check_ids'][:4], meaning=report['source_review']['meaning']),
        submission=report['submission'], coverage=report['coverage'])


def lower_legacy_verify(action: Action) -> Action:
    """Instrument a single inline Python check; arbitrary shell stays opaque."""
    if not action.command:
        return action
    spec = CheckSpec(id='legacy', kind='command', command=action.command)
    try:
        words = shlex.split(action.command)
        name = words[0].replace('\\', '/').rsplit('/', 1)[-1]
        if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?', name) and '-c' in words:
            index = words.index('-c')
            if index + 2 == len(words) and all(word in ('-B', '-u') for word in words[1:index]):
                spec = CheckSpec(id='legacy', kind='python_assertions', code=words[index + 1])
    except (ValueError, IndexError):
        pass
    return action.model_copy(update=dict(command='', checks=(spec,)))
