"""Callable definitions and evaluator identities have distinct owners."""
import unittest

from horizon_weaver.context import ContextCompiler
from horizon_weaver.core import (Action, Attempt, Budget, Check, CheckSpec, FileState, GraphView,
    ProgressAssessment, StateDelta, StateSnapshot, TaskContract, ToolResult, VerificationReceipt, canonical, digest)


class CheckCatalogTests(unittest.TestCase):
    def task(self):
        return TaskContract(task_id='catalog', goal='The product must contain 42.', exploration_history=True,
            verification_policy='behavioral', acceptance_checks=(Check(id='EVALUATOR_PRIVATE_ID', kind='external'),),
            budget=Budget(context_tokens=14336))

    def fixture(self, identity='saved-product'):
        product=FileState(path='product.txt', kind='file', sha256=digest('42'), size=2, mode=0o644)
        snapshot=StateSnapshot(files=(product,))
        spec=CheckSpec(id=identity, kind='text_equals', path='product.txt', expected='42')
        result=ToolResult(exit_code=0, output=canonical(dict(actual='42', expected='42', matched=True)))
        receipt=VerificationReceipt(spec=spec, spec_hash=digest(canonical(spec)), snapshot_hash=snapshot.fingerprint,
            environment_hash='fixture', workspace_unchanged=True, status='pass', assertions=1, result=result)
        attempts=[Attempt(step=1, action=Action(kind='verify', checks=(spec,)), before=snapshot, after=snapshot,
            delta=StateDelta(), result=result, verification=(receipt,), tool_executed=True,
            progress=ProgressAssessment(level='verified', reason_code='fixture'))]
        for step in range(2,7):
            before=snapshot
            text=str(step)
            snapshot=StateSnapshot(files=(product, FileState(path='other.txt', kind='file', sha256=digest(text), size=1, mode=0o644)))
            attempts.append(Attempt(step=step, action=Action(kind='write', path='other.txt', content=text), before=before,
                after=snapshot, delta=StateDelta.between(before,snapshot), result=ToolResult(exit_code=0, output='wrote other.txt'),
                tool_executed=True, progress=ProgressAssessment(level='state_change', reason_code='fixture')))
        return tuple(attempts), snapshot

    def test_external_identity_stays_in_contract_but_out_of_callable_policy_view(self):
        task=self.task()
        original=canonical(task)
        request=ContextCompiler({}).build(task, GraphView(), StateSnapshot(), 1, 100000)
        self.assertNotIn('EVALUATOR_PRIVATE_ID', request.context)
        self.assertIn('Hidden grading runs after done', request.context)
        self.assertEqual(request.known_check_ids, ())
        self.assertEqual(canonical(task), original)

    def test_old_saved_definition_remains_discoverable_after_evidence_expires(self):
        attempts,snapshot=self.fixture()
        snapshot=StateSnapshot(files=(FileState(path='product.txt', kind='file', sha256=digest('changed'),
            size=7, mode=0o644), *tuple(f for f in snapshot.files if f.path != 'product.txt')))
        request=ContextCompiler({}).build(self.task(), GraphView(attempts=attempts), snapshot, 7, 100000)
        self.assertNotIn(1, request.selected_attempt_steps)
        self.assertIn('SAVED CHECK DEFINITIONS', request.context)
        self.assertIn('"id":"saved-product"', request.context)
        self.assertIn('"predicate_ready":false', request.context)
        self.assertEqual(request.known_check_ids, ('saved-product',))

    def test_self_defined_id_is_callable_even_if_its_name_matches_an_evaluator_id(self):
        attempts,snapshot=self.fixture('EVALUATOR_PRIVATE_ID')
        request=ContextCompiler({}).build(self.task(), GraphView(attempts=attempts), snapshot, 7, 100000)
        self.assertIn('"id":"EVALUATOR_PRIVATE_ID"', request.context)
        self.assertEqual(request.known_check_ids, ('EVALUATOR_PRIVATE_ID',))

    def test_public_inline_acceptance_definition_is_preserved(self):
        task=TaskContract(task_id='inline', goal='Produce 42.', acceptance_checks=(
            Check(id='PUBLIC_CHECK_ID',kind='text_equals',path='product.txt',expected='42'),))
        request=ContextCompiler({}).build(task, GraphView(), StateSnapshot(), 1, 100000)
        self.assertIn('PUBLIC_CHECK_ID', request.context)
        self.assertIn('"expected":"42"', request.context)


if __name__=='__main__':
    unittest.main(verbosity=2)
