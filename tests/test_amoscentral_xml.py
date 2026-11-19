import unittest

from app.amos.xml_import import parse_data_source


def export(kind, columns, rows):
    from lxml import etree
    root = etree.Element('exportDataSource', version='1.0')
    source = etree.SubElement(root, 'dataSource')
    etree.SubElement(source, 'dataSourceId').text = kind
    header = etree.SubElement(source, 'header')
    for name in columns:
        cell = etree.SubElement(header, 'cellDefinition')
        for tag, value in [('name', name), ('description', ''), ('amosType', 'D_TEXT')]:
            etree.SubElement(cell, tag).text = value
    for values in rows:
        row = etree.SubElement(source, 'row')
        for value in values:
            etree.SubElement(row, 'value').text = value
    return etree.tostring(root)


class XmlImportTests(unittest.TestCase):
    ids = {'parts': 'parts', 'configurations': 'configurations', 'documents': 'documents'}

    def test_transport_envelope_payload_is_validated_and_imported(self):
        from lxml import etree
        body = export('documents', ['pn', 'docno'], [['001', 'DOC1']])
        wrapper = etree.Element('amosTransportEnvelope', version='1.0')
        etree.SubElement(wrapper, 'header')
        payload = etree.SubElement(wrapper, 'payload', type='exportDataSource', version='1.0')
        payload.append(etree.fromstring(body))
        self.assertEqual(parse_data_source(etree.tostring(wrapper), self.ids),
                         ('documents', [{'pn': '001', 'docno': 'DOC1'}]))
        payload[0].set('version', '2.0')
        with self.assertRaisesRegex(ValueError, 'Invalid exportDataSource'):
            parse_data_source(etree.tostring(wrapper), self.ids)

    def test_transport_envelope_rejects_missing_or_ambiguous_payload(self):
        from lxml import etree
        wrapper = etree.Element('amosTransportEnvelope')
        payload = etree.SubElement(wrapper, 'payload')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            parse_data_source(etree.tostring(wrapper), self.ids)
        body = export('documents', ['pn', 'docno'], [])
        payload.extend([etree.fromstring(body), etree.fromstring(body)])
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            parse_data_source(etree.tostring(wrapper), self.ids)

    def test_custom_header_selects_export_independently_of_xml_id(self):
        from app.amos.central_config import RoutingConfig
        routing = RoutingConfig(data_source_ids=self.ids,
                                export_type_header='amc_2k5mapper_product_header')
        for value, kind, columns, rows in [
            ('part_definition', 'parts', ['part_number', 'description'], [['001', 'Pump']]),
            ('configuration', 'configurations', ['parent_pn', 'fp_code', 'fp_description', 'child_pn'], []),
            ('documents', 'documents', ['pn', 'docno'], []),
        ]:
            with self.subTest(value=value):
                selected = routing.export_kind({'amc_2k5mapper_product_header': value})
                actual, _ = parse_data_source(export('sender-export-id', columns, rows),
                                              self.ids, export_kind=selected)
                self.assertEqual(actual, kind)
        for headers in [{}, {'amc_2k5mapper_product_header': 'unknown'}]:
            with self.assertRaises(ValueError):
                routing.export_kind(headers)

    def test_schema_leading_zeros_and_reordered_columns(self):
        kind, rows = parse_data_source(export('parts', ['DESCRIPTION', 'PART_NUMBER'], [['Pümp', '001']]), self.ids)
        self.assertEqual((kind, rows), ('parts', [{'description': 'Pümp', 'part_number': '001'}]))

    def test_empty_exports_are_valid(self):
        self.assertEqual(parse_data_source(export('documents', ['pn', 'docno'], []), self.ids), ('documents', []))
        self.assertEqual(parse_data_source(export('parts', ['part_number', 'description'], []), self.ids), ('parts', []))

    def test_validation_error_identifies_xml_location_and_column_problem(self):
        with self.assertRaisesRegex(ValueError, 'line'):
            parse_data_source(b'<exportDataSource>', self.ids)
        with self.assertRaisesRegex(ValueError, 'expected headers'):
            parse_data_source(export('parts', ['wrong'], [['001']]), self.ids)

    def test_invalid_xml_columns_entities_version_and_unknown_source(self):
        valid = export('parts', ['part_number', 'description'], [['001', 'Pump']])
        for data in [valid.replace(b'version="1.0"', b'version="2.0"'),
                     export('unknown', ['part_number', 'description'], [['001', 'Pump']]),
                     export('parts', ['part_number', 'part_number'], [['001', 'Pump']]),
                     export('parts', ['part_number', 'description'], [['001']]),
                     b'<!DOCTYPE exportDataSource [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + valid,
                     b'<exportDataSource>']:
            with self.subTest(data=data[:70]), self.assertRaises(ValueError):
                parse_data_source(data, self.ids)
