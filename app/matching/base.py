from abc import ABC, abstractmethod
from typing import List, Dict, Any

class BaseMatcher(ABC):
    @abstractmethod
    def match_item(self, query: str, candidates: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Returns sorted list of match dicts:
        [
            {"candidate": "CFM56-5C4/P", "score": 100.0, "match_type": "EXACT"},
            {"candidate": "CFM56-5C4", "score": 90.5, "match_type": "FUZZY"}
        ]
        """
        pass