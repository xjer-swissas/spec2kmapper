"""Shared background sync lifecycle for manual and scheduled refreshes."""
import logging
from sqlalchemy import text
from app.database import SessionLocal
from app.amos.extractor import AmosDataSyncer

logger = logging.getLogger(__name__)


def claim_sync(db, tenant_id):
    claimed = db.execute(text("""
        UPDATE public.tenants SET last_sync_status = 'RUNNING',
            sync_phase = 'Queued', sync_error = NULL, sync_started_at = CURRENT_TIMESTAMP
        WHERE id = :id AND last_sync_status != 'RUNNING' AND amos_source = 'database'
        RETURNING id
    """), {"id": tenant_id}).fetchone()
    db.commit()
    return bool(claimed)


def set_progress(tenant_id, phase):
    with SessionLocal() as db:
        db.execute(text("UPDATE public.tenants SET sync_phase = :phase WHERE id = :id"),
                   {"phase": phase, "id": tenant_id})
        db.commit()


def run_tenant_sync(tenant_id):
    with SessionLocal() as db:
        try:
            row = db.execute(text("""
                SELECT schema_name, amos_host, amos_port, amos_dbname, amos_user, amos_password
                FROM public.tenants WHERE id = :id
            """), {"id": tenant_id}).fetchone()
            if not row:
                return
            schema, host, port, name, user, password = row
            config = dict(host=host, port=port or 5432, dbname=name, user=user,
                          password=password, connect_timeout=15,
                          options='-c search_path=amos,public -c statement_timeout=600000')
            syncer = AmosDataSyncer(config, schema, db)
            counts = syncer.run_sync(progress=lambda phase: set_progress(tenant_id, phase), commit=False)
            db.execute(text("""
                UPDATE public.tenants SET last_sync_status = 'SUCCESS',
                    last_sync_time = CURRENT_TIMESTAMP, sync_error = NULL, sync_phase = :phase
                WHERE id = :id
            """), {"id": tenant_id, "phase":
                    f'Complete: {counts[0]} parts, {counts[1]} part links, {counts[2]} positions, {counts[3]} documents'})
            db.commit()
            from app.matching.catalog import schedule
            schedule(schema)
        except Exception:
            db.rollback()
            logger.exception("AMOS sync failed for tenant %s", tenant_id)
            # Connection errors can contain connection details. Keep those in server logs.
            db.execute(text("""
                UPDATE public.tenants SET last_sync_status = 'FAILED', sync_phase = 'Failed',
                    sync_error = 'Sync failed. Check the AMOS connection settings and server logs. The previous cache was retained.'
                WHERE id = :id
            """), {"id": tenant_id})
            db.commit()
