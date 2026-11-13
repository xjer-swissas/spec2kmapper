import psycopg2
from app.amos.queries import EXPORTS
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
        query = EXPORTS["parts"]["sql"]
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
        query = EXPORTS["configurations"]["sql"]
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
        query = EXPORTS["documents"]["sql"]
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

    def run_sync(self, progress=None, commit=True):
        """Performs full extraction, roll-up graph propagation, and atomic write to tenant schema."""
        progress = progress or (lambda phase: None)
        progress("Loading part numbers and descriptions")
        parts = self.fetch_amos_all_master_parts()
        progress(f"Loaded {len(parts)} parts; loading assembly configurations")
        direct_children, direct_fps = self.fetch_amos_pa_configurations()
        progress("Loading documents")
        direct_docs = self.fetch_amos_documents()
        return self.replace_cache(parts, direct_children, direct_fps, direct_docs, progress, commit)

    def replace_cache(self, parts, direct_children, direct_fps, direct_docs, progress=None, commit=True):
        progress = progress or (lambda phase: None)
        progress("Building assembly hierarchy")

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
        progress("Saving cache (previous data remains available until complete)")
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

        if commit:
            self.db.commit()
        return len(parts), len(pn_records), len(fp_records), len(doc_records)
