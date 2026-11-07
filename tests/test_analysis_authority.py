"""Actual analysis worker with injected DB/model: unit tests, not PG evidence."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from psycopg.errors import InsufficientPrivilege

from folio import worker


def _cursor(row):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    return cursor


def _direct_path(actor, capability=False):
    """Capability probe answers ``capability``; every other query returns actor."""
    def execute(query, params=()):
        if 'has_function_privilege' in query:
            return _cursor((capability,))
        return _cursor(actor)
    return execute


class AnalysisRequesterAuthority(unittest.TestCase):
    def test_installed_capability_takes_lock_through_fixed_definer_function(self):
        from folio.analysis_authority import requester_problem
        from folio.requester_lock import SIGNATURE
        db = MagicMock()
        db.execute.side_effect = _direct_path((True, ['reviewer']), capability=True)
        self.assertIsNone(requester_problem(db, 'unit-reviewer'))
        db.transaction.assert_called_once()
        query, params = db.execute.call_args.args
        self.assertEqual(query, 'SELECT active,roles FROM ' + SIGNATURE.replace('(text)', '(%s)'))
        self.assertEqual(params, ('unit-reviewer',))
        self.assertNotIn('FROM users', query)

    def test_missing_row_lock_privilege_blocks_before_inference_and_is_durable(self):
        rid, workspace, cid = (str(uuid4()) for _ in range(3))
        run = {'requested_by': 'unit-reviewer', 'prepare_proposal': True,
               'case_version': 1, 'source_digest': 'unit-source'}
        db = MagicMock()
        db.__enter__.return_value = db
        attempt = []

        def execute(query, params=()):
            cursor = MagicMock()
            if query.startswith('SELECT id,workspace,case_id,body FROM analysis_runs'):
                cursor.fetchone.return_value = (rid, workspace, cid, run)
            elif 'FROM users' in query:
                raise InsufficientPrivilege('unit-only permission denial')
            elif query.startswith("UPDATE analysis_runs SET status='running'"):
                attempt.append(params[0])
            elif query.startswith('SELECT attempt,lease_until'):
                cursor.fetchone.return_value = (attempt[0], True)
            return cursor

        db.execute.side_effect = execute
        case = {'id': cid, 'workspace': workspace, 'version': 1,
                'source_digest': 'unit-source', 'target_id': 'unit-target'}
        with (patch.object(worker, 'connect', return_value=db),
              patch.object(worker, 'load_case', return_value=case),
              patch.object(worker, 'event') as event,
              patch('folio.agent.run_bounded', side_effect=AssertionError(
                  'MODEL_MUST_NOT_RUN_WITHOUT_REQUESTER_LOCK')) as model):
            try:
                finished = worker.analyze_one(run_id=rid)
            except InsufficientPrivilege:
                self.fail('PERMISSION_DENIAL_ESCAPED_WITHOUT_DURABLE_BLOCKED_RESULT')
        self.assertEqual(finished['status'], 'blocked')
        result = finished['result']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error'], 'REQUESTER_LOCK_CAPABILITY_REQUIRED')
        self.assertEqual(result['target_writes'], 0)
        self.assertFalse(result['quality_gate_passed'])
        model.assert_not_called()
        persisted = [call for call in db.execute.call_args_list
                     if call.args[0].startswith('UPDATE analysis_runs SET status=%s,body=%s')]
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].args[1][0], 'blocked')
        self.assertEqual(persisted[0].args[1][1].obj['result'], result)
        self.assertEqual(persisted[0].args[1][2], rid)
        self.assertEqual(len(attempt), 0, 'No model attempt should be consumed')
        event.assert_called_once()
        self.assertEqual(event.call_args.args[2], 'DOC-analysis-completed')

    def test_lock_privilege_revoked_during_inference_is_recorded_without_a_draft(self):
        rid, workspace, cid = (str(uuid4()) for _ in range(3))
        run = {'requested_by': 'unit-reviewer', 'prepare_proposal': True,
               'case_version': 1, 'source_digest': 'unit-source'}
        db = MagicMock()
        db.__enter__.return_value = db
        attempts, auth_reads = [], []

        def execute(query, params=()):
            cursor = MagicMock()
            if query.startswith('SELECT id,workspace,case_id,body FROM analysis_runs'):
                cursor.fetchone.return_value = (rid, workspace, cid, run)
            elif 'FROM users' in query:
                auth_reads.append(True)
                if len(auth_reads) > 1:
                    raise InsufficientPrivilege('unit-only concurrent revocation')
                cursor.fetchone.return_value = (True, ['reviewer'])
            elif query.startswith("UPDATE analysis_runs SET status='running'"):
                attempts.append(params[0])
            elif query.startswith('SELECT attempt,lease_until'):
                cursor.fetchone.return_value = (attempts[0], True)
            return cursor

        db.execute.side_effect = execute
        case = {'id': cid, 'workspace': workspace, 'version': 1,
                'source_digest': 'unit-source', 'target_id': 'unit-target'}
        recommendation = {'status': 'recommended', 'target_writes': 0,
                          'quality_gate_passed': True, 'recommendation': {}}
        with (patch.object(worker, 'connect', return_value=db),
              patch.object(worker, 'load_case', return_value=case),
              patch.object(worker, 'event') as event,
              patch('folio.workbench.apply_decision', side_effect=AssertionError(
                  'NO_DRAFT_AFTER_AUTHORITY_LOSS')) as decision,
              patch('folio.agent.run_bounded', return_value=recommendation) as model):
            try:
                finished = worker.analyze_one(run_id=rid)
            except InsufficientPrivilege:
                self.fail('PERMISSION_DENIAL_ESCAPED_WITHOUT_DURABLE_BLOCKED_RESULT')
        self.assertEqual(finished['status'], 'blocked')
        self.assertEqual(finished['result']['error'], 'REQUESTER_LOCK_CAPABILITY_REQUIRED')
        self.assertEqual(finished['result']['target_writes'], 0)
        self.assertEqual(len(auth_reads), 2)
        model.assert_called_once()
        decision.assert_not_called()
        event.assert_called_once()
        self.assertGreaterEqual(db.transaction.call_count, 2)

    def test_prepare_enqueue_refuses_missing_lock_before_creating_a_run(self):
        from fastapi.testclient import TestClient

        from folio import workbench
        db = MagicMock()
        db.__enter__.return_value = db

        def execute(sql, *args):
            if 'pg_db_role_setting' in sql:
                cursor = MagicMock()
                cursor.fetchone.return_value = (False,)
                return cursor
            raise InsufficientPrivilege('unit-only denied lock')

        db.execute.side_effect = execute
        case = {'id': str(uuid4()), 'version': 1, 'source_digest': 'unit-source'}
        with (patch.object(workbench, 'connect', return_value=db),
              patch.object(workbench, 'pooled', return_value=db),
              patch.object(workbench, 'identity', return_value={'username': 'unit-reviewer'}),
              patch.object(workbench, 'command', return_value=None),
              patch.object(workbench, 'load_case', return_value=case),
              patch.object(workbench, 'recorded', return_value={'status': 'queued'}),
              patch.object(workbench, 'event') as event):
            client = TestClient(workbench.app, raise_server_exceptions=False)
            try:
                response = client.post(
                    f'/api/v1/workspaces/{workbench.WORKSPACE}/cases/{case["id"]}/analysis-runs',
                    headers={'If-Match': '"case-v1"'}, json={'prepare_proposal': True},
                )
            finally:
                client.close()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()['detail'], 'REQUESTER_LOCK_CAPABILITY_REQUIRED')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertFalse(any('INSERT INTO analysis_runs' in call.args[0]
                             for call in db.execute.call_args_list))
        event.assert_not_called()

    def test_requester_roles_are_exact_arrays_not_substring_or_mapping_authority(self):
        from folio.analysis_authority import requester_problem
        cases = [
            (None, 'REQUESTER_AUTHORITY_REVOKED'),
            ((False, ['reviewer']), 'REQUESTER_AUTHORITY_REVOKED'),
            ((True, ['viewer']), 'REQUESTER_AUTHORITY_REVOKED'),
            ((True, 'notreviewer'), 'REQUESTER_AUTHORITY_REVOKED'),
            ((True, {'reviewer': False}), 'REQUESTER_AUTHORITY_REVOKED'),
            ((True, ['reviewer']), None),
        ]
        for actor, expected in cases:
            with self.subTest(actor=actor):
                db = MagicMock()
                db.execute.side_effect = _direct_path(actor)
                self.assertEqual(requester_problem(db, 'unit-reviewer'), expected)
                db.transaction.assert_called_once()
                query, params = db.execute.call_args.args
                self.assertIn('FOR SHARE NOWAIT', query)
                self.assertEqual(params, ('unit-reviewer',))

    def test_contended_authority_lock_is_a_retryable_problem_not_an_unbounded_wait(self):
        from psycopg.errors import LockNotAvailable

        from folio.analysis_authority import requester_problem
        db = MagicMock()

        def contended(query, params=()):
            if 'has_function_privilege' in query:
                return _cursor((False,))
            raise LockNotAvailable('unit-only concurrent role update')

        db.execute.side_effect = contended
        try:
            problem = requester_problem(db, 'unit-reviewer')
        except LockNotAvailable:
            self.fail('LOCK_CONTENTION_MUST_HAVE_A_TYPED_RESULT')
        self.assertEqual(problem, 'REQUESTER_LOCK_BUSY')
        self.assertIn('FOR SHARE NOWAIT', db.execute.call_args.args[0])

    def test_expired_worker_cannot_overwrite_a_newer_terminal_body(self):
        import copy
        rid, workspace, cid = (str(uuid4()) for _ in range(3))
        run = {'requested_by': 'unit-reviewer', 'prepare_proposal': False,
               'case_version': 1, 'source_digest': 'unit-source'}
        terminal = {'status': 'blocked', 'result': {'error': 'NEWER_TERMINAL_RESULT'}}
        persisted = copy.deepcopy(terminal)
        db = MagicMock()
        db.__enter__.return_value = db

        def execute(query, params=()):
            cursor = MagicMock()
            if query.startswith('SELECT id,workspace,case_id,body FROM analysis_runs'):
                cursor.fetchone.return_value = (rid, workspace, cid, run)
            elif query.startswith('UPDATE analysis_runs SET body='):
                if "status='running'" in query and 'attempt=%s' in query and 'lease_until>clock_timestamp()' in query:
                    cursor.fetchone.return_value = None  # the newer owner already finished
                else:
                    persisted.clear()
                    persisted.update(copy.deepcopy(params[0].obj))
            return cursor

        db.execute.side_effect = execute
        case = {'id': cid, 'workspace': workspace, 'version': 1,
                'source_digest': 'unit-source', 'target_id': 'unit-target'}
        with (patch.object(worker, 'connect', return_value=db),
              patch.object(worker, 'load_case', return_value=case),
              patch.object(worker, 'event') as event,
              patch('folio.agent.run_bounded', side_effect=AssertionError(
                  'STALE_WORKER_MUST_NOT_BEGIN_INFERENCE')) as model):
            with self.assertRaisesRegex(RuntimeError, '^ANALYSIS_LEASE_LOST$'):
                worker.analyze_one(run_id=rid)
        self.assertEqual(persisted, terminal)
        model.assert_not_called()
        event.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
