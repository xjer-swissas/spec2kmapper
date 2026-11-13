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

    def test_schema_leading_zeros_and_reordered_columns(self):
        kind, rows = parse_data_source(export('parts', ['DESCRIPTION', 'PART_NUMBER'], [['Pümp', '001']]), self.ids)
        self.assertEqual((kind, rows), ('parts', [{'description': 'Pümp', 'part_number': '001'}]))

    def test_empty_optional_exports_and_nonempty_master(self):
        self.assertEqual(parse_data_source(export('documents', ['pn', 'docno'], []), self.ids), ('documents', []))
        with self.assertRaises(ValueError):
            parse_data_source(export('parts', ['part_number', 'description'], []), self.ids)

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
