from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_tenant_db(tenant_schema: str):
    """Provides a session bound strictly to the tenant's isolated PostgreSQL schema."""
    db = SessionLocal()
    try:
        # Schema-level physical barrier:
        db.execute(text(f'SET search_path TO "{tenant_schema}", public;'))
        yield db
    finally:
        db.close()

def init_master_tables():
    """Initializes public system tables (Tenants, Users)."""
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS public.tenants (
                id SERIAL PRIMARY KEY,
                company_name VARCHAR(255) UNIQUE NOT NULL,
                schema_name VARCHAR(63) UNIQUE NOT NULL,
                amos_host VARCHAR(255),
                amos_port INT DEFAULT 5432,
                amos_dbname VARCHAR(255),
                amos_user VARCHAR(255),
                amos_password VARCHAR(255),
                refresh_interval_hours INT DEFAULT 24,
                last_sync_status VARCHAR(50) DEFAULT 'IDLE',
                last_sync_time TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS public.users (
                id SERIAL PRIMARY KEY,
                tenant_id INT REFERENCES public.tenants(id) ON DELETE CASCADE,
                username VARCHAR(100) UNIQUE NOT NULL,
                hashed_password VARCHAR(255) NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        conn.execute(text("""
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS amos_source TEXT NOT NULL DEFAULT 'database';
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS sync_phase TEXT;
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS sync_error TEXT;
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS sync_started_at TIMESTAMP;
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS reranker TEXT NOT NULL DEFAULT 'none';
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS ranking_mode TEXT NOT NULL DEFAULT 'weighted';
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS collect_confirmed_mappings BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS tuning_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS pn_weight FLOAT NOT NULL DEFAULT 0.25;
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS amoscentral_last_snapshot_at TIMESTAMPTZ;
            ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS amoscentral_address VARCHAR(64);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_amoscentral_address
                ON public.tenants(amoscentral_address) WHERE amoscentral_address IS NOT NULL;
            CREATE TABLE IF NOT EXISTS public.amoscentral_batches (
                tenant_id INT NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
                batch_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                snapshot_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'PENDING',
                exports JSONB NOT NULL DEFAULT '{}',
                digests JSONB NOT NULL DEFAULT '{}',
                PRIMARY KEY (tenant_id, sender, batch_id)
            );
            CREATE TABLE IF NOT EXISTS public.amoscentral_receipts (
                message_id TEXT PRIMARY KEY,
                tenant_id INT REFERENCES public.tenants(id) ON DELETE CASCADE,
                sender TEXT NOT NULL,
                digest TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_amoscentral_pending
                ON public.amoscentral_batches(tenant_id, created_at) WHERE status = 'PENDING';
            CREATE TABLE IF NOT EXISTS public.confirmed_mappings (
                tenant_id INT NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
                root_pn TEXT NOT NULL, source_pn TEXT NOT NULL, source_description TEXT NOT NULL,
                target_pn TEXT, candidates JSONB NOT NULL,
                confirmed_by INT REFERENCES public.users(id),
                confirmed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, root_pn, source_pn, source_description)
            );
        """))
        conn.commit()

def create_tenant_schema(schema_name: str):
    """Provisions an isolated PostgreSQL schema and table structure for a new customer."""
    with engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}";'))
        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS "{schema_name}".amos_part_master (
                part_number VARCHAR(255) PRIMARY KEY,
                description TEXT
            );

            CREATE TABLE IF NOT EXISTS "{schema_name}".pn_part_of_pn_conf (
                parent_pn VARCHAR(255) NOT NULL,
                child_pn VARCHAR(255) NOT NULL,
                is_direct INT NOT NULL,
                PRIMARY KEY (parent_pn, child_pn)
            );

            CREATE TABLE IF NOT EXISTS "{schema_name}".fp_part_of_pn_conf (
                parent_pn VARCHAR(255) NOT NULL,
                fp_code VARCHAR(255) NOT NULL,
                fp_description TEXT,
                is_direct INT NOT NULL,
                PRIMARY KEY (parent_pn, fp_code)
            );

            CREATE TABLE IF NOT EXISTS "{schema_name}".doc_part_of_pn_conf (
                parent_pn VARCHAR(255) NOT NULL,
                docno VARCHAR(255) NOT NULL,
                is_direct INT NOT NULL,
                PRIMARY KEY (parent_pn, docno)
            );

            CREATE INDEX IF NOT EXISTS idx_{schema_name}_pn_parent ON "{schema_name}".pn_part_of_pn_conf(parent_pn);
            CREATE INDEX IF NOT EXISTS idx_{schema_name}_fp_parent ON "{schema_name}".fp_part_of_pn_conf(parent_pn);
            CREATE INDEX IF NOT EXISTS idx_{schema_name}_doc_parent ON "{schema_name}".doc_part_of_pn_conf(parent_pn);
        '''))
        conn.commit()
