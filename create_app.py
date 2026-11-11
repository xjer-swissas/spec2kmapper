import base64
import io
import os
import zipfile

FILES = {}

# -----------------------------------------------------------------------------
# requirements.txt
# -----------------------------------------------------------------------------
FILES["requirements.txt"] = """
fastapi==0.115.0
uvicorn[standard]==0.31.0
pydantic==2.9.2
pydantic-settings==2.5.2
psycopg2-binary==2.9.9
SQLAlchemy==2.0.35
alembic==1.13.3
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
python-multipart==0.0.12
apscheduler==3.10.4
rapidfuzz==3.9.7
lxml==5.3.0
requests==2.32.3
"""

# -----------------------------------------------------------------------------
# Dockerfile & docker-compose.yml
# -----------------------------------------------------------------------------
FILES["Dockerfile"] = """
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    libpq-dev \\
    libxml2-dev \\
    libxslt1-dev \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

FILES["docker-compose.yml"] = """
services:
  postgres:
    image: postgres:18-alpine
    container_name: amos_postgres_app
    environment:
      POSTGRES_USER: app_admin
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD}
      POSTGRES_DB: amos_app_db
    ports:
      - "5433:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app_admin -d amos_app_db"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  backend:
    build: .
    container_name: amos_backend_app
    environment:
      DATABASE_URL: postgresql://app_admin:${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD}@postgres:5432/amos_app_db
      SECRET_KEY: ${SECRET_KEY:?Set SECRET_KEY}
      ACCESS_TOKEN_EXPIRE_MINUTES: 1440
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - ./uploads:/app/uploads
    restart: unless-stopped

volumes:
  pgdata:
"""

# -----------------------------------------------------------------------------
# app/config.py
# -----------------------------------------------------------------------------
FILES["app/__init__.py"] = ""

FILES["app/config.py"] = """
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440 # 24 hours
    UPLOAD_DIR: str = "uploads"

    class Config:
        env_file = ".env"

settings = Settings()
"""

# -----------------------------------------------------------------------------
# app/database.py
# -----------------------------------------------------------------------------
FILES["app/database.py"] = """
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
    \"\"\"Provides a session bound strictly to the tenant's isolated PostgreSQL schema.\"\"\"
    db = SessionLocal()
    try:
        # Schema-level physical barrier:
        db.execute(text(f'SET search_path TO "{tenant_schema}", public;'))
        yield db
    finally:
        db.close()

def init_master_tables():
    \"\"\"Initializes public system tables (Tenants, Users).\"\"\"
    with engine.connect() as conn:
        conn.execute(text(\"\"\"
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
        \"\"\"))
        conn.commit()

def create_tenant_schema(schema_name: str):
    \"\"\"Provisions an isolated PostgreSQL schema and table structure for a new customer.\"\"\"
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
"""

# -----------------------------------------------------------------------------
# app/auth.py
# -----------------------------------------------------------------------------
FILES["app/auth.py"] = """
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.config import settings
from app.database import get_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        tenant_id: int = payload.get("tenant_id")
        schema_name: str = payload.get("schema_name")
        if username is None or tenant_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.execute(
        text("SELECT id, tenant_id, username, is_admin FROM public.users WHERE username = :u"),
        {"u": username}
    ).fetchone()

    if user is None:
        raise credentials_exception

    return {
        "id": user[0],
        "tenant_id": user[1],
        "username": user[2],
        "is_admin": user[3],
        "schema_name": schema_name
    }
"""

# -----------------------------------------------------------------------------
# app/amos/extractor.py
# -----------------------------------------------------------------------------
FILES["app/amos/__init__.py"] = ""

FILES["app/amos/extractor.py"] = """
import psycopg2
import psycopg2.extras
from collections import defaultdict, deque
from typing import Dict, List, Set, Tuple
from sqlalchemy import text
from sqlalchemy.orm import Session

class AmosDataSyncer:
    def __init__(self, amos_pg_config: dict, tenant_schema: str, app_db_session: Session):
        self.amos_pg_config = amos_pg_config
        self.tenant_schema = tenant_schema
        self.db = app_db_session

    def fetch_amos_all_master_parts(self) -> List[Tuple[str, str]]:
        query = \"\"\"
            SELECT DISTINCT p.partno AS part_number, p.description AS description
            FROM amos.part p
            WHERE p.partno IS NOT NULL
        \"\"\"
        records = []
        with psycopg2.connect(**self.amos_pg_config) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute(query)
                for row in cursor.fetchall():
                    pn = row["part_number"].strip() if row["part_number"] else ""
                    desc = row["description"].strip() if row["description"] else ""
                    if pn:
                        records.append((pn, desc))
        return records

    def fetch_amos_pa_configurations(self) -> Tuple[Dict[str, Set[str]], Dict[str, Dict[str, str]]]:
        query = \"\"\"
            SELECT DISTINCT
                ee.partno          AS parent_pn,
                cfh.code           AS fp_code,
                cfh.description    AS fp_description,
                ccrp.part_number   AS child_pn
            FROM amos.conf_config cc
            INNER JOIN amos.event_effectivity_link eel  ON eel.event_key = cc.conf_configno_i AND eel.event_type = 'ACCONF'
            INNER JOIN amos.event_effectivity ee        ON ee.effectivityno_i = eel.effectivityno_i
            INNER JOIN amos.conf_fp_header cfh          ON cfh.conf_configno_i = cc.conf_configno_i
            INNER JOIN amos.conf_fp cf                  ON cf.conf_fp_headerno_i = cfh.conf_fp_headerno_i
            INNER JOIN amos.event_effectivity_link eel2 ON eel2.event_key = cf.conf_fpno_i AND eel2.event_type = 'ACLOCID'
            INNER JOIN amos.conf_config_rule ccr        ON ccr.event_key = eel2.effectivity_linkno_i AND ccr.event_type = 'EFL'
            INNER JOIN amos.conf_config_rule_rev ccrr   ON ccrr.conf_config_ruleno_i = ccr.conf_config_ruleno_i
            INNER JOIN amos.conf_config_rule_pn ccrp    ON ccrp.conf_config_rule_revno_i = ccrr.conf_config_rule_revno_i
            WHERE cc.type = 'PA'
              AND ee.partno IS NOT NULL
              AND ccrp.part_number IS NOT NULL
        \"\"\"
        direct_children = defaultdict(set)
        direct_fps = defaultdict(dict)

        with psycopg2.connect(**self.amos_pg_config) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute(query)
                for row in cursor.fetchall():
                    parent = row["parent_pn"].strip() if row["parent_pn"] else ""
                    child = row["child_pn"].strip() if row["child_pn"] else ""
                    fp_code = row["fp_code"].strip() if row["fp_code"] else ""
                    fp_desc = row["fp_description"].strip() if row["fp_description"] else ""
                    if parent and child:
                        direct_children[parent].add(child)
                    if parent and fp_code:
                        direct_fps[parent][fp_code] = fp_desc

        return direct_children, direct_fps

    def fetch_amos_documents(self) -> Dict[str, Set[str]]:
        query = \"\"\"
            SELECT DISTINCT ee.partno AS pn, doc.docno AS docno
            FROM amos.doc_header doc
            INNER JOIN amos.event_effectivity_link eel ON eel.event_key = doc.docno_i AND eel.event_type = 'DO'
            INNER JOIN amos.event_effectivity ee       ON ee.effectivityno_i = eel.effectivityno_i
            INNER JOIN amos.part p                     ON p.partno = ee.partno
            WHERE ee.partno IS NOT NULL AND doc.docno IS NOT NULL
        \"\"\"
        direct_docs = defaultdict(set)
        with psycopg2.connect(**self.amos_pg_config) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                cursor.execute(query)
                for row in cursor.fetchall():
                    pn = row["pn"].strip() if row["pn"] else ""
                    docno = row["docno"].strip() if row["docno"] else ""
                    if pn and docno:
                        direct_docs[pn].add(docno)
        return direct_docs

    def run_sync(self):
        \"\"\"Performs full extraction, roll-up graph propagation, and atomic write to tenant schema.\"\"\"
        parts = self.fetch_amos_all_master_parts()
        direct_children, direct_fps = self.fetch_amos_pa_configurations()
        direct_docs = self.fetch_amos_documents()

        all_parent_pns = set(direct_children.keys()).union(set(direct_fps.keys())).union(set(direct_docs.keys()))
        pn_records, fp_records, doc_records = [], [], []

        for root_pn in all_parent_pns:
            queue = deque([root_pn])
            visited = {root_pn}
            accumulated_pns = set()
            accumulated_fps = {}
            accumulated_docs = {}

            for fp, desc in direct_fps.get(root_pn, {}).items():
                accumulated_fps[fp] = (desc, 1)
            for doc in direct_docs.get(root_pn, set()):
                accumulated_docs[doc] = 1

            while queue:
                curr = queue.popleft()
                for child in direct_children.get(curr, set()):
                    if child not in accumulated_pns:
                        accumulated_pns.add(child)
                        pn_records.append((root_pn, child, 1 if curr == root_pn else 0))

                    for fp, desc in direct_fps.get(child, {}).items():
                        if fp not in accumulated_fps:
                            accumulated_fps[fp] = (desc, 0)

                    for doc in direct_docs.get(child, set()):
                        if doc not in accumulated_docs:
                            accumulated_docs[doc] = 0

                    if child not in visited:
                        visited.add(child)
                        queue.append(child)

            for fp, (desc, is_dir) in accumulated_fps.items():
                fp_records.append((root_pn, fp, desc, is_dir))
            for doc, is_dir in accumulated_docs.items():
                doc_records.append((root_pn, doc, is_dir))

        # Atomic tenant schema write
        s = self.tenant_schema
        self.db.execute(text(f'DELETE FROM "{s}".amos_part_master;'))
        self.db.execute(text(f'DELETE FROM "{s}".pn_part_of_pn_conf;'))
        self.db.execute(text(f'DELETE FROM "{s}".fp_part_of_pn_conf;'))
        self.db.execute(text(f'DELETE FROM "{s}".doc_part_of_pn_conf;'))

        if parts:
            self.db.execute(
                text(f'INSERT INTO "{s}".amos_part_master (part_number, description) VALUES (:pn, :desc) ON CONFLICT DO NOTHING'),
                [{"pn": r[0], "desc": r[1]} for r in parts]
            )
        if pn_records:
            self.db.execute(
                text(f'INSERT INTO "{s}".pn_part_of_pn_conf (parent_pn, child_pn, is_direct) VALUES (:p, :c, :d) ON CONFLICT DO NOTHING'),
                [{"p": r[0], "c": r[1], "d": r[2]} for r in pn_records]
            )
        if fp_records:
            self.db.execute(
                text(f'INSERT INTO "{s}".fp_part_of_pn_conf (parent_pn, fp_code, fp_description, is_direct) VALUES (:p, :f, :d, :dir) ON CONFLICT DO NOTHING'),
                [{"p": r[0], "f": r[1], "d": r[2], "dir": r[3]} for r in fp_records]
            )
        if doc_records:
            self.db.execute(
                text(f'INSERT INTO "{s}".doc_part_of_pn_conf (parent_pn, docno, is_direct) VALUES (:p, :doc, :d) ON CONFLICT DO NOTHING'),
                [{"p": r[0], "doc": r[1], "d": r[2]} for r in doc_records]
            )

        self.db.commit()
        return len(parts), len(pn_records), len(fp_records), len(doc_records)
"""

# -----------------------------------------------------------------------------
# app/amos/scheduler.py
# -----------------------------------------------------------------------------
FILES["app/amos/scheduler.py"] = """
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from datetime import datetime, timezone
from app.database import SessionLocal
from app.amos.extractor import AmosDataSyncer

scheduler = BackgroundScheduler()

def sync_all_tenants():
    db = SessionLocal()
    try:
        tenants = db.execute(text(\"\"\"
            SELECT id, schema_name, amos_host, amos_port, amos_dbname, amos_user, amos_password
            FROM public.tenants
            WHERE amos_host IS NOT NULL AND amos_host != ''
        \"\"\")).fetchall()

        for t in tenants:
            t_id, schema, host, port, dbname, user, password = t
            pg_conf = {
                "host": host,
                "port": port or 5432,
                "dbname": dbname,
                "user": user,
                "password": password,
                "options": "-c search_path=amos,public"
            }
            try:
                syncer = AmosDataSyncer(pg_conf, schema, db)
                p_c, pn_c, fp_c, doc_c = syncer.run_sync()
                db.execute(text(\"\"\"
                    UPDATE public.tenants
                    SET last_sync_status = 'SUCCESS', last_sync_time = :now
                    WHERE id = :id
                \"\"\"), {"now": datetime.now(timezone.utc), "id": t_id})
                db.commit()
                print(f"[Sync Success] Tenant: {schema} -> Parts: {p_c}, PNs: {pn_c}, FPs: {fp_c}, Docs: {doc_c}")
            except Exception as e:
                db.rollback()
                db.execute(text(\"\"\"
                    UPDATE public.tenants
                    SET last_sync_status = :err, last_sync_time = :now
                    WHERE id = :id
                \"\"\"), {"err": f"FAILED: {str(e)[:45]}", "now": datetime.now(timezone.utc), "id": t_id})
                db.commit()
                print(f"[Sync Error] Tenant {schema}: {e}")
    finally:
        db.close()

def start_scheduler():
    scheduler.add_job(sync_all_tenants, "interval", hours=1, id="amos_sync_job", replace_existing=True)
    scheduler.start()
"""

# -----------------------------------------------------------------------------
# app/matching/ (Base, Fuzzy, LLM stub)
# -----------------------------------------------------------------------------
FILES["app/matching/__init__.py"] = ""

FILES["app/matching/base.py"] = """
from abc import ABC, abstractmethod
from typing import List, Dict, Any

class BaseMatcher(ABC):
    @abstractmethod
    def match_item(self, query: str, candidates: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        \"\"\"
        Returns sorted list of match dicts:
        [
            {"candidate": "CFM56-5C4/P", "score": 100.0, "match_type": "EXACT"},
            {"candidate": "CFM56-5C4", "score": 90.5, "match_type": "FUZZY"}
        ]
        \"\"\"
        pass
"""

FILES["app/matching/fuzzy.py"] = """
import re
from rapidfuzz import fuzz, process
from typing import List, Dict, Any
from app.matching.base import BaseMatcher

class RapidFuzzMatcher(BaseMatcher):
    def clean_str(self, s: str) -> str:
        if not s:
            return ""
        return re.sub(r'[^A-Za-z0-9]', '', s).upper()

    def match_item(self, query: str, candidates: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        if not query or not candidates:
            return []

        clean_q = self.clean_str(query)
        results = []

        for cand in candidates:
            if not cand:
                continue
            clean_c = self.clean_str(cand)

            if query.strip().upper() == cand.strip().upper():
                score = 100.0
                mtype = "EXACT"
            elif clean_q == clean_c:
                score = 99.0
                mtype = "EXACT_NORMALIZED"
            else:
                ratio = fuzz.ratio(query, cand)
                token_sort = fuzz.token_sort_ratio(query, cand)
                partial = fuzz.partial_ratio(query, cand)
                # Weighted blend
                score = round((ratio * 0.5) + (token_sort * 0.3) + (partial * 0.2), 1)
                mtype = "FUZZY"

            results.append({"candidate": cand, "score": score, "match_type": mtype})

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
"""

FILES["app/matching/llm_stub.py"] = """
from typing import List, Dict, Any
from app.matching.base import BaseMatcher

class LLMMatcher(BaseMatcher):
    \"\"\"Modular LLM-based matching interface (Ready for OpenAI / Anthropic / Local LLM integration).\"\"\"
    def __init__(self, api_key: str = None):
        self.api_key = api_key

    def match_item(self, query: str, candidates: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        # Future: Pass candidates & query into an LLM context prompt for semantic part matching
        return []
"""

# -----------------------------------------------------------------------------
# app/spec2500/parser.py & transformer.py
# -----------------------------------------------------------------------------
FILES["app/spec2500/__init__.py"] = ""

FILES["app/spec2500/parser.py"] = """
from lxml import etree
from typing import Dict, List, Set, Any
from collections import defaultdict

class Spec2500Parser:
    def __init__(self, xml_content: bytes):
        self.tree = etree.fromstring(xml_content)

    def extract_root_pn(self) -> str:
        # Check Engine PartNumber in AssetInformation
        for elem in self.tree.xpath("//AssetInformation/Engine/PartNumber"):
            if elem.text and elem.text.strip():
                return elem.text.strip()
        # Fallback to AssetStatus
        for elem in self.tree.xpath("//ATA_AssetStatus/Engine/PartNumber"):
            if elem.text and elem.text.strip():
                return elem.text.strip()
        # Fallback to first PartDetails
        for elem in self.tree.xpath("//PartDetails[1]/SpecificUnitInformation/PartNumber"):
            if elem.text and elem.text.strip():
                return elem.text.strip()
        return ""

    def extract_assembly_and_elements(self) -> Dict[str, Any]:
        \"\"\"Extracts all unique sub-PNs, hierarchy graph, Functional Positions, and Documents.\"\"\"
        sub_pns: Set[str] = set()
        fp_codes: Set[str] = set()
        doc_numbers: Set[str] = set()
        pn_parent_map: Dict[str, str] = {} # child_pn -> parent_pn
        pn_children_map: Dict[str, Set[str]] = defaultdict(set) # parent_pn -> set(child_pns)

        # 1. Parse PartDetails
        for part_elem in self.tree.xpath("//PartDetails"):
            pn_node = part_elem.xpath("./SpecificUnitInformation/PartNumber")
            if not pn_node or not pn_node[0].text:
                continue
            pn = pn_node[0].text.strip()
            sub_pns.add(pn)

            # Functional Position
            for fp in part_elem.xpath(".//PositionInformation/FunctionalPositionInformationList/FunctionalPositionInformation/PositionValue"):
                if fp.text and fp.text.strip():
                    fp_codes.add(fp.text.strip())

            # Documents inside PartDetails
            for doc in part_elem.xpath(".//OtherDocuments/DocumentNumber"):
                if doc.text and doc.text.strip():
                    doc_numbers.add(doc.text.strip())
            for doc in part_elem.xpath(".//TrackedEvent/OperatorTaskInfo/DocumentNumber"):
                if doc.text and doc.text.strip() and doc.text.strip() != "NOT DEFINED":
                    doc_numbers.add(doc.text.strip())

            # Next higher assembly hierarchy
            nha_pn = part_elem.xpath("./NextHigherAssemblyInformation/NHA_PartInfo/PartNumber")
            if nha_pn and nha_pn[0].text:
                parent_pn = nha_pn[0].text.strip()
                pn_parent_map[pn] = parent_pn
                pn_children_map[parent_pn].add(pn)
            else:
                eng_pn = part_elem.xpath("./NextHigherAssemblyInformation/EngineInfo/PartNumber")
                if eng_pn and eng_pn[0].text:
                    parent_pn = eng_pn[0].text.strip()
                    pn_parent_map[pn] = parent_pn
                    pn_children_map[parent_pn].add(pn)

        # 2. Parse SB/Mod/AD Documents across entire Spec2500 crate
        for doc in self.tree.xpath("//PrimaryDocumentRef/DocumentNumber"):
            if doc.text and doc.text.strip():
                doc_numbers.add(doc.text.strip())
        for doc in self.tree.xpath("//AD_Num/AD_Number"):
            if doc.text and doc.text.strip():
                doc_numbers.add(doc.text.strip())
        for doc in self.tree.xpath("//PositionValue"):
            if doc.text and doc.text.strip():
                fp_codes.add(doc.text.strip())
        for doc in self.tree.xpath("//Part_ID_Information/PartNumber"):
            if doc.text and doc.text.strip():
                sub_pns.add(doc.text.strip())

        return {
            "root_pn": self.extract_root_pn(),
            "sub_pns": sorted(list(sub_pns)),
            "fp_codes": sorted(list(fp_codes)),
            "doc_numbers": sorted(list(doc_numbers)),
            "pn_parent_map": pn_parent_map,
            "pn_children_map": {k: list(v) for k, v in pn_children_map.items()}
        }
"""

FILES["app/spec2500/transformer.py"] = """
from lxml import etree
from typing import Dict, Set, List
from collections import deque

class Spec2500Transformer:
    def __init__(self, xml_content: bytes):
        self.tree = etree.fromstring(xml_content)

    def calculate_excluded_cascade(self, excluded_pns: Set[str], pn_children_map: Dict[str, List[str]]) -> Set[str]:
        \"\"\"Cascades exclusions down the hierarchy to ensure all descendant subparts are pruned.\"\"\"
        all_excluded = set(excluded_pns)
        queue = deque(list(excluded_pns))
        while queue:
            curr = queue.popleft()
            for child in pn_children_map.get(curr, []):
                if child not in all_excluded:
                    all_excluded.add(child)
                    queue.append(child)
        return all_excluded

    def transform(
        self,
        pn_mapping: Dict[str, str],        # original_pn -> mapped_pn or '__EXCLUDE__'
        fp_mapping: Dict[str, str],        # original_fp -> mapped_fp
        doc_mapping: Dict[str, str],       # original_doc -> mapped_doc
        pn_children_map: Dict[str, List[str]]
    ) -> bytes:
        excluded_pns = {pn for pn, target in pn_mapping.items() if target == "__EXCLUDE__"}
        total_excluded_pns = self.calculate_excluded_cascade(excluded_pns, pn_children_map)

        # 1. Prune PartDetails for excluded PNs
        for part_elem in self.tree.xpath("//PartDetails"):
            pn_node = part_elem.xpath("./SpecificUnitInformation/PartNumber")
            if pn_node and pn_node[0].text and pn_node[0].text.strip() in total_excluded_pns:
                parent = part_elem.getparent()
                if parent is not None:
                    parent.remove(part_elem)

        # 2. Prune PartInformation entries inside SB/AD compliance lists
        for pi_elem in self.tree.xpath("//PartInformation"):
            pn_node = pi_elem.xpath(".//PartNumber")
            if pn_node and pn_node[0].text and pn_node[0].text.strip() in total_excluded_pns:
                parent = pi_elem.getparent()
                if parent is not None:
                    parent.remove(pi_elem)

        # 3. Apply Part Number Mappings across all XML tags
        valid_pn_maps = {k: v for k, v in pn_mapping.items() if v and v != "__EXCLUDE__"}
        for elem in self.tree.xpath("//PartNumber"):
            if elem.text and elem.text.strip() in valid_pn_maps:
                elem.text = valid_pn_maps[elem.text.strip()]

        # 4. Apply Functional Position Mappings
        for elem in self.tree.xpath("//PositionValue"):
            if elem.text and elem.text.strip() in fp_mapping and fp_mapping[elem.text.strip()]:
                elem.text = fp_mapping[elem.text.strip()]

        # 5. Apply Document Mappings
        for elem in self.tree.xpath("//DocumentNumber | //AD_Number"):
            if elem.text and elem.text.strip() in doc_mapping and doc_mapping[elem.text.strip()]:
                elem.text = doc_mapping[elem.text.strip()]

        return etree.tostring(self.tree, pretty_print=True, xml_declaration=True, encoding="UTF-8")
"""

# -----------------------------------------------------------------------------
# app/routers/ (auth, amos, spec2500)
# -----------------------------------------------------------------------------
FILES["app/routers/__init__.py"] = ""

FILES["app/routers/auth_routes.py"] = """
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db, create_tenant_schema
from app.auth import get_password_hash, verify_password, create_access_token, get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

class RegisterTenantRequest(BaseModel):
    company_name: str
    username: str
    password: str

@router.post("/register")
def register_tenant(req: RegisterTenantRequest, db: Session = Depends(get_db)):
    schema_name = f"tenant_{req.company_name.lower().replace(' ', '_').replace('-', '_')}"

    existing = db.execute(text("SELECT id FROM public.tenants WHERE company_name = :c"), {"c": req.company_name}).fetchone()
    if existing:
        raise HTTPException(status_code=400, detail="Company name already registered")

    user_exist = db.execute(text("SELECT id FROM public.users WHERE username = :u"), {"u": req.username}).fetchone()
    if user_exist:
        raise HTTPException(status_code=400, detail="Username already exists")

    # Create Tenant & User
    t_res = db.execute(
        text("INSERT INTO public.tenants (company_name, schema_name) VALUES (:c, :s) RETURNING id"),
        {"c": req.company_name, "s": schema_name}
    )
    tenant_id = t_res.fetchone()[0]

    db.execute(
        text("INSERT INTO public.users (tenant_id, username, hashed_password, is_admin) VALUES (:t, :u, :p, true)"),
        {"t": tenant_id, "u": req.username, "p": get_password_hash(req.password)}
    )
    db.commit()

    # Create isolated PostgreSQL schema for this tenant
    create_tenant_schema(schema_name)

    return {"message": "Customer registered successfully", "tenant_id": tenant_id, "schema": schema_name}

@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.execute(
        text(\"\"\"
            SELECT u.id, u.tenant_id, u.username, u.hashed_password, t.schema_name
            FROM public.users u
            JOIN public.tenants t ON t.id = u.tenant_id
            WHERE u.username = :u
        \"\"\"),
        {"u": form_data.username}
    ).fetchone()

    if not user or not verify_password(form_data.password, user[3]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")

    token = create_access_token(data={"sub": user[2], "tenant_id": user[1], "schema_name": user[4]})
    return {"access_token": token, "token_type": "bearer", "schema_name": user[4], "username": user[2]}

@router.get("/me")
def get_me(user: dict = Depends(get_current_user)):
    return user
"""

FILES["app/routers/amos_routes.py"] = """
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.auth import get_current_user
from app.amos.extractor import AmosDataSyncer
from pydantic import BaseModel

router = APIRouter(prefix="/api/amos", tags=["AMOS Settings"])

class AmosConnectionConfig(BaseModel):
    host: str
    port: int = 5432
    dbname: str
    user: str
    password: str
    refresh_interval_hours: int = 24

@router.get("/config")
def get_amos_config(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT amos_host, amos_port, amos_dbname, amos_user, refresh_interval_hours, last_sync_status, last_sync_time FROM public.tenants WHERE id = :id"),
        {"id": user["tenant_id"]}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {
        "host": row[0] or "",
        "port": row[1] or 5432,
        "dbname": row[2] or "",
        "user": row[3] or "",
        "refresh_interval_hours": row[4] or 24,
        "last_sync_status": row[5] or "IDLE",
        "last_sync_time": row[6]
    }

@router.post("/config")
def update_amos_config(cfg: AmosConnectionConfig, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    db.execute(
        text(\"\"\"
            UPDATE public.tenants
            SET amos_host = :h, amos_port = :p, amos_dbname = :d, amos_user = :u, amos_password = :pwd, refresh_interval_hours = :r
            WHERE id = :id
        \"\"\"),
        {"h": cfg.host, "p": cfg.port, "d": cfg.dbname, "u": cfg.user, "pwd": cfg.password, "r": cfg.refresh_interval_hours, "id": user["tenant_id"]}
    )
    db.commit()
    return {"message": "AMOS connection settings saved successfully"}

@router.post("/sync")
def trigger_manual_sync(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.execute(
        text("SELECT amos_host, amos_port, amos_dbname, amos_user, amos_password FROM public.tenants WHERE id = :id"),
        {"id": user["tenant_id"]}
    ).fetchone()

    if not t or not t[0]:
        raise HTTPException(status_code=400, detail="Please configure AMOS connection settings first")

    pg_conf = {
        "host": t[0],
        "port": t[1] or 5432,
        "dbname": t[2],
        "user": t[3],
        "password": t[4],
        "options": "-c search_path=amos,public"
    }

    try:
        syncer = AmosDataSyncer(pg_conf, user["schema_name"], db)
        p, pn, fp, doc = syncer.run_sync()
        return {"status": "SUCCESS", "message": f"Successfully synced {p} parts, {pn} sub-PNs, {fp} FPs, and {doc} documents."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AMOS Sync failed: {str(e)}")
"""

FILES["app/routers/spec2500_routes.py"] = """
import io
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.auth import get_current_user
from app.spec2500.parser import Spec2500Parser
from app.spec2500.transformer import Spec2500Transformer
from app.matching.fuzzy import RapidFuzzMatcher
from pydantic import BaseModel
from typing import Dict, List, Any

router = APIRouter(prefix="/api/spec2500", tags=["Spec2500 Processing"])
matcher = RapidFuzzMatcher()

# In-memory session analysis store (or use Redis/DB table)
PARSED_SESSIONS: Dict[str, Any] = {}

class MatchCandidatesRequest(BaseModel):
    root_pn: str

class TransformRequest(BaseModel):
    session_id: str
    pn_mapping: Dict[str, str]
    fp_mapping: Dict[str, str]
    doc_mapping: Dict[str, str]

@router.post("/upload")
async def upload_spec2500(file: UploadFile = File(...), user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        parser = Spec2500Parser(content)
        data = parser.extract_assembly_and_elements()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Spec2500 XML: {str(e)}")

    s = user["schema_name"]
    # Get all master parts for root matching
    master_parts = [r[0] for r in db.execute(text(f'SELECT part_number FROM "{s}".amos_part_master')).fetchall()]

    root_matches = matcher.match_item(data["root_pn"], master_parts, top_k=10)

    import uuid
    session_id = str(uuid.uuid4())
    PARSED_SESSIONS[session_id] = {
        "raw_xml": content,
        "data": data,
        "schema": s
    }

    return {
        "session_id": session_id,
        "detected_root_pn": data["root_pn"],
        "root_matches": root_matches,
        "total_sub_pns": len(data["sub_pns"]),
        "total_fps": len(data["fp_codes"]),
        "total_docs": len(data["doc_numbers"])
    }

@router.post("/candidates/{session_id}")
def get_candidates_for_root(session_id: str, req: MatchCandidatesRequest, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    session = PARSED_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session expired or not found")

    s = user["schema_name"]
    root_pn = req.root_pn

    # 1. Fetch valid sub-PNs for this selected root PN from tenant DB
    sub_pns_db = [r[0] for r in db.execute(
        text(f'SELECT child_pn FROM "{s}".pn_part_of_pn_conf WHERE parent_pn = :p'), {"p": root_pn}
    ).fetchall()]

    # 2. Fetch valid FPs
    fps_db = [r[0] for r in db.execute(
        text(f'SELECT fp_code FROM "{s}".fp_part_of_pn_conf WHERE parent_pn = :p'), {"p": root_pn}
    ).fetchall()]

    # 3. Fetch valid Docs
    docs_db = [r[0] for r in db.execute(
        text(f'SELECT docno FROM "{s}".doc_part_of_pn_conf WHERE parent_pn = :p'), {"p": root_pn}
    ).fetchall()]

    # Run ranked fuzzy matching
    spec_data = session["data"]

    pn_matches = {pn: matcher.match_item(pn, sub_pns_db, top_k=5) for pn in spec_data["sub_pns"]}
    fp_matches = {fp: matcher.match_item(fp, fps_db, top_k=5) for fp in spec_data["fp_codes"]}
    doc_matches = {doc: matcher.match_item(doc, docs_db, top_k=5) for doc in spec_data["doc_numbers"]}

    return {
        "pn_matches": pn_matches,
        "fp_matches": fp_matches,
        "doc_matches": doc_matches,
        "available_pns_pool": sub_pns_db,
        "available_fps_pool": fps_db,
        "available_docs_pool": docs_db,
        "pn_children_map": spec_data["pn_children_map"]
    }

@router.post("/export")
def export_transformed_spec2500(req: TransformRequest):
    session = PARSED_SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session expired")

    transformer = Spec2500Transformer(session["raw_xml"])
    output_xml = transformer.transform(
        pn_mapping=req.pn_mapping,
        fp_mapping=req.fp_mapping,
        doc_mapping=req.doc_mapping,
        pn_children_map=session["data"]["pn_children_map"]
    )

    return Response(
        content=output_xml,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=transformed_spec2500.xml"}
    )
"""

# -----------------------------------------------------------------------------
# app/main.py
# -----------------------------------------------------------------------------
FILES["app/main.py"] = """
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import init_master_tables
from app.amos.scheduler import start_scheduler
from app.routers import auth_routes, amos_routes, spec2500_routes
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_master_tables()
    start_scheduler()
    os.makedirs("uploads", exist_ok=True)
    yield

app = FastAPI(title="AMOS Spec2500 Transformer Suite", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(amos_routes.router)
app.include_router(spec2500_routes.router)

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
"""

# -----------------------------------------------------------------------------
# app/static/ (Web Frontend UI)
# -----------------------------------------------------------------------------
FILES["app/static/index.html"] = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AMOS Spec2500 Pre-Processor & Mapping Hub</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen" x-data="app()">

  <!-- Navbar -->
  <header class="border-b border-slate-800 bg-slate-950/70 backdrop-blur sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <i class="fa-solid fa-plane-engines text-blue-500 text-2xl"></i>
        <span class="font-bold text-xl tracking-tight bg-gradient-to-r from-blue-400 to-indigo-400 bg-clip-text text-transparent">AMOS Spec2500 Suite</span>
      </div>
      <div class="flex items-center space-x-4" x-show="token">
        <span class="text-sm text-slate-400"><i class="fa-solid fa-building mr-1"></i> <span x-text="tenantSchema"></span></span>
        <button @click="openAmosModal()" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-xs font-semibold rounded-lg border border-slate-700">
          <i class="fa-solid fa-database mr-1 text-blue-400"></i> AMOS DB
        </button>
        <button @click="logout()" class="text-slate-400 hover:text-red-400 text-sm">
          <i class="fa-solid fa-right-from-bracket"></i>
        </button>
      </div>
    </div>
  </header>

  <!-- Main Content Area -->
  <main class="max-w-7xl mx-auto px-6 py-8">

    <!-- Auth View -->
    <div x-show="!token" class="max-w-md mx-auto mt-12 bg-slate-950 p-8 rounded-2xl border border-slate-800 shadow-2xl">
      <h2 class="text-2xl font-bold text-center mb-6" x-text="authMode === 'login' ? 'Customer Sign In' : 'Register Customer Account'"></h2>

      <div class="space-y-4">
        <div x-show="authMode === 'register'">
          <label class="text-xs font-semibold text-slate-400">Company Name</label>
          <input type="text" x-model="authForm.company_name" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-sm mt-1 focus:border-blue-500 outline-none">
        </div>
        <div>
          <label class="text-xs font-semibold text-slate-400">Username</label>
          <input type="text" x-model="authForm.username" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-sm mt-1 focus:border-blue-500 outline-none">
        </div>
        <div>
          <label class="text-xs font-semibold text-slate-400">Password</label>
          <input type="password" x-model="authForm.password" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-sm mt-1 focus:border-blue-500 outline-none">
        </div>
        <button @click="handleAuth()" class="w-full bg-blue-600 hover:bg-blue-500 text-white font-semibold py-2.5 rounded-lg text-sm transition mt-2">
          <span x-text="authMode === 'login' ? 'Sign In' : 'Create Customer Tenant'"></span>
        </button>
      </div>

      <div class="mt-6 text-center text-xs text-slate-400">
        <span x-text="authMode === 'login' ? 'New Customer?' : 'Already have an account?'"></span>
        <button @click="authMode = authMode === 'login' ? 'register' : 'login'" class="text-blue-400 font-semibold ml-1 underline">
          <span x-text="authMode === 'login' ? 'Register here' : 'Sign in'"></span>
        </button>
      </div>
    </div>

    <!-- Wizard Steps View -->
    <div x-show="token">

      <!-- Step Navigation Indicators -->
      <div class="flex items-center justify-between mb-8 max-w-4xl mx-auto">
        <template x-for="(stepName, index) in ['1. Upload Spec2500', '2. Root PN Mapping', '3. Sub-PNs & Exclusions', '4. Positions & Documents', '5. Export']">
          <div class="flex items-center space-x-2" :class="step >= index + 1 ? 'text-blue-400 font-semibold' : 'text-slate-600'">
            <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs border"
                 :class="step >= index + 1 ? 'border-blue-400 bg-blue-950/50' : 'border-slate-800 bg-slate-950'"
                 x-text="index + 1"></div>
            <span class="text-xs hidden md:inline" x-text="stepName"></span>
          </div>
        </template>
      </div>

      <!-- Step 1: Upload File -->
      <div x-show="step === 1" class="max-w-2xl mx-auto bg-slate-950 p-10 rounded-2xl border border-slate-800 text-center">
        <i class="fa-solid fa-file-code text-5xl text-blue-500 mb-4"></i>
        <h3 class="text-xl font-bold mb-2">Upload Spec2500 XML File</h3>
        <p class="text-sm text-slate-400 mb-6">Import your engine or aircraft transfer record XML file to begin automated parsing and mapping.</p>

        <input type="file" id="specFileInput" @change="uploadFile($event)" class="hidden" accept=".xml">
        <label for="specFileInput" class="cursor-pointer inline-flex items-center px-6 py-3 bg-blue-600 hover:bg-blue-500 text-white font-semibold rounded-xl text-sm transition">
          <i class="fa-solid fa-cloud-arrow-up mr-2"></i> Choose Spec2500 XML
        </label>
      </div>

      <!-- Step 2: Root PN Match -->
      <div x-show="step === 2" class="max-w-2xl mx-auto bg-slate-950 p-8 rounded-2xl border border-slate-800">
        <h3 class="text-xl font-bold mb-1">Step 1: Confirm Root Engine / Asset Part Number</h3>
        <p class="text-xs text-slate-400 mb-6">We identified the following root part number in the uploaded Spec2500 crate.</p>

        <div class="bg-slate-900 p-4 rounded-xl border border-slate-800 mb-6 flex items-center justify-between">
          <div>
            <div class="text-xs text-slate-500">Detected in Spec2500</div>
            <div class="text-lg font-mono font-bold text-blue-400" x-text="uploadResult.detected_root_pn"></div>
          </div>
          <span class="px-3 py-1 rounded-full text-xs font-semibold bg-blue-950 text-blue-400 border border-blue-800">Root Assembly</span>
        </div>

        <label class="text-xs font-semibold text-slate-400 mb-2 block">Matched AMOS Database Candidates (Ranked by Match %):</label>
        <select x-model="selectedRootPn" class="w-full bg-slate-900 border border-slate-800 rounded-xl px-4 py-3 text-sm focus:border-blue-500 outline-none font-mono">
          <template x-for="m in uploadResult.root_matches" :key="m.candidate">
            <option :value="m.candidate" x-text="`${m.candidate}  (${m.score}% match - ${m.match_type})`"></option>
          </template>
        </select>

        <div class="mt-8 flex justify-end">
          <button @click="confirmRootPn()" class="px-6 py-2.5 bg-blue-600 hover:bg-blue-500 text-white font-semibold rounded-xl text-sm">
            Confirm & Proceed to Sub-Assemblies <i class="fa-solid fa-arrow-right ml-1"></i>
          </button>
        </div>
      </div>

      <!-- Step 3: Sub-PNs & Exclusion Cascade -->
      <div x-show="step === 3" class="bg-slate-950 p-8 rounded-2xl border border-slate-800">
        <div class="flex items-center justify-between mb-6">
          <div>
            <h3 class="text-xl font-bold">Step 2: Sub-Assembly Part Numbers Mapping & Exclusions</h3>
            <p class="text-xs text-slate-400">Map discovered part numbers to your AMOS tree. Mark 'Exclude' to prune a component and all its subparts.</p>
          </div>
          <button @click="step = 4" class="px-5 py-2 bg-blue-600 hover:bg-blue-500 text-white font-semibold rounded-xl text-sm">
            Next: Positions & Documents <i class="fa-solid fa-arrow-right ml-1"></i>
          </button>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm border-collapse">
            <thead>
              <tr class="border-b border-slate-800 text-xs text-slate-400 uppercase">
                <th class="py-3 px-4">Spec2500 Part Number</th>
                <th class="py-3 px-4">Match Status</th>
                <th class="py-3 px-4">Mapped AMOS Part Number</th>
                <th class="py-3 px-4 text-center">Action</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60 font-mono text-xs">
              <template x-for="(matches, origPn) in candidates.pn_matches" :key="origPn">
                <tr :class="pnMapping[origPn] === '__EXCLUDE__' ? 'opacity-40 bg-red-950/20' : ''">
                  <td class="py-3 px-4 font-semibold text-slate-200" x-text="origPn"></td>
                  <td class="py-3 px-4">
                    <span class="px-2 py-0.5 rounded text-[11px] font-semibold"
                          :class="matches[0]?.score >= 90 ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-amber-950 text-amber-400 border border-amber-800'"
                          x-text="matches[0] ? `${matches[0].score}% Match` : 'No Match'"></span>
                  </td>
                  <td class="py-3 px-4">
                    <select x-model="pnMapping[origPn]" :disabled="pnMapping[origPn] === '__EXCLUDE__'" class="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-1.5 text-xs outline-none focus:border-blue-500">
                      <option value="">-- Leave Unresolved --</option>
                      <template x-for="m in matches" :key="m.candidate">
                        <option :value="m.candidate" x-text="`${m.candidate} (${m.score}%)`"></option>
                      </template>
                    </select>
                  </td>
                  <td class="py-3 px-4 text-center">
                    <button @click="toggleExclude(origPn)" class="px-2.5 py-1 rounded text-[11px] font-semibold border"
                            :class="pnMapping[origPn] === '__EXCLUDE__' ? 'bg-red-600 text-white border-red-500' : 'bg-slate-900 text-slate-400 border-slate-800 hover:border-red-600 hover:text-red-400'">
                      <i class="fa-solid fa-ban mr-1"></i> <span x-text="pnMapping[origPn] === '__EXCLUDE__' ? 'Excluded' : 'Exclude'"></span>
                    </button>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Step 4: Positions & Documents -->
      <div x-show="step === 4" class="bg-slate-950 p-8 rounded-2xl border border-slate-800 space-y-8">
        <div class="flex items-center justify-between">
          <h3 class="text-xl font-bold">Step 3: Functional Positions & Documents Mapping</h3>
          <button @click="step = 5" class="px-6 py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white font-semibold rounded-xl text-sm">
            Generate & Download Clean Spec2500 <i class="fa-solid fa-check ml-1"></i>
          </button>
        </div>

        <!-- Functional Positions -->
        <div>
          <h4 class="text-sm font-semibold text-blue-400 uppercase tracking-wider mb-3">Functional Positions</h4>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <template x-for="(matches, origFp) in candidates.fp_matches" :key="origFp">
              <div class="bg-slate-900 p-3 rounded-xl border border-slate-800 flex items-center justify-between space-x-4">
                <span class="font-mono text-xs font-bold text-slate-300" x-text="origFp"></span>
                <select x-model="fpMapping[origFp]" class="bg-slate-950 border border-slate-800 rounded-lg px-2 py-1 text-xs font-mono outline-none">
                  <option value="">-- Same as Original --</option>
                  <template x-for="m in matches" :key="m.candidate">
                    <option :value="m.candidate" x-text="`${m.candidate} (${m.score}%)`"></option>
                  </template>
                </select>
              </div>
            </template>
          </div>
        </div>

        <!-- Documents -->
        <div>
          <h4 class="text-sm font-semibold text-indigo-400 uppercase tracking-wider mb-3">Document Numbers (SB / AD / Task)</h4>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <template x-for="(matches, origDoc) in candidates.doc_matches" :key="origDoc">
              <div class="bg-slate-900 p-3 rounded-xl border border-slate-800 flex items-center justify-between space-x-4">
                <span class="font-mono text-xs font-bold text-slate-300 truncate max-w-[200px]" x-text="origDoc"></span>
                <select x-model="docMapping[origDoc]" class="bg-slate-950 border border-slate-800 rounded-lg px-2 py-1 text-xs font-mono outline-none">
                  <option value="">-- Same as Original --</option>
                  <template x-for="m in matches" :key="m.candidate">
                    <option :value="m.candidate" x-text="`${m.candidate} (${m.score}%)`"></option>
                  </template>
                </select>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- Step 5: Export Summary -->
      <div x-show="step === 5" class="max-w-2xl mx-auto bg-slate-950 p-10 rounded-2xl border border-slate-800 text-center">
        <i class="fa-solid fa-circle-check text-5xl text-emerald-400 mb-4"></i>
        <h3 class="text-2xl font-bold mb-2">Spec2500 Transformations Ready</h3>
        <p class="text-sm text-slate-400 mb-6">All part numbers, functional positions, documents have been resolved and excluded branches pruned.</p>

        <button @click="downloadExport()" class="px-8 py-3.5 bg-emerald-600 hover:bg-emerald-500 text-white font-bold rounded-xl text-sm shadow-lg shadow-emerald-950 transition">
          <i class="fa-solid fa-download mr-2"></i> Download Transformed Spec2500 XML
        </button>
      </div>

    </div>

    <!-- AMOS Settings Modal -->
    <div x-show="showAmosModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 z-50">
      <div class="bg-slate-950 border border-slate-800 rounded-2xl max-w-lg w-full p-6 space-y-4">
        <div class="flex items-center justify-between">
          <h3 class="text-lg font-bold">AMOS PostgreSQL Connection</h3>
          <button @click="showAmosModal = false" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <div class="space-y-3 text-xs">
          <div><label class="text-slate-400">Host</label><input type="text" x-model="amosForm.host" class="w-full bg-slate-900 border border-slate-800 rounded-lg p-2 mt-1"></div>
          <div><label class="text-slate-400">Port</label><input type="number" x-model="amosForm.port" class="w-full bg-slate-900 border border-slate-800 rounded-lg p-2 mt-1"></div>
          <div><label class="text-slate-400">Database Name</label><input type="text" x-model="amosForm.dbname" class="w-full bg-slate-900 border border-slate-800 rounded-lg p-2 mt-1"></div>
          <div><label class="text-slate-400">User</label><input type="text" x-model="amosForm.user" class="w-full bg-slate-900 border border-slate-800 rounded-lg p-2 mt-1"></div>
          <div><label class="text-slate-400">Password</label><input type="password" x-model="amosForm.password" class="w-full bg-slate-900 border border-slate-800 rounded-lg p-2 mt-1"></div>
        </div>
        <div class="flex items-center justify-between pt-4 border-t border-slate-800">
          <button @click="triggerSync()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-xs font-semibold rounded-lg"><i class="fa-solid fa-rotate mr-1"></i> Sync Now</button>
          <button @click="saveAmosConfig()" class="px-5 py-2 bg-blue-600 hover:bg-blue-500 text-xs font-semibold rounded-lg text-white">Save Configuration</button>
        </div>
      </div>
    </div>

  </main>

  <script src="/app.js"></script>
</body>
</html>
"""

FILES["app/static/app.js"] = """
function app() {
  return {
    token: localStorage.getItem("token") || null,
    tenantSchema: localStorage.getItem("schema") || "",
    authMode: "login",
    authForm: { company_name: "", username: "", password: "" },
    step: 1,
    showAmosModal: false,
    amosForm: { host: "", port: 5432, dbname: "", user: "", password: "" },
    uploadResult: {},
    selectedRootPn: "",
    candidates: { pn_matches: {}, fp_matches: {}, doc_matches: {}, pn_children_map: {} },
    pnMapping: {},
    fpMapping: {},
    docMapping: {},

    async handleAuth() {
      const endpoint = this.authMode === "login" ? "/api/auth/login" : "/api/auth/register";
      let body;
      if (this.authMode === "login") {
        body = new URLSearchParams({ username: this.authForm.username, password: this.authForm.password });
      } else {
        body = JSON.stringify(this.authForm);
      }

      const res = await fetch(endpoint, {
        method: "POST",
        headers: this.authMode === "login" ? { "Content-Type": "application/x-www-form-urlencoded" } : { "Content-Type": "application/json" },
        body
      });
      const data = await res.json();
      if (!res.ok) return alert(data.detail || "Authentication error");

      if (this.authMode === "login") {
        this.token = data.access_token;
        this.tenantSchema = data.schema_name;
        localStorage.setItem("token", this.token);
        localStorage.setItem("schema", this.tenantSchema);
      } else {
        alert("Registration successful! Please sign in.");
        this.authMode = "login";
      }
    },

    logout() {
      this.token = null;
      localStorage.clear();
      this.step = 1;
    },

    async openAmosModal() {
      this.showAmosModal = true;
      const res = await fetch("/api/amos/config", { headers: { Authorization: `Bearer ${this.token}` } });
      if (res.ok) this.amosForm = await res.json();
    },

    async saveAmosConfig() {
      const res = await fetch("/api/amos/config", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.token}` },
        body: JSON.stringify(this.amosForm)
      });
      if (res.ok) { alert("AMOS Settings saved!"); this.showAmosModal = false; }
    },

    async triggerSync() {
      const res = await fetch("/api/amos/sync", { method: "POST", headers: { Authorization: `Bearer ${this.token}` } });
      const data = await res.json();
      alert(data.message || data.detail);
    },

    async uploadFile(e) {
      const file = e.target.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append("file", file);

      const res = await fetch("/api/spec2500/upload", {
        method: "POST",
        headers: { Authorization: `Bearer ${this.token}` },
        body: fd
      });
      const data = await res.json();
      if (!res.ok) return alert(data.detail);

      this.uploadResult = data;
      this.selectedRootPn = data.root_matches[0]?.candidate || data.detected_root_pn;
      this.step = 2;
    },

    async confirmRootPn() {
      const res = await fetch(`/api/spec2500/candidates/${this.uploadResult.session_id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.token}` },
        body: JSON.stringify({ root_pn: this.selectedRootPn })
      });
      const data = await res.json();
      if (!res.ok) return alert(data.detail);

      this.candidates = data;
      // Pre-select best matches
      for (const [orig, matches] of Object.entries(data.pn_matches)) {
        this.pnMapping[orig] = matches[0]?.score >= 80 ? matches[0].candidate : "";
      }
      for (const [orig, matches] of Object.entries(data.fp_matches)) {
        this.fpMapping[orig] = matches[0]?.score >= 85 ? matches[0].candidate : "";
      }
      for (const [orig, matches] of Object.entries(data.doc_matches)) {
        this.docMapping[orig] = matches[0]?.score >= 85 ? matches[0].candidate : "";
      }
      this.step = 3;
    },

    toggleExclude(pn) {
      if (this.pnMapping[pn] === "__EXCLUDE__") {
        this.pnMapping[pn] = this.candidates.pn_matches[pn]?.[0]?.candidate || "";
      } else {
        this.pnMapping[pn] = "__EXCLUDE__";
      }
    },

    async downloadExport() {
      const res = await fetch("/api/spec2500/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: this.uploadResult.session_id,
          pn_mapping: this.pnMapping,
          fp_mapping: this.fpMapping,
          doc_mapping: this.docMapping
        })
      });

      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Transformed_Spec2500_${this.selectedRootPn.replace('/', '_')}.xml`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
  };
}
"""

# -----------------------------------------------------------------------------
# Write ZIP
# -----------------------------------------------------------------------------
zip_filename = "amos_spec2500_app.zip"
print(f"Creating {zip_filename} with full application source code...")

with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
    for filepath, content in FILES.items():
        zipf.writestr(filepath, content.strip())
        print(f" + Added {filepath}")

print(f"\n[DONE] Successfully generated {zip_filename} ({os.path.getsize(zip_filename)} bytes).")
