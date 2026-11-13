import unittest
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.auth import get_current_user
from app.database import get_db
from app.routers.matching_routes import router, evaluate
from app.matching.ranking import rrf_rank


class SettingsTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.user = {'id': 1, 'tenant_id': 7, 'schema_name': 'tenant_test', 'is_admin': False}
        self.db = Mock()
        self.db.execute.return_value.fetchone.return_value = ('weighted', False, False, 0.25, 'none')
        self.db.execute.return_value.scalar_one.return_value = 0
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def test_non_admin_cannot_change_settings(self):
        response = self.client.put('/api/amos/matching-settings', json={'ranking_mode': 'rrf'})
        self.assertEqual(response.status_code, 403)
        self.db.execute.assert_not_called()

    def test_admin_updates_only_own_tenant(self):
        self.user['is_admin'] = True
        response = self.client.put('/api/amos/matching-settings', json={'ranking_mode': 'rrf'})
        self.assertEqual(response.status_code, 200)
        writes = [c for c in self.db.execute.call_args_list if 'UPDATE' in str(c.args[0])]
        self.assertEqual(writes[0].args[1]['id'], 7)
        self.assertEqual(writes[0].args[1]['mode'], 'rrf')
        self.db.commit.assert_called_once()

    def test_disabled_collection_cannot_save_a_mapping(self):
        response = self.client.post('/api/amos/confirmed-mappings', json={'session_id':'x','source_pn':'A','target_pn':'B'})
        self.assertEqual(response.status_code, 403)
        self.db.commit.assert_not_called()

    def test_trial_does_not_change_settings(self):
        self.user['is_admin'] = True
        self.db.execute.return_value.fetchone.return_value = ('weighted', True, True, .25, 'none')
        self.db.execute.return_value.fetchall.return_value = []
        response = self.client.post('/api/amos/evaluate-matching', json={'pn_weight':.7})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['eligible'], 0)
        self.db.commit.assert_not_called()

    def test_invalid_mode_and_weight_rejected(self):
        self.user['is_admin'] = True
        for data in ({'ranking_mode':'unknown'}, {'pn_weight':2}, {'reranker':'unknown'}):
            self.assertEqual(self.client.put('/api/amos/matching-settings', json=data).status_code, 422)

    def test_admin_can_select_reranker_independently(self):
        self.user['is_admin'] = True
        self.db.execute.return_value.fetchone.return_value = ('rrf', False, False, .25, 'cross_encoder')
        response = self.client.put('/api/amos/matching-settings', json={'ranking_mode':'rrf','reranker':'cross_encoder'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['reranker'],'cross_encoder')
        writes=[c for c in self.db.execute.call_args_list if 'UPDATE' in str(c.args[0])]
        self.assertEqual(writes[0].args[1]['reranker'],'cross_encoder')
        self.assertEqual(writes[0].args[1]['id'],7)

    def test_rrf_keeps_exact_priority_and_evaluation_counts(self):
        items = [dict(candidate='A', match_type='EXACT', pn_score=100, description_score=5, semantic_score=5),
                 dict(candidate='B', match_type='SEMANTIC_DESCRIPTION', pn_score=80, description_score=95, semantic_score=95)]
        self.assertEqual(rrf_rank(items)[0]['candidate'], 'A')
        result = evaluate([('A',items),(None,items),('OTHER',items)],.25)
        self.assertEqual(result['eligible'],1)
        self.assertEqual(result['no_match'],1)
        self.assertEqual(result['outside_pool'],1)
        self.assertEqual(result['results']['rrf']['top1'],1)
