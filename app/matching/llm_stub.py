from typing import List, Dict, Any
from app.matching.base import BaseMatcher

class LLMMatcher(BaseMatcher):
    """Modular LLM-based matching interface (Ready for OpenAI / Anthropic / Local LLM integration)."""
    def __init__(self, api_key: str = None):
        self.api_key = api_key

    def match_item(self, query: str, candidates: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        # Future: Pass candidates & query into an LLM context prompt for semantic part matching
        return []