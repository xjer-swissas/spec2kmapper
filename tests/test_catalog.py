import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from app.matching.catalog import hybrid_search, index_path


class CatalogTests(unittest.TestCase):
    def test_exact_identifier_outranks_semantic_result(self):
        rows = [('A-1', 'Cabin lamp'), ('B2', 'Oil pump'), ('C3', 'Oil pump')]
        index = (['Cabin lamp', 'Oil pump'], np.array([[0., 1.], [1., 0.]]))
        with patch('app.matching.catalog.encoder.encode', return_value={'lubrication unit': np.array([1., 0.])}):
            result = hybrid_search(rows, 'A-1', 'lubrication unit', index, ['C3'])
        self.assertEqual(result[0]['part_number'], 'A-1')
        self.assertEqual(result[0]['match_type'], 'EXACT PN')
        self.assertEqual(result[1]['part_number'], 'C3')
        self.assertTrue(result[1]['in_configuration'])
        self.assertEqual(result[1]['semantic_score'], 100.)
        self.assertEqual({r['part_number'] for r in result}, {'A-1','B2','C3'})

    def test_description_only_and_missing_descriptions(self):
        rows = [('P1', 'Oil pump'), ('P2', '')]
        with patch('app.matching.catalog.encoder.encode', return_value={'fluid delivery': np.array([1.,0.])}):
            result = hybrid_search(rows, '', 'fluid delivery', (['Oil pump'], np.array([[1.,0.]])))
        self.assertEqual(result[0]['part_number'], 'P1')
        self.assertIsNone(result[0]['pn_score'])
        self.assertEqual(hybrid_search(rows, 'P2', '', None)[0]['part_number'], 'P2')

    def test_normalized_identifier_and_fallback(self):
        result = hybrid_search([('A-1', 'Oil pump')], 'A1', 'oil pump', None)
        self.assertEqual(result[0]['match_type'], 'NORMALIZED PN')
        self.assertEqual(result[0]['description_score'], 100.)
        self.assertIsNone(result[0]['semantic_score'])

    def test_tenant_index_paths_are_isolated(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.matching.catalog.settings.SEMANTIC_INDEX_DIR', folder):
            self.assertNotEqual(index_path('a'), index_path('b'))
            self.assertEqual(index_path('../../a').parent.as_posix(), folder)

class PersistentIndexTests(unittest.TestCase):
    def test_restart_reuses_vectors_and_removed_parts_do_not_return(self):
        from app.matching import catalog as module
        rows = [('P1', 'pump'), ('P2', 'pump'), ('L1', 'lamp')]
        with tempfile.TemporaryDirectory() as folder, patch.object(module.settings, 'SEMANTIC_INDEX_DIR', folder), patch.object(module, 'SessionLocal'), patch.object(module, 'catalog', return_value=rows), patch.object(module.encoder, 'encode', return_value={'pump': np.array([1.,0.]), 'lamp': np.array([0.,1.])}) as encode:
            module.states['test'] = {'status': 'QUEUED', 'completed': 0}
            try:
                module.build('test')
                self.assertEqual(module.status('test')['status'], 'READY')
                self.assertEqual(module.status('test')['completed'], 2)
                encode.reset_mock()
                module.indexes.pop('test')
                module.build('test')
                encode.assert_not_called()
                with patch.object(module.encoder, 'encode', return_value={'pump': np.array([1.,0.])}):
                    results = module.hybrid_search([('L1','lamp')], '', 'pump', module.indexes['test'])
                self.assertNotIn('P1', [r['part_number'] for r in results])
            finally:
                module.states.pop('test', None)
                module.indexes.pop('test', None)
