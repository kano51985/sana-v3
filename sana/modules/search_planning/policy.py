"""Versioned limits for plan validation and query compilation."""

from dataclasses import dataclass

from sana.modules.orchestration.domain import SearchMode


@dataclass(frozen=True, slots=True)
class SearchPlanningPolicy:
    version: str = "search-v4"
    max_facts: int = 8
    max_query_characters: int = 64
    fast_max_queries_per_fact: int = 1
    research_max_queries_per_fact: int = 3
    fast_max_queries: int = 4
    research_initial_max_queries: int = 8
    research_total_max_queries: int = 12

    def query_limit(self, mode: SearchMode, *, expansion: bool = False) -> int:
        if mode is SearchMode.FAST:
            return self.fast_max_queries
        return (
            self.research_total_max_queries
            if expansion
            else self.research_initial_max_queries
        )
