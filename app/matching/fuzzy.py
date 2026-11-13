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
            elif clean_q and clean_q == clean_c:
                score = 99.0
                mtype = "EXACT_NORMALIZED"
            else:
                ratio = fuzz.ratio(query.strip().upper(), cand.strip().upper())
                token_sort = fuzz.token_sort_ratio(query.strip().upper(), cand.strip().upper())
                partial = fuzz.partial_ratio(query.strip().upper(), cand.strip().upper())
                # Weighted blend
                score = round((ratio * 0.5) + (token_sort * 0.3) + (partial * 0.2), 1)
                mtype = "FUZZY"

            results.append({"candidate": cand, "score": score, "match_type": mtype})

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
