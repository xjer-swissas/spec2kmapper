from collections import defaultdict, deque
import os
import sqlite3
import subprocess
from typing import Dict, List, Optional, Set, Tuple
import psycopg2
import psycopg2.extras


class AmosConfigTreeBuilder:

    def __init__(self, pg_config: dict, sqlite_db_path: str = "amos_tree.db"):
        self.pg_config = pg_config
        self.sqlite_db_path = sqlite_db_path
        self.init_sqlite_db()

    def init_sqlite_db(self):
        """Initializes the local SQLite database schema."""
        with sqlite3.connect(self.sqlite_db_path) as conn:
            cursor = conn.cursor()

            # 1. Master Parts Table (All AMOS Parts)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS amos_part_master (
                    part_number TEXT PRIMARY KEY,
                    description TEXT
                )
            """)

            # 2. Table for PN -> Child PN relations
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pn_part_of_pn_conf (
                    parent_pn TEXT NOT NULL,
                    child_pn TEXT NOT NULL,
                    is_direct INTEGER NOT NULL, -- 1 = Direct child, 0 = Inherited/Sub-level
                    PRIMARY KEY (parent_pn, child_pn)
                )
            """)

            # 3. Table for PN -> Functional Position relations
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fp_part_of_pn_conf (
                    parent_pn TEXT NOT NULL,
                    fp_code TEXT NOT NULL,
                    fp_description TEXT,
                    is_direct INTEGER NOT NULL, -- 1 = Direct FP, 0 = Inherited/Sub-level
                    PRIMARY KEY (parent_pn, fp_code)
                )
            """)

            # 4. Table for PN -> Document relations
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS doc_part_of_pn_conf (
                    parent_pn TEXT NOT NULL,
                    docno TEXT NOT NULL,
                    is_direct INTEGER NOT NULL, -- 1 = Direct Document, 0 = Inherited/Sub-level
                    PRIMARY KEY (parent_pn, docno)
                )
            """)

            # Indexes for fast querying
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_master_pn ON"
                " amos_part_master(part_number);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_pn_parent ON"
                " pn_part_of_pn_conf(parent_pn);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_parent ON"
                " fp_part_of_pn_conf(parent_pn);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_doc_parent ON"
                " doc_part_of_pn_conf(parent_pn);"
            )
            conn.commit()

    def fetch_all_amos_parts(self) -> List[Tuple[str, str]]:
        """Queries AMOS PostgreSQL database for all part numbers in the master part catalog."""
        print("Connecting to AMOS PostgreSQL and querying all master parts...")

        query = """
            SELECT DISTINCT
                p.partno       AS part_number,
                p.description  AS description
            FROM amos.part p
            WHERE p.partno IS NOT NULL
        """

        parts_records = []
        with psycopg2.connect(**self.pg_config) as conn:
            with conn.cursor(
                cursor_factory=psycopg2.extras.DictCursor
            ) as cursor:
                cursor.execute(query)
                for row in cursor.fetchall():
                    pn = (
                        row["part_number"].strip()
                        if row["part_number"]
                        else ""
                    )
                    desc = (
                        row["description"].strip()
                        if row["description"]
                        else ""
                    )

                    if pn:
                        parts_records.append((pn, desc))

        print(f"Retrieved {len(parts_records)} master parts from AMOS.")
        return parts_records

    def fetch_amos_pa_configurations(
        self,
    ) -> Tuple[Dict[str, Set[str]], Dict[str, Dict[str, str]]]:
        """Queries AMOS PostgreSQL database for all PA (Part Assembly) configurations."""
        print(
            "Connecting to AMOS PostgreSQL and querying 'PA' configurations..."
        )

        query = """
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
        """

        direct_children: Dict[str, Set[str]] = defaultdict(set)
        direct_fps: Dict[str, Dict[str, str]] = defaultdict(dict)

        with psycopg2.connect(**self.pg_config) as conn:
            with conn.cursor(
                cursor_factory=psycopg2.extras.DictCursor
            ) as cursor:
                cursor.execute(query)
                for row in cursor.fetchall():
                    parent = (
                        row["parent_pn"].strip() if row["parent_pn"] else ""
                    )
                    child = row["child_pn"].strip() if row["child_pn"] else ""
                    fp_code = row["fp_code"].strip() if row["fp_code"] else ""
                    fp_desc = (
                        row["fp_description"].strip()
                        if row["fp_description"]
                        else ""
                    )

                    if parent and child:
                        direct_children[parent].add(child)
                    if parent and fp_code:
                        direct_fps[parent][fp_code] = fp_desc

        print(
            f"Retrieved {len(direct_children)} parent PNs with child"
            " configurations."
        )
        return direct_children, direct_fps

    def fetch_amos_pn_documents(self) -> Dict[str, Set[str]]:
        """Queries AMOS PostgreSQL database for all documents linked to part numbers."""
        print("Connecting to AMOS PostgreSQL and querying linked documents...")

        query = """
            SELECT DISTINCT
                ee.partno   AS pn,
                doc.docno   AS docno
            FROM amos.doc_header doc
            INNER JOIN amos.event_effectivity_link eel ON eel.event_key = doc.docno_i AND eel.event_type = 'DO'
            INNER JOIN amos.event_effectivity ee       ON ee.effectivityno_i = eel.effectivityno_i
            INNER JOIN amos.part p                     ON p.partno = ee.partno
            WHERE ee.partno IS NOT NULL
              AND doc.docno IS NOT NULL
        """

        direct_docs: Dict[str, Set[str]] = defaultdict(set)

        with psycopg2.connect(**self.pg_config) as conn:
            with conn.cursor(
                cursor_factory=psycopg2.extras.DictCursor
            ) as cursor:
                cursor.execute(query)
                for row in cursor.fetchall():
                    pn = row["pn"].strip() if row["pn"] else ""
                    docno = row["docno"].strip() if row["docno"] else ""
                    if pn and docno:
                        direct_docs[pn].add(docno)

        print(f"Retrieved document links for {len(direct_docs)} unique PNs.")
        return direct_docs

    def build_and_save_trees(self):
        """Builds master parts, configuration trees, documents, and writes results into SQLite."""
        all_master_parts = self.fetch_all_amos_parts()
        direct_children, direct_fps = self.fetch_amos_pa_configurations()
        direct_docs = self.fetch_amos_pn_documents()

        all_parent_pns = (
            set(direct_children.keys())
            .union(set(direct_fps.keys()))
            .union(set(direct_docs.keys()))
        )

        pn_records = []  # (parent_pn, child_pn, is_direct)
        fp_records = []  # (parent_pn, fp_code, fp_description, is_direct)
        doc_records = []  # (parent_pn, docno, is_direct)

        print("Calculating recursive tree roll-ups across all parent PNs...")

        for root_pn in all_parent_pns:
            queue = deque([root_pn])
            visited_pns = {root_pn}

            accumulated_pns = set()
            accumulated_fps: Dict[str, Tuple[str, int]] = {}
            accumulated_docs: Dict[str, int] = {}

            # Direct FPs for the root
            for fp, desc in direct_fps.get(root_pn, {}).items():
                accumulated_fps[fp] = (desc, 1)

            # Direct Docs for the root
            for doc in direct_docs.get(root_pn, set()):
                accumulated_docs[doc] = 1

            while queue:
                curr_pn = queue.popleft()

                for child_pn in direct_children.get(curr_pn, set()):
                    if child_pn not in accumulated_pns:
                        is_direct = 1 if curr_pn == root_pn else 0
                        accumulated_pns.add(child_pn)
                        pn_records.append((root_pn, child_pn, is_direct))

                    for fp, desc in direct_fps.get(child_pn, {}).items():
                        if fp not in accumulated_fps:
                            accumulated_fps[fp] = (desc, 0)

                    for doc in direct_docs.get(child_pn, set()):
                        if doc not in accumulated_docs:
                            accumulated_docs[doc] = 0

                    if child_pn not in visited_pns:
                        visited_pns.add(child_pn)
                        queue.append(child_pn)

            for fp, (desc, is_dir) in accumulated_fps.items():
                fp_records.append((root_pn, fp, desc, is_dir))

            for doc, is_dir in accumulated_docs.items():
                doc_records.append((root_pn, doc, is_dir))

        # Save to SQLite in batch
        print(
            "Writing resolved configuration trees and master parts to"
            " SQLite..."
        )
        with sqlite3.connect(self.sqlite_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM amos_part_master")
            cursor.execute("DELETE FROM pn_part_of_pn_conf")
            cursor.execute("DELETE FROM fp_part_of_pn_conf")
            cursor.execute("DELETE FROM doc_part_of_pn_conf")

            # 1. Master parts (Fixed: exactly 2 placeholders (?, ?))
            cursor.executemany(
                """
                INSERT OR IGNORE INTO amos_part_master (part_number, description)
                VALUES (?, ?)
            """,
                all_master_parts,
            )

            # 2. Child PNs
            cursor.executemany(
                """
                INSERT OR IGNORE INTO pn_part_of_pn_conf (parent_pn, child_pn, is_direct)
                VALUES (?, ?, ?)
            """,
                pn_records,
            )

            # 3. Functional Positions
            cursor.executemany(
                """
                INSERT OR IGNORE INTO fp_part_of_pn_conf (parent_pn, fp_code, fp_description, is_direct)
                VALUES (?, ?, ?, ?)
            """,
                fp_records,
            )

            # 4. Documents
            cursor.executemany(
                """
                INSERT OR IGNORE INTO doc_part_of_pn_conf (parent_pn, docno, is_direct)
                VALUES (?, ?, ?)
            """,
                doc_records,
            )

            conn.commit()

        print(
            f"Successfully saved {len(all_master_parts)} master parts, "
            f"{len(pn_records)} PN relationships, "
            f"{len(fp_records)} FP relationships, and "
            f"{len(doc_records)} Doc relationships."
        )

    # -------------------------------------------------------------------------
    # Retrieval Methods
    # -------------------------------------------------------------------------
    def get_all_master_parts(
        self, search_pattern: Optional[str] = None
    ) -> List[Tuple[str, str]]:
        """Returns all part numbers from the master catalog, optionally filtered by a SQL LIKE pattern."""
        with sqlite3.connect(self.sqlite_db_path) as conn:
            cursor = conn.cursor()
            if search_pattern:
                cursor.execute(
                    """
                    SELECT part_number, description
                    FROM amos_part_master
                    WHERE part_number LIKE ?
                    ORDER BY part_number
                """,
                    (search_pattern,),
                )
            else:
                cursor.execute(
                    """
                    SELECT part_number, description
                    FROM amos_part_master
                    ORDER BY part_number
                """
                )
            return cursor.fetchall()

    def get_all_sub_pns(self, pn: str, direct_only: bool = False) -> List[str]:
        """Returns a list of all part numbers that can be in the tree below the given PN."""
        with sqlite3.connect(self.sqlite_db_path) as conn:
            cursor = conn.cursor()
            query = f"""
                SELECT child_pn FROM pn_part_of_pn_conf
                WHERE parent_pn = ? {'AND is_direct = 1' if direct_only else ''}
                ORDER BY child_pn
            """
            cursor.execute(query, (pn,))
            return [row[0] for row in cursor.fetchall()]

    def get_all_sub_fps(
        self, pn: str, direct_only: bool = False
    ) -> List[Tuple[str, str]]:
        """Returns a list of all functional positions that can be in the tree below the given PN."""
        with sqlite3.connect(self.sqlite_db_path) as conn:
            cursor = conn.cursor()
            query = f"""
                SELECT fp_code, fp_description FROM fp_part_of_pn_conf
                WHERE parent_pn = ? {'AND is_direct = 1' if direct_only else ''}
                ORDER BY fp_code
            """
            cursor.execute(query, (pn,))
            return cursor.fetchall()

    def get_all_sub_docs(
        self, pn: str, direct_only: bool = False
    ) -> List[str]:
        """Returns a list of all document numbers linked to the PN or any PN in its sub-tree."""
        with sqlite3.connect(self.sqlite_db_path) as conn:
            cursor = conn.cursor()
            query = f"""
                SELECT docno FROM doc_part_of_pn_conf
                WHERE parent_pn = ? {'AND is_direct = 1' if direct_only else ''}
                ORDER BY docno
            """
            cursor.execute(query, (pn,))
            return [row[0] for row in cursor.fetchall()]


# -------------------------------------------------------------------------
# Example Usage
# -------------------------------------------------------------------------
if __name__ == "__main__":
    windows_host = os.environ.get("AMOS_PGHOST") or subprocess.check_output(
        "ip route | awk '/default/ {print $3; exit}'",
        shell=True,
        text=True,
    ).strip()

    # AMOS PostgreSQL Connection Parameters
    PG_CONFIG = {
        "host": os.environ.get("AMOS_PGHOST", windows_host),
        "port": int(os.environ.get("AMOS_PGPORT", "5432")),
        "dbname": os.environ["AMOS_PGDATABASE"],
        "user": os.environ["AMOS_PGUSER"],
        "password": os.environ["AMOS_PGPASSWORD"],
        "options": "-c search_path=amos,public",
    }

    # Instantiate and run tree sync
    tree_builder = AmosConfigTreeBuilder(
        pg_config=PG_CONFIG, sqlite_db_path="amos_configs.db"
    )

    # Run data extraction & hierarchical propagation
    tree_builder.build_and_save_trees()

    # 1. Inspect Master Part Catalog
    all_parts = tree_builder.get_all_master_parts()
    print(f"\nTotal Master Parts in SQLite: {len(all_parts)}")
    print(f"Sample First 5 Parts: {all_parts[:5]}")

    # 2. Test queries for a specific PN
    target_pn = "CFM56-5B4P/3"

    print(f"\n--- Sub-PNs for {target_pn} ---")
    for p in tree_builder.get_all_sub_pns(target_pn):
        print(f" - {p}")

    print(f"\n--- Sub-FPs for {target_pn} ---")
    for code, desc in tree_builder.get_all_sub_fps(target_pn):
        print(f" - [{code}] {desc}")

    print(f"\n--- Sub-Documents for {target_pn} ---")
    for doc in tree_builder.get_all_sub_docs(target_pn):
        print(f" - Doc: {doc}")
