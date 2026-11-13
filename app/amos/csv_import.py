"""Validate customer exports before modifying the tenant cache."""
import csv
import io
from collections import defaultdict
from app.amos.queries import EXPORTS

MAX_FILE_BYTES = 25 * 1024 * 1024


def parse_export(stream, kind):
    raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f'{kind}: file exceeds 25 MB')
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError(f'{kind}: export the CSV as UTF-8') from None
    if '\x00' in content:
        raise ValueError(f'{kind}: CSV contains a null character')
    columns = EXPORTS[kind]['columns']
    reader = csv.reader(io.StringIO(content, newline=''), strict=True)
    rows = []
    try:
        header = next(reader, [])
        header = [cell.strip().lower() for cell in header]
        if len(header) != len(columns) or set(header) != set(columns):
            raise ValueError(f'{kind}: expected CSV headers: {", ".join(columns)}')
        for values in reader:
            if not values:
                continue
            if len(values) != len(header):
                raise ValueError(f'{kind}: line {reader.line_num} has the wrong number of columns')
            row = dict(zip(header, (value.strip() for value in values)))
            required = {'parts': ['part_number'], 'configurations': ['parent_pn', 'child_pn'], 'documents': ['pn', 'docno']}[kind]
            for key in required:
                if not row[key]:
                    raise ValueError(f'{kind}: line {reader.line_num} requires {key}')
            for key, value in row.items():
                if 'description' not in key and len(value) > 255:
                    raise ValueError(f'{kind}: line {reader.line_num}, {key} exceeds 255 characters')
            rows.append(row)
    except csv.Error as exc:
        raise ValueError(f'{kind}: invalid CSV near line {reader.line_num}: {exc}') from None
    if kind == 'parts' and not rows:
        raise ValueError('parts: at least one part is required')
    return rows


def prepare_exports(parts, configurations, documents):
    master = {}
    for row in parts:
        pn, description = row['part_number'], row['description']
        if pn in master and master[pn] != description:
            raise ValueError(f'parts: conflicting descriptions for {pn}')
        master[pn] = description
    children, positions, docs = defaultdict(set), defaultdict(dict), defaultdict(set)
    for row in configurations:
        parent = row['parent_pn']
        children[parent].add(row['child_pn'])
        if row['fp_code']:
            positions[parent][row['fp_code']] = row['fp_description']
    for row in documents:
        docs[row['pn']].add(row['docno'])
    return list(master.items()), children, positions, docs
