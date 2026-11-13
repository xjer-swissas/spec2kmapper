"""Tenant-local description vector index; PN identifiers are never embedded."""
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from rapidfuzz import fuzz, process
from sqlalchemy import text
from app.config import settings
from app.database import SessionLocal
from app.matching.parts import encoder
from app.matching.fuzzy import RapidFuzzMatcher

log = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='catalog-index')
lock = threading.Lock()
states = {}
indexes = {}
catalogs = {}
clean = RapidFuzzMatcher().clean_str


def catalog(db, schema):
    revision = str(db.execute(text('SELECT last_sync_time FROM public.tenants WHERE schema_name=:s'), {'s': schema}).scalar())
    with lock:
        cached = catalogs.get(schema)
    if cached and cached[0] == revision:
        return cached[1]
    rows = [(pn, (description or '').strip()) for pn, description in db.execute(
        text(f'SELECT part_number, description FROM "{schema}".amos_part_master ORDER BY part_number')).fetchall()]
    with lock:
        catalogs[schema] = (revision, rows)
    return rows


def index_path(schema):
    root = Path(settings.SEMANTIC_INDEX_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root / (hashlib.sha256((schema + settings.SEMANTIC_MODEL).encode()).hexdigest() + '.sqlite')


def status(schema):
    with lock:
        return dict(states.get(schema, {'status': 'NOT_READY', 'completed': 0, 'total': 0}))


def schedule(schema):
    with lock:
        if states.get(schema, {}).get('status') in ('QUEUED', 'BUILDING'):
            # A sync may finish during a build. Re-read the latest catalogue afterward.
            states[schema]['rerun'] = True
            return
        states[schema] = {'status': 'QUEUED', 'completed': 0, 'total': 0}
    executor.submit(build, schema)


def build(schema):
    try:
        with SessionLocal() as db:
            rows = catalog(db, schema)
        descriptions = sorted({d for _, d in rows if d})
        with lock:
            states[schema].update(status='BUILDING', total=len(descriptions))
        # Persist each batch so interrupted builds resume without re-embedding.
        with sqlite3.connect(index_path(schema)) as cache:
            cache.execute('CREATE TABLE IF NOT EXISTS embeddings (description TEXT PRIMARY KEY, vector BLOB NOT NULL)')
            existing = {d: np.frombuffer(v, dtype=np.float32) for d, v in cache.execute('SELECT description, vector FROM embeddings')}
            def publish():
                ready = [d for d in descriptions if d in existing]
                if ready:
                    with lock:
                        indexes[schema] = (ready, np.stack([existing[d] for d in ready]))
            publish()
            missing = [d for d in descriptions if d not in existing]
            with lock:
                states[schema]['completed'] = len(descriptions) - len(missing)
            for start in range(0, len(missing), 64):
                batch = missing[start:start + 64]
                vectors = encoder.encode(batch)
                if len(vectors) != len(batch):
                    raise RuntimeError(encoder.warning or 'Description model unavailable')
                for description, vector in vectors.items():
                    vector = np.asarray(vector, dtype=np.float32)
                    norm = np.linalg.norm(vector)
                    vector = vector / norm if norm else vector
                    existing[description] = vector
                    cache.execute('INSERT OR REPLACE INTO embeddings VALUES (?, ?)', (description, vector.tobytes()))
                cache.commit()
                with lock:
                    states[schema]['completed'] += len(batch)
                if start % 1024 == 0:
                    publish()
        matrix = np.stack([existing[d] for d in descriptions]) if descriptions else np.empty((0, 0), dtype=np.float32)
        with lock:
            indexes[schema] = (descriptions, matrix)
            states[schema].update(status='READY', completed=len(descriptions))
    except Exception:
        log.exception('Catalogue indexing failed for %s', schema)
        with lock:
            states[schema].update(status='FAILED', error='Description index unavailable. Retry indexing; PN search remains available.')
    finally:
        with lock:
            rerun = states[schema].pop('rerun', False)
        if rerun:
            schedule(schema)


def start_indexes():
    with SessionLocal() as db:
        schemas = [r[0] for r in db.execute(text('SELECT schema_name FROM public.tenants')).fetchall()]
    for schema in schemas:
        schedule(schema)


def hybrid_search(rows, query, description, index, configured=(), limit=50, ranking_mode='weighted', reranker='none'):
    """Merge PN and description evidence; exact identifiers always win."""
    pns = [pn for pn, _ in rows]
    descriptions_by_pn = dict(rows)
    normalized = clean(query)
    evidence = {}
    if query:
        for candidate, score, _ in process.extract(query.upper(), pns, processor=str.upper,
                                                   scorer=fuzz.ratio, limit=limit, score_cutoff=35):
            evidence[candidate] = dict(pn_score=round(score, 1))
        # Normalization is a separate identifier rule, not an embedding signal.
        for pn in pns:
            if normalized and clean(pn) == normalized:
                evidence.setdefault(pn, {})['pn_score'] = 99.
            if pn.strip().upper() == query.strip().upper():
                evidence.setdefault(pn, {})['pn_score'] = 100.
    semantic_scores = {}
    if description and index is not None and len(index[0]):
        vectors = encoder.encode([description])
        if description in vectors:
            vector = vectors[description]
            norm = np.linalg.norm(vector)
            scores = index[1] @ (vector / norm if norm else vector)
            best = np.argsort(scores)[-limit:][::-1]
            semantic_scores = {index[0][i]: round(float(scores[i]) * 100, 1) for i in best if scores[i] > 0}
    if description:
        unique = list({d for _, d in rows if d})
        lexical = {d: round(score, 1) for d, score, _ in process.extract(
            description, unique, processor=str.upper, scorer=fuzz.token_sort_ratio, limit=limit, score_cutoff=35)}
        for pn, desc in rows:
            if desc in semantic_scores or desc in lexical:
                evidence.setdefault(pn, {}).update(semantic_score=semantic_scores.get(desc), description_score=lexical.get(desc))
    configured = set(configured)
    results = []
    for pn, scores in evidence.items():
        pn_score = scores.get('pn_score', 0)
        kind = 'EXACT PN' if pn_score == 100 else ('NORMALIZED PN' if normalized and clean(pn) == normalized else 'SUGGESTION')
        rank = max(pn_score, scores.get('semantic_score') or 0, scores.get('description_score') or 0)
        results.append(dict(part_number=pn, description=descriptions_by_pn[pn], match_type=kind,
                            pn_score=scores.get('pn_score'), semantic_score=scores.get('semantic_score'),
                            description_score=scores.get('description_score'), in_configuration=pn in configured,
                            rank=rank))
    results.sort(key=lambda r: (r['match_type'] == 'EXACT PN', r['match_type'] == 'NORMALIZED PN', r['rank'], r['in_configuration']), reverse=True)
    if ranking_mode == 'rrf':
        from app.matching.ranking import rrf_rank
        results = rrf_rank(results, identifier='part_number')
    from app.matching.reranker import rerank_candidates
    return rerank_candidates(results, description, reranker)[:limit]


def search(db, schema, query, description, root):
    rows = catalog(db, schema)
    with lock:
        index = indexes.get(schema)
    configured = [r[0] for r in db.execute(text(
        f'SELECT child_pn FROM "{schema}".pn_part_of_pn_conf WHERE parent_pn=:p'), {'p': root}).fetchall()] if root else []
    from app.matching.preferences import preferences
    prefs = preferences(db, schema)
    items = hybrid_search(rows, query, description, index, configured, ranking_mode=prefs['ranking_mode'], reranker=prefs['reranker'])
    from app.matching.reranker import reranker_warning
    return {'reranker_warning': reranker_warning([items]), 'items': items, 'index': status(schema), 'semantic_used': bool(description and index is not None and not encoder.warning)}
