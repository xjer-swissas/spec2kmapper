"""Exercise real PostgreSQL cache SQL in an isolated, rolled-back schema."""
import uuid
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.config import settings
from app.routers.amos_routes import browse_cache
from app.amos.extractor import AmosDataSyncer
from app.amos.csv_import import parse_export, prepare_exports
import io

engine = create_engine(settings.DATABASE_URL)
schema = 'smoke_' + uuid.uuid4().hex
with engine.connect() as conn:
    transaction = conn.begin()
    try:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'''CREATE TABLE "{schema}".amos_part_master (part_number TEXT, description TEXT);
            CREATE TABLE "{schema}".pn_part_of_pn_conf (parent_pn TEXT, child_pn TEXT, is_direct INT);
            CREATE TABLE "{schema}".fp_part_of_pn_conf (parent_pn TEXT, fp_code TEXT, fp_description TEXT, is_direct INT);
            CREATE TABLE "{schema}".doc_part_of_pn_conf (parent_pn TEXT, docno TEXT, is_direct INT);
            INSERT INTO "{schema}".amos_part_master VALUES ('A%1', 'Oil pump'), ('A_2', 'Cabin light');
            INSERT INTO "{schema}".pn_part_of_pn_conf VALUES ('ROOT', 'A%1', 1);
            INSERT INTO "{schema}".fp_part_of_pn_conf VALUES ('ROOT', 'FP1', 'Pump position', 1);
            INSERT INTO "{schema}".doc_part_of_pn_conf VALUES ('ROOT', 'DOC1', 1);
        '''))
        with Session(bind=conn) as db:
            user = {'schema_name': schema}
            for q, expected in [('pump', 'A%1'), ('%', 'A%1'), ('_', 'A_2')]:
                result = browse_cache('parts', q, 0, 50, user, db)
                assert result['total'] == 1, result
                assert result['items'][0]['part_number'] == expected
            result = browse_cache('parts', '', 1, 1, user, db)
            assert result['total'] == 2 and len(result['items']) == 1
            for kind in ('relationships', 'positions', 'documents'):
                assert browse_cache(kind, 'ROOT', 0, 50, user, db)['total'] == 1
            exports = prepare_exports(
                parse_export(io.BytesIO(b'part_number,description\n001,Root\n002,Child\n003,Leaf\n'), 'parts'),
                parse_export(io.BytesIO(b'parent_pn,fp_code,fp_description,child_pn\n001,F1,Root position,002\n002,F2,Child position,003\n'), 'configurations'),
                parse_export(io.BytesIO(b'pn,docno\n003,DOC1\n'), 'documents'))
            counts = AmosDataSyncer({}, schema, db).replace_cache(*exports, commit=False)
            assert counts == (3, 3, 3, 3), counts
            assert browse_cache('parts', '', 0, 50, user, db)['total'] == 3
            assert browse_cache('parts', 'A%', 0, 50, user, db)['total'] == 0
            docs = browse_cache('documents', '001', 0, 50, user, db)['items']
            assert docs == [{'parent_pn': '001', 'docno': 'DOC1', 'is_direct': 0}], docs
    finally:
        transaction.rollback()
print('PostgreSQL smoke passed: descriptions, literal searches, pagination, all cache categories. Test schema rolled back.')
