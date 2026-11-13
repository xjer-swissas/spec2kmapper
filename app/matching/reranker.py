"""Optional bounded description reranking; original evidence stays intact."""
import logging
import math
import threading
import time

from app.config import settings

log = logging.getLogger(__name__)
WARNING = 'Cross-encoder unavailable; suggestions retain the initial ranking.'
IDENTIFIED = {'EXACT', 'EXACT_NORMALIZED', 'EXACT PN', 'NORMALIZED PN'}


class CrossEncoder:
    def __init__(self):
        self.model = None
        self.lock = threading.Lock()
        self.retry_after = 0

    def score(self, query, descriptions):
        with self.lock:
            if time.monotonic() < self.retry_after:
                raise RuntimeError(WARNING)
            try:
                if self.model is None:
                    from fastembed.rerank.cross_encoder import TextCrossEncoder
                    self.model = TextCrossEncoder(settings.RERANK_MODEL,
                        cache_dir=settings.RERANK_CACHE_DIR, threads=2)
                scores = [float(s) for s in self.model.rerank(query, descriptions)]
                if len(scores) != len(descriptions) or not all(math.isfinite(s) for s in scores):
                    raise ValueError('Invalid cross-encoder scores')
                return scores
            except Exception:
                self.retry_after = time.monotonic() + 300
                log.exception('Cross-encoder inference unavailable')
                raise


cross_encoder = CrossEncoder()


def rerank_candidates(items, description, mode='none'):
    if mode not in ('none', 'cross_encoder'):
        raise ValueError('Unknown reranker')
    if mode == 'none' or not description or not description.strip():
        return items
    # Preserve slots for exact/normalized PNs and missing descriptions. Never
    # promote unscored items or compare relevance logits with other scores.
    slots = [i for i, m in enumerate(items[:settings.RERANK_TOP_N])
             if m['match_type'] not in IDENTIFIED and (m.get('description') or '').strip()]
    if len(slots) < 2:
        return items
    try:
        scores = cross_encoder.score(description.strip(), [items[i]['description'].strip() for i in slots])
    except Exception:
        return [dict(m, reranker_warning=WARNING) for m in items]
    scored = [dict(items[i], cross_encoder_score=s, reranker='cross_encoder') for i, s in zip(slots, scores)]
    scored.sort(key=lambda m: m['cross_encoder_score'], reverse=True)
    result = list(items)
    for i, m in zip(slots, scored):
        result[i] = m
    return result


def reranker_warning(groups):
    return next((m['reranker_warning'] for group in groups for m in group if m.get('reranker_warning')), None)
