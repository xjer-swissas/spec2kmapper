from pathlib import Path
from lxml import etree

from app.amos.csv_import import validate_table


def parse_data_source(body, data_source_ids, *, export_kind=None):
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.fromstring(body, parser)
        if root.getroottree().docinfo.doctype:
            raise ValueError('XML document types are not supported')
        if etree.QName(root).localname == 'amosTransportEnvelope':
            exports = root.xpath('./*[local-name()="payload"]/*[local-name()="exportDataSource"]')
            if len(exports) != 1:
                raise ValueError('amosTransportEnvelope must contain exactly one payload/exportDataSource')
            root = exports[0]
        schema = etree.XMLSchema(etree.parse(str(Path(__file__).parent / 'schemas' / 'exportDataSource_1.0.xsd')))
        if not schema.validate(root):
            errors = '; '.join(f'line {e.line}: {e.message}' for e in schema.error_log)
            raise ValueError('Invalid exportDataSource 1.0 XML: ' + errors)
        source = root.find('dataSource')
        source_id = source.findtext('dataSourceId')
        kinds = ([export_kind] if export_kind is not None else
                 [kind for kind, value in data_source_ids.items() if value == source_id])
        if len(kinds) != 1:
            raise ValueError('Unknown dataSourceId')
        kind = kinds[0]
        columns = [c.findtext('name') or '' for c in source.findall('header/cellDefinition')]
        rows = validate_table(columns, ([v.text or '' for v in row.findall('value')]
                                        for row in source.findall('row')), kind, allow_empty_master=True)
        return kind, rows
    except etree.XMLSyntaxError as exc:
        raise ValueError(f'Invalid XML export at line {exc.lineno}, column {exc.position[1]}: {exc.msg}') from None
