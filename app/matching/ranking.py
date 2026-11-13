"""Shared reciprocal rank fusion with explicit identifier priority."""
def rrf_rank(items, identifier='candidate'):
    fused = {m[identifier]: 0. for m in items}
    for field in ('pn_score', 'description_score', 'semantic_score'):
        eligible = [m for m in items if m.get(field) is not None and m[field] > 0]
        eligible.sort(key=lambda m: (-m[field], m[identifier]))
        previous, rank = None, 0
        for position, match in enumerate(eligible, 1):
            if match[field] != previous:
                rank = position
            previous = match[field]
            fused[match[identifier]] += 1. / (60 + rank)
    result = [dict(m, rrf_score=round(fused[m[identifier]], 8)) for m in items]
    result.sort(key=lambda m: (m['match_type'] in ('EXACT', 'EXACT PN'),
                               m['match_type'] in ('EXACT_NORMALIZED', 'NORMALIZED PN'),
                               m['rrf_score'], m.get('in_configuration', False)), reverse=True)
    return result
