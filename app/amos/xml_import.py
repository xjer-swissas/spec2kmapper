from pathlib import Path
from lxml import etree

from app.amos.csv_import import validate_table, MAX_FILE_BYTES


def parse_data_source(body, data_source_ids):
    if len(body) > MAX_FILE_BYTES:
        raise ValueError('XML export exceeds 25 MB')
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.fromstring(body, parser)
        if root.getroottree().docinfo.doctype:
            raise ValueError('XML document types are not supported')
        schema = etree.XMLSchema(etree.parse(str(Path(__file__).parent / 'schemas' / 'exportDataSource_1.0.xsd')))
        if not schema.validate(root):
            raise ValueError('Invalid exportDataSource 1.0 XML')
        source = root.find('dataSource')
        source_id = source.findtext('dataSourceId')
        kinds = [kind for kind, value in data_source_ids.items() if value == source_id]
        if len(kinds) != 1:
            raise ValueError('Unknown dataSourceId')
        kind = kinds[0]
        columns = [c.findtext('name') or '' for c in source.findall('header/cellDefinition')]
        rows = validate_table(columns, ([v.text or '' for v in row.findall('value')]
                                        for row in source.findall('row')), kind)
        return kind, rows
    except etree.XMLSyntaxError:
        raise ValueError('Invalid XML export') from None
