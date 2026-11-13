import unittest
from unittest.mock import Mock, patch

from app.matching.reranker import CrossEncoder, rerank_candidates
from app.matching.parts import PartMatcher
from app.matching.catalog import hybrid_search


def item(pn, description='text', kind='FUZZY'):
    return dict(candidate=pn, description=description, match_type=kind, score=80, requires_review=kind!='EXACT')


class RerankerTests(unittest.TestCase):
    @patch('app.matching.reranker.cross_encoder.score', return_value=[-2., 3.])
    def test_fixed_slots_priority_and_evidence(self, score):
        items=[item('exact',kind='EXACT'),item('a'),item('missing',''),item('normalized',kind='EXACT_NORMALIZED'),item('b')]
        result=rerank_candidates(items,'query','cross_encoder')
        self.assertEqual([m['candidate'] for m in result],['exact','b','missing','normalized','a'])
        self.assertEqual(result[1]['cross_encoder_score'],3.)
        self.assertEqual(result[1]['score'],80)
        self.assertTrue(result[1]['requires_review'])
        self.assertNotIn('cross_encoder_score',items[1])
        score.assert_called_once_with('query',['text','text'])

    @patch('app.matching.reranker.cross_encoder.score')
    def test_disabled_missing_query_and_single_eligible_skip_inference(self, score):
        items=[item('a'),item('b')]
        self.assertEqual(rerank_candidates(items,'query'),items)
        self.assertEqual(rerank_candidates(items,'  ','cross_encoder'),items)
        self.assertEqual(rerank_candidates(items[:1],'query','cross_encoder'),items[:1])
        score.assert_not_called()

    @patch('app.matching.reranker.settings.RERANK_TOP_N',2)
    @patch('app.matching.reranker.cross_encoder.score',return_value=[1.,1.])
    def test_bounded_and_stable_ties(self, score):
        items=[item('a'),item('b'),item('c')]
        result=rerank_candidates(items,'query','cross_encoder')
        self.assertEqual([m['candidate'] for m in result],['a','b','c'])
        self.assertNotIn('cross_encoder_score',result[2])
        self.assertEqual(len(score.call_args.args[1]),2)

    @patch('app.matching.reranker.cross_encoder.score',side_effect=RuntimeError('offline'))
    def test_failure_keeps_order_and_reports_warning(self, score):
        result=rerank_candidates([item('a'),item('b')],'query','cross_encoder')
        self.assertEqual([m['candidate'] for m in result],['a','b'])
        self.assertIn('reranker_warning',result[0])

    def test_invalid_model_output_and_retry(self):
        for values in ([float('nan'),1], [1]):
            scorer=CrossEncoder()
            scorer.model=Mock()
            scorer.model.rerank.return_value=iter(values)
            with self.assertLogs('app.matching.reranker',level='ERROR'):
                with self.assertRaises(ValueError): scorer.score('q',['a','b'])
            with self.assertRaises(RuntimeError): scorer.score('q',['a','b'])
            self.assertEqual(scorer.model.rerank.call_count,1)

    @patch('app.matching.reranker.cross_encoder.score',return_value=[-1.,2.])
    def test_part_modes_rerank_before_top_k(self, score):
        enc=Mock();enc.encode.return_value={}
        for mode in ('weighted','rrf'):
            result=PartMatcher(enc).match_many(['ABC'], [('ABD','pump'),('ZZZ','lamp')],
                {'ABC':'unit'},top_k=1,ranking_mode=mode,reranker='cross_encoder')
            self.assertEqual(result['ABC'][0]['candidate'],'ZZZ')

    @patch('app.matching.reranker.cross_encoder.score',return_value=[-1.,2.])
    def test_catalog_uses_reranker_and_preserves_exact(self, score):
        result=hybrid_search([('A','pump'),('B','pump'),('C','pump')], 'A','pump',None,reranker='cross_encoder')
        self.assertEqual([m['part_number'] for m in result],['A','C','B'])
        self.assertEqual(result[0]['match_type'],'EXACT PN')
