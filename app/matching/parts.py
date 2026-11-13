"""Identifier-first ranking with lexical and local semantic description evidence."""
import logging
import threading
import time
from collections import OrderedDict

from rapidfuzz import fuzz
from app.matching.fuzzy import RapidFuzzMatcher
from app.config import settings

logger = logging.getLogger(__name__)


class DescriptionEncoder:
    def __init__(self):
        self.model = None
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.retry_after = 0
        self.warning = None

    def encode(self, descriptions):
        if not descriptions:
            return {}
        if not settings.SEMANTIC_MATCHING:
            self.warning = 'Semantic matching is disabled; description text similarity is still available.'
            return {}
        with self.lock:
            if time.monotonic() < self.retry_after:
                return {}
            try:
                if self.model is None:
                    from fastembed import TextEmbedding
                    self.model = TextEmbedding(model_name=settings.SEMANTIC_MODEL,
                                               cache_dir=settings.SEMANTIC_CACHE_DIR, threads=2)
                unique = list(dict.fromkeys(descriptions))
                missing = [d for d in unique if d not in self.cache]
                vectors = dict(zip(missing, self.model.embed(missing))) if missing else {}
                result = {d: self.cache[d] if d in self.cache else vectors[d] for d in unique}
                self.cache.update(vectors)
                while len(self.cache) > 10000:
                    self.cache.popitem(last=False)
                self.warning = None
                return result
            except Exception:
                logger.exception('Local description model unavailable')
                self.warning = 'Semantic model unavailable; suggestions use part numbers and description text similarity.'
                self.retry_after = time.monotonic() + 300
                return {}


encoder = DescriptionEncoder()


class PartMatcher:
    def __init__(self, description_encoder=encoder):
        self.encoder = description_encoder
        self.identifiers = RapidFuzzMatcher()

    def match_many(self, queries, parts, descriptions=None, top_k=5, ranking_mode='weighted', pn_weight=0.25, reranker='none'):
        descriptions = descriptions or {}
        pool = {pn: description or '' for pn, description in parts if pn}
        normalized_pool = {self.identifiers.clean_str(pn) for pn in pool}
        # Already-identifiable parts need no model inference, especially when
        # comparing the root against a large master catalogue.
        texts = [descriptions[q] for q in queries if descriptions.get(q)
                 and self.identifiers.clean_str(q) not in normalized_pool]
        vectors = self.encoder.encode(texts + [d for d in pool.values() if d]) if texts else {}
        results = {}
        for query in queries:
            description = descriptions.get(query, '')
            ranked = self.identifiers.match_item(query, list(pool), top_k=len(pool))
            for match in ranked:
                pn = match['candidate']
                candidate_description = pool[pn]
                match.update(description=candidate_description, pn_score=match['score'],
                             description_score=None, semantic_score=None,
                             requires_review=match['match_type'] != 'EXACT')
                if description and candidate_description:
                    lexical = fuzz.token_sort_ratio(description.upper(), candidate_description.upper())
                    semantic = None
                    if description in vectors and candidate_description in vectors:
                        import numpy as np
                        a, b = vectors[description], vectors[candidate_description]
                        denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
                        semantic = max(0., min(100., float(np.dot(a, b)) / denominator * 100)) if denominator else 0.
                    match['description_score'] = round(lexical, 1)
                    match['semantic_score'] = round(semantic, 1) if semantic is not None else None
                    # Exact identifiers always outrank description evidence. Similar
                    # descriptions suggest review; they do not prove interchangeability.
                    if match['match_type'] not in ('EXACT', 'EXACT_NORMALIZED'):
                        desc_score = max(lexical, semantic or 0.)
                        combined = min(98., pn_weight * match['pn_score'] + (1 - pn_weight) * desc_score)
                        if combined > match['score']:
                            match['score'] = round(combined, 1)
                            match['match_type'] = 'SEMANTIC_DESCRIPTION' if (semantic or 0.) > lexical else 'DESCRIPTION'
                if match['match_type'] == 'FUZZY':
                    match['score'] = min(98., match['score'])
            ranked.sort(key=lambda m: (m['match_type'] == 'EXACT',
                                      m['match_type'] == 'EXACT_NORMALIZED', m['score']), reverse=True)
            if ranking_mode == 'rrf':
                from app.matching.ranking import rrf_rank
                ranked = rrf_rank(ranked)
            from app.matching.reranker import rerank_candidates
            ranked = rerank_candidates(ranked, description, reranker)
            results[query] = ranked[:top_k]
        return results
