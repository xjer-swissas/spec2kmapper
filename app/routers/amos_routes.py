from typing import Literal
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, File, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.auth import get_current_user
from app.amos.sync import claim_sync, run_tenant_sync
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/amos", tags=["AMOS Settings"])


@router.get('/part-search')
def search_parts(q: str = Query('', max_length=255), description: str = Query('', max_length=1000),
                 root: str = '', user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.matching.catalog import search
    return search(db, user['schema_name'], q.strip(), description.strip(), root)


@router.post('/index')
def rebuild_index(user: dict = Depends(get_current_user)):
    from app.matching.catalog import schedule, status
    schedule(user['schema_name'])
    return status(user['schema_name'])


class AmosConnectionConfig(BaseModel):
    source: Literal["database", "csv"] = "database"
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str
    user: str
    password: str = ""
    refresh_interval_hours: int = Field(default=24, ge=1)


@router.get('/config')
def get_amos_config(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.execute(text('''
        SELECT amos_host, amos_port, amos_dbname, amos_user, refresh_interval_hours, amos_source
        FROM public.tenants WHERE id = :id
    '''), {'id': user['tenant_id']}).fetchone()
    if not row:
        raise HTTPException(404, 'Tenant not found')
    return dict(host=row[0] or '', port=row[1] or 5432, dbname=row[2] or '',
                user=row[3] or '', password='', refresh_interval_hours=row[4] or 24, source=row[5])


@router.post('/config')
def update_amos_config(cfg: AmosConnectionConfig, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    updated = db.execute(text('''
        UPDATE public.tenants SET amos_source=:source, amos_host=:h, amos_port=:p, amos_dbname=:d, amos_user=:u,
            amos_password=COALESCE(NULLIF(:pwd, ''), amos_password), refresh_interval_hours=:r
        WHERE id=:id AND last_sync_status != 'RUNNING'
        RETURNING id
    '''), dict(source=cfg.source, h=cfg.host, p=cfg.port, d=cfg.dbname, u=cfg.user, pwd=cfg.password,
               r=cfg.refresh_interval_hours, id=user['tenant_id'])).fetchone()
    if not updated:
        db.rollback()
        raise HTTPException(409, 'Wait for the current refresh to finish before changing the data source')
    db.commit()
    return {'message': 'AMOS connection settings saved'}


@router.post('/sync', status_code=202)
def trigger_manual_sync(background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.execute(text("SELECT amos_host FROM public.tenants WHERE id=:id AND amos_source = 'database'"),
                     {'id': user['tenant_id']}).fetchone()
    if not row or not row[0]:
        raise HTTPException(400, 'Select Direct database and save AMOS connection settings first')
    if not claim_sync(db, user['tenant_id']):
        return {'status': 'RUNNING', 'message': 'A sync is already running'}
    background_tasks.add_task(run_tenant_sync, user['tenant_id'])
    return {'status': 'RUNNING', 'message': 'Sync queued'}


@router.get('/status')
def get_sync_status(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.execute(text('''
        SELECT last_sync_status, sync_phase, sync_error, sync_started_at, last_sync_time
        FROM public.tenants WHERE id=:id
    '''), {'id': user['tenant_id']}).fetchone()
    if not row:
        raise HTTPException(404, 'Tenant not found')
    s = user['schema_name']
    counts = {}
    for kind, table in [('parts', 'amos_part_master'), ('relationships', 'pn_part_of_pn_conf'),
                        ('positions', 'fp_part_of_pn_conf'), ('documents', 'doc_part_of_pn_conf')]:
        counts[kind] = db.execute(text(f'SELECT count(*) FROM "{s}".{table}')).scalar_one()
    from app.matching.catalog import status
    return dict(status=row[0], phase=row[1], error=row[2], started_at=row[3],
                last_success_at=row[4], counts=counts, index=status(s))


@router.get('/cache')
def browse_cache(kind: Literal['parts', 'relationships', 'positions', 'documents'] = 'parts',
                 q: str = '', offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                 user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    # SQL identifiers come exclusively from this allowlist; search text is bound.
    definitions = {
        'parts': ('amos_part_master', ['part_number', 'description']),
        'relationships': ('pn_part_of_pn_conf', ['parent_pn', 'child_pn', 'is_direct']),
        'positions': ('fp_part_of_pn_conf', ['parent_pn', 'fp_code', 'fp_description', 'is_direct']),
        'documents': ('doc_part_of_pn_conf', ['parent_pn', 'docno', 'is_direct']),
    }
    table, columns = definitions[kind]
    source = f'"{user["schema_name"]}".{table}'
    where = ' OR '.join(f'CAST({c} AS TEXT) ILIKE :q' for c in columns if c != 'is_direct')
    escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    params = dict(q=f'%{escaped}%', offset=offset, limit=limit)
    total = db.execute(text(f'SELECT count(*) FROM {source} WHERE {where}'), params).scalar_one()
    rows = db.execute(text(f'SELECT {", ".join(columns)} FROM {source} WHERE {where} '
                           f'ORDER BY {", ".join(columns[:2])} LIMIT :limit OFFSET :offset'), params).fetchall()
    return dict(items=[dict(zip(columns, row)) for row in rows], total=total,
                columns=columns, offset=offset, limit=limit)


@router.get('/csv/queries')
def csv_queries(user: dict = Depends(get_current_user)):
    from app.amos.queries import EXPORTS
    return EXPORTS


@router.post('/csv/import')
def import_csv(parts: UploadFile = File(...), configurations: UploadFile = File(...),
               documents: UploadFile = File(...), user: dict = Depends(get_current_user),
               db: Session = Depends(get_db)):
    from app.amos.csv_import import parse_export, prepare_exports
    from app.amos.extractor import AmosDataSyncer
    try:
        data = prepare_exports(*(parse_export(upload.file, kind) for kind, upload in
                                 [('parts', parts), ('configurations', configurations), ('documents', documents)]))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    # Lock and replace in one transaction. A concurrent sync cannot claim this tenant.
    try:
        row = db.execute(text('SELECT last_sync_status FROM public.tenants WHERE id=:id FOR UPDATE'),
                         {'id': user['tenant_id']}).fetchone()
        if not row:
            raise HTTPException(404, 'Tenant not found')
        if row[0] == 'RUNNING':
            raise HTTPException(409, 'A refresh is running. Wait until it finishes before importing CSV files.')
        counts = AmosDataSyncer({}, user['schema_name'], db).replace_cache(*data, commit=False)
        db.execute(text("""
            UPDATE public.tenants SET amos_source='csv', last_sync_status='SUCCESS',
                last_sync_time=CURRENT_TIMESTAMP, sync_started_at=CURRENT_TIMESTAMP,
                sync_error=NULL, sync_phase='CSV import complete'
            WHERE id=:id
        """), {'id': user['tenant_id']})
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        import logging
        logging.getLogger(__name__).exception('CSV import failed for tenant %s', user['tenant_id'])
        raise HTTPException(500, 'CSV import failed. The previous cache was retained.') from None
    from app.matching.catalog import schedule
    schedule(user['schema_name'])
    return {'message': 'CSV import complete. Automatic database refresh is disabled.',
            'counts': dict(zip(['parts', 'relationships', 'positions', 'documents'], counts))}
