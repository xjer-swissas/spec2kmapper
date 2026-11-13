import unittest
from datetime import timedelta
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import get_current_user, create_access_token
from app.database import get_db
from app.routers import spec2500_routes, amos_routes
from app.amos.sync import run_tenant_sync


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(spec2500_routes.router)
        self.app.include_router(amos_routes.router)
        self.db = Mock()
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(self.app)
        self.user = {'tenant_id': 1, 'schema_name': 'tenant_test'}

    def test_expired_token_is_401_on_all_protected_flows(self):
        token = create_access_token({'sub': 'tester', 'tenant_id': 1, 'schema_name': 'tenant_test'}, timedelta(seconds=-1))
        for method, url in [('get', '/api/amos/status'), ('get', '/api/amos/cache'), ('get', '/api/amos/csv/queries'), ('post', '/api/amos/csv/import'), ('post', '/api/spec2500/export')]:
            response = getattr(self.client, method)(url, headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(response.status_code, 401)

    def test_export_rejects_other_tenant_session(self):
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        spec2500_routes.PARSED_SESSIONS['foreign'] = {'schema': 'tenant_other'}
        try:
            response = self.client.post('/api/spec2500/export', json={
                'session_id': 'foreign', 'pn_mapping': {}, 'fp_mapping': {}, 'doc_mapping': {}})
            self.assertEqual(response.status_code, 404)
        finally:
            spec2500_routes.PARSED_SESSIONS.pop('foreign')

    def test_cache_search_is_paginated_and_parameterized(self):
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.db.execute.return_value.scalar_one.return_value = 1
        self.db.execute.return_value.fetchall.return_value = [('A1', 'Oil pump')]
        response = self.client.get('/api/amos/cache', params={'q': "%' OR 1=1 --", 'offset': 50})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items'][0]['description'], 'Oil pump')
        statement, params = self.db.execute.call_args.args
        self.assertNotIn("OR 1=1 --", str(statement))
        self.assertEqual(params['offset'], 50)
        self.assertEqual(self.client.get('/api/amos/cache?kind=tenants').status_code, 422)
        self.assertEqual(self.client.get('/api/amos/cache?offset=-1').status_code, 422)

    def test_duplicate_sync_is_not_queued_again(self):
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.db.execute.return_value.fetchone.return_value = ('host',)
        with patch('app.routers.amos_routes.claim_sync', return_value=False), patch('app.routers.amos_routes.run_tenant_sync') as run:
            response = self.client.post('/api/amos/sync')
        self.assertEqual(response.status_code, 202)
        run.assert_not_called()

    def test_failed_sync_rolls_back_and_persists_failure(self):
        db = Mock()
        db.execute.return_value.fetchone.return_value = ('tenant_test', 'host', 5432, 'db', 'user', 'secret')
        with patch('app.amos.sync.SessionLocal') as session, patch('app.amos.sync.AmosDataSyncer') as syncer, patch('app.amos.sync.logger'):
            session.return_value.__enter__.return_value = db
            syncer.return_value.run_sync.side_effect = RuntimeError('connection failed')
            run_tenant_sync(1)
        db.rollback.assert_called_once()
        self.assertIn("last_sync_status = 'FAILED'", str(db.execute.call_args.args[0]))
        db.commit.assert_called_once()
