import unittest
from unittest.mock import Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.routers.amos_routes import router


class BrokerSettingsTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.db = Mock()
        self.user = {'tenant_id': 1, 'schema_name': 'tenant_one', 'is_admin': True}
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def test_admin_assigns_address_to_own_tenant_without_broker_credentials(self):
        response = self.client.post('/api/amos/config', json={
            'source': 'amoscentral', 'amoscentral_address': 'com.swiss-as.amos.customer.prod'})
        self.assertEqual(response.status_code, 200, response.text)
        statement, parameters = self.db.execute.call_args_list[0].args
        self.assertEqual(parameters['id'], 1)
        self.assertEqual(parameters['address'], 'com.swiss-as.amos.customer.prod')
        self.assertNotIn('client_secret', parameters)
        self.assertIn('amoscentral_address=:address', str(statement))

    def test_non_admin_cannot_assign_or_change_source(self):
        self.user['is_admin'] = False
        response = self.client.post('/api/amos/config', json={'source': 'csv'})
        self.assertEqual(response.status_code, 403)
        self.db.execute.assert_not_called()

    def test_missing_or_invalid_address_is_rejected(self):
        for address, status in [('', 400), ('Com.Invalid', 422), ('wrong/address', 422)]:
            response = self.client.post('/api/amos/config', json={'source': 'amoscentral', 'amoscentral_address': address})
            self.assertEqual(response.status_code, status, response.text)
        self.db.execute.assert_not_called()

    def test_public_config_never_returns_saved_passwords(self):
        self.db.execute.return_value.fetchone.return_value = ('host', 5432, 'db', 'user', 24, 'amoscentral', 'com.example.sender')
        result = self.client.get('/api/amos/config').json()
        self.assertEqual(result['password'], '')
        self.assertEqual(result['amoscentral_address'], 'com.example.sender')
        self.assertNotIn('client_secret', result)

    def test_duplicate_address_returns_conflict_and_rolls_back(self):
        from sqlalchemy.exc import IntegrityError
        self.db.execute.side_effect = IntegrityError('statement', {}, Exception('duplicate address'))
        response = self.client.post('/api/amos/config', json={'source': 'amoscentral', 'amoscentral_address': 'com.example.sender'})
        self.assertEqual(response.status_code, 409)
        self.db.rollback.assert_called_once()
