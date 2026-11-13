from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from app.database import SessionLocal
from app.amos.sync import claim_sync, run_tenant_sync

scheduler = BackgroundScheduler()


def expire_broker_snapshots():
    from app.amos.central import receiver
    if receiver.routing is None:
        return
    with SessionLocal() as db:
        db.execute(text('''
            UPDATE public.amoscentral_batches SET status='EXPIRED', exports='{}'::jsonb
            WHERE status='PENDING' AND created_at < CURRENT_TIMESTAMP - make_interval(hours => :hours)
        '''), {'hours': receiver.routing.pending_batch_hours})
        db.commit()


def sync_all_tenants():
    with SessionLocal() as db:
        tenants = db.execute(text("""
            SELECT id FROM public.tenants
            WHERE amos_source = 'database' AND amos_host IS NOT NULL AND amos_host != ''
              AND last_sync_status != 'RUNNING'
              AND (COALESCE(sync_started_at, last_sync_time) IS NULL OR
                   COALESCE(sync_started_at, last_sync_time) < CURRENT_TIMESTAMP -
                   make_interval(hours => refresh_interval_hours))
        """)).fetchall()
        for (tenant_id,) in tenants:
            if claim_sync(db, tenant_id):
                run_tenant_sync(tenant_id)


def start_scheduler():
    # This app runs a single backend worker. Recover jobs interrupted by its restart.
    with SessionLocal() as db:
        db.execute(text("""
            UPDATE public.tenants SET last_sync_status = 'FAILED', sync_phase = 'Interrupted',
                sync_error = 'The server restarted during sync. Run Sync Now to retry.'
            WHERE last_sync_status = 'RUNNING'
        """))
        db.commit()
    scheduler.add_job(sync_all_tenants, 'interval', minutes=5, id='amos_sync_job', replace_existing=True)
    scheduler.add_job(expire_broker_snapshots, 'interval', minutes=5, id='amoscentral_cleanup', replace_existing=True)
    scheduler.start()
