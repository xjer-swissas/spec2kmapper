import io
import unittest
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.amos.csv_import import parse_export, prepare_exports
from app.amos.extractor import AmosDataSyncer
from app.auth import get_current_user
from app.database import get_db
from app.routers.amos_routes import router


class CsvImportTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.db = Mock()
        self.db.execute.return_value.fetchone.return_value = ('IDLE',)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: {'tenant_id': 7, 'schema_name': 'tenant_seven'}
        self.client = TestClient(app)
        self.files = {
            'parts': ('parts.csv', b'part_number,description\n001,"Pump, assembly"\n002,Child\n003,Leaf\n'),
            'configurations': ('config.csv', b'parent_pn,fp_code,fp_description,child_pn\n001,F1,Position,002\n002,F2,Leaf position,003\n'),
            'documents': ('docs.csv', b'pn,docno\n003,DOC1\n'),
        }

    def test_utf8_bom_quoted_newline_and_leading_zero(self):
        rows = parse_export(io.BytesIO('\ufeffdescription,part_number\r\n"Pump,\nassembly",001\r\n'.encode()), 'parts')
        self.assertEqual(rows, [{'description': 'Pump,\nassembly', 'part_number': '001'}])

    def test_invalid_csv(self):
        for data in [b'wrong,description\nA,B', b'part_number,description\n,B',
                     b'part_number,description\nA,B,C', b'part_number,description\n',
                     b'part_number,description\nA,"unfinished', b'part_number,description\nA,\xff']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_export(io.BytesIO(data), 'parts')
        with patch('app.amos.csv_import.MAX_FILE_BYTES', 5), self.assertRaises(ValueError):
            parse_export(io.BytesIO(b'123456'), 'parts')

    def test_header_only_optional_exports(self):
        for kind in ['configurations', 'documents']:
            content = self.files[kind][1].split(b'\n')[0] + b'\n'
            self.assertEqual(parse_export(io.BytesIO(content), kind), [])

    def test_invalid_import_never_writes(self):
        self.files['documents'] = ('bad.csv', b'incorrect\n')
        response = self.client.post('/api/amos/csv/import', files=self.files)
        self.assertEqual(response.status_code, 400)
        self.db.execute.assert_not_called()

    def test_running_sync_blocks_import(self):
        self.db.execute.return_value.fetchone.return_value = ('RUNNING',)
        response = self.client.post('/api/amos/csv/import', files=self.files)
        self.assertEqual(response.status_code, 409)
        self.db.commit.assert_not_called()
        self.db.rollback.assert_called_once()

    def test_import_propagates_hierarchy_and_commits_source(self):
        with patch('app.matching.catalog.schedule') as schedule:
            response = self.client.post('/api/amos/csv/import', files=self.files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['counts'], {'parts': 3, 'relationships': 3, 'positions': 3, 'documents': 3})
        calls = self.db.execute.call_args_list
        doc_rows = next(call.args[1] for call in calls if 'INSERT INTO' in str(call.args[0]) and 'doc_part' in str(call.args[0]))
        self.assertIn({'p': '001', 'doc': 'DOC1', 'd': 0}, doc_rows)
        for call in calls:
            if 'DELETE FROM' in str(call.args[0]):
                self.assertIn('"tenant_seven"', str(call.args[0]))
        self.assertIn("amos_source='csv'", str(calls[-1].args[0]))
        self.db.commit.assert_called_once()
        schedule.assert_called_once_with('tenant_seven')

    def test_failed_write_rolls_back(self):
        with patch.object(AmosDataSyncer, 'replace_cache', side_effect=RuntimeError('write failed')), patch('app.matching.catalog.schedule') as schedule:
            response = self.client.post('/api/amos/csv/import', files=self.files)
        self.assertEqual(response.status_code, 500)
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()
        schedule.assert_not_called()

    def test_queries_match_export_headers(self):
        response = self.client.get('/api/amos/csv/queries')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), set(self.files))
        for item in response.json().values():
            self.assertTrue(item['sql'].startswith('SELECT DISTINCT'))
            for column in item['columns']:
                self.assertIn(column, item['sql'])
