import unittest
from unittest.mock import Mock
import numpy as np
from app.matching.parts import PartMatcher
from app.matching.fuzzy import RapidFuzzMatcher
from app.spec2500.parser import Spec2500Parser


class MatchingTests(unittest.TestCase):
    def test_meaning_can_find_a_different_number(self):
        encoder = Mock()
        encoder.encode.return_value = {
            'lubrication fluid delivery unit': np.array([1., 0.]),
            'oil pump': np.array([1., 0.]),
            'cabin light': np.array([0., 1.]),
        }
        matches = PartMatcher(encoder).match_many(
            ['ABC'], [('XYZ', 'oil pump'), ('ABD', 'cabin light')],
            {'ABC': 'lubrication fluid delivery unit'},
        )['ABC']
        self.assertEqual(matches[0]['candidate'], 'XYZ')
        self.assertEqual(matches[0]['match_type'], 'SEMANTIC_DESCRIPTION')
        self.assertTrue(matches[0]['requires_review'])
        self.assertEqual(matches[0]['semantic_score'], 100.)

    def test_exact_number_beats_description_and_skips_inference(self):
        encoder = Mock()
        matches = PartMatcher(encoder).match_many(
            ['ABC'], [('ABC', 'old name'), ('XYZ', 'oil pump')], {'ABC': 'oil pump'},
        )['ABC']
        self.assertEqual(matches[0]['candidate'], 'ABC')
        self.assertEqual(matches[0]['score'], 100.)
        self.assertFalse(matches[0]['requires_review'])
        encoder.encode.assert_not_called()

    def test_missing_model_still_uses_descriptions(self):
        encoder = Mock()
        encoder.encode.return_value = {}
        matches = PartMatcher(encoder).match_many(
            ['ABC'], [('XYZ', 'pump oil'), ('DEF', 'cabin light')], {'ABC': 'oil pump'},
        )['ABC']
        self.assertEqual(matches[0]['candidate'], 'XYZ')
        self.assertEqual(matches[0]['match_type'], 'DESCRIPTION')
        self.assertIsNone(matches[0]['semantic_score'])

    def test_normalization_requires_review_and_not_empty_strings(self):
        encoder = Mock()
        match = PartMatcher(encoder).match_many(['A-1'], [('A1', '')])['A-1'][0]
        self.assertEqual(match['match_type'], 'EXACT_NORMALIZED')
        self.assertTrue(match['requires_review'])
        self.assertNotEqual(RapidFuzzMatcher().match_item('---', ['///'])[0]['match_type'], 'EXACT_NORMALIZED')

    def test_description_extraction_does_not_use_parent_description(self):
        xml = b'''<Crate><PartDetails>
          <SpecificUnitInformation><PartNumber>A</PartNumber><PartDescription>Oil <b>pump</b></PartDescription></SpecificUnitInformation>
          <NextHigherAssemblyInformation><EngineInfo><PartNumber>ROOT</PartNumber><Description>Engine</Description></EngineInfo></NextHigherAssemblyInformation>
        </PartDetails></Crate>'''
        data = Spec2500Parser(xml).extract_assembly_and_elements()
        self.assertEqual(data['pn_descriptions']['A'], 'Oil pump')
        self.assertEqual(data['pn_descriptions']['ROOT'], 'Engine')
