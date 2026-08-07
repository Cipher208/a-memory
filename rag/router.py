"""
Retrieval Router — async routes queries to the right memory strategy
Multi-signal: FTS5 + Vector + Entity/NER extraction
"""

import re
from enum import Enum
from typing import Any

from .engine import RAGEngine


class Strategy(str, Enum):
    L1_BUFFER = "l1_buffer"
    SEMANTIC = "semantic"
    GRAPH = "graph"
    WIKI = "wiki"


class RouterResult:
    def __init__(self, strategy: Strategy, context: list[dict[str, Any]], confidence: float):
        self.strategy = strategy
        self.context = context
        self.confidence = confidence


# B2.1-B2.3: Data-driven keyword tables (RU + EN)
_DEFAULT_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "recent": {
        "ru": ["это", "почему", "как", "только что", "ранее", "опять", "сейчас", "вчера", "утром", "снова"],
        "en": ["this", "why", "how", "earlier", "just now", "again", "now", "yesterday", "recently"],
    },
    "wiki": {
        "ru": [
            "документация",
            "настроить",
            "архитектура",
            "баг",
            "конфиг",
            "функция",
            "класс",
            "модуль",
            "сервис",
            "api",
            "handler",
            "руководство",
            "гайд",
            "инструкция",
            "как сделать",
        ],
        "en": [
            "documentation",
            "tutorial",
            "how to",
            "guide",
            "manual",
            "rtfm",
            "wiki",
            "configure",
            "setup",
            "module",
            "class",
            "handler",
            "service",
            "endpoint",
            "function",
        ],
    },
    "graph": {
        "ru": ["связи", "связано", "граф", "паттерн", "ошибка", "решение", "почему выбрал", "взаимосвязь", "зависит"],
        "en": ["relation", "graph", "pattern", "decision", "learned", "depends on", "linked to", "follows from", "error_pattern"],
    },
}

# B2.4: Entity stopwords — short tokens that are valid abbreviations (not noise)
_ENTITY_STOPWORDS = {
    "ai",
    "ml",
    "js",
    "ts",
    "go",
    "rs",
    "py",
    "ci",
    "cd",
    "ui",
    "ux",
    "io",
    "db",
    "os",
    "vm",
    "ip",
    "id",
    "ok",
    "no",
    "en",
    "ru",
    "es",
    "кб",
    "мб",
    "гб",
}

# B2.5: Route priorities — data-driven table
_ROUTE_TABLE: list[dict[str, Any]] = [
    {"strategy": "L1_BUFFER", "keywords": "recent", "confidence": 0.9},
    {"strategy": "WIKI", "keywords": "wiki", "confidence": 0.95},
    {"strategy": "GRAPH", "keywords": "entity", "confidence": 0.85},
    {"strategy": "GRAPH", "keywords": "graph", "confidence": 0.85},
    {"strategy": "SEMANTIC", "keywords": None, "confidence": 0.8},
]


class RetrievalRouter:
    def __init__(self, layer: str = "user", user_id: str = "default", keyword_overrides: dict | None = None, recent_max_chars: int = 60):
        self.layer = layer
        self.user_id = user_id
        self.recent_max_chars = recent_max_chars
        self._rag = RAGEngine(layer=layer)
        self._persona_keywords = {"кто ты", "расскажи о себе", "как тебя зовут"}
        # Merge default + overrides
        self.keywords = {**_DEFAULT_KEYWORDS, **(keyword_overrides or {})}

        # Entity patterns (Russian + English)
        self._entity_patterns = [
            (r"\b([А-ЯЁ][а-яё]+)", "ru"),
            (r"\b([A-Z][a-z]+)", "en"),
            (r"\b(\w+)\.(py|js|ts|go|rs|sql|md|yaml|yml)", "file"),
            (r"\b(redis|sqlite|postgres|mysql|mongo|docker|kubernetes|aws|gcp|azure)", "tech"),
            (r"\b(python|javascript|typescript|go|rust|java|kotlin|swift)", "lang"),
        ]

    def _flat_keywords(self, kind: str) -> list[str]:
        """Flatten RU + EN keyword lists for a given kind."""
        v = self.keywords.get(kind, {})
        out = []
        if isinstance(v, dict):
            for lang_list in v.values():
                out.extend(lang_list)
        elif isinstance(v, list):
            out.extend(v)
        return [kw.lower() for kw in out]

    async def route(self, query: str, recent_context: list[dict] | None = None) -> RouterResult:
        q = query.lower()

        for route in _ROUTE_TABLE:
            strategy = Strategy[route["strategy"]]
            confidence = route["confidence"]
            keyword_kind = route["keywords"]

            result = await self._match_route(q, query, keyword_kind, strategy, confidence, recent_context)
            if result is not None:
                return result

        return RouterResult(Strategy.SEMANTIC, [], 0.0)

    async def _match_route(
        self,
        q_lower: str,
        query: str,
        keyword_kind: str | None,
        strategy: Strategy,
        confidence: float,
        recent_context: list[dict] | None,
    ) -> RouterResult | None:
        """Try to match a single route. Returns RouterResult or None."""
        if keyword_kind == "recent":
            if self._is_recent_query(q_lower) and recent_context:
                return RouterResult(strategy, recent_context, confidence)

        elif keyword_kind == "wiki":
            if self._is_wiki_query(q_lower):
                return await self._route_wiki(query, strategy, confidence)

        elif keyword_kind == "entity":
            entities = self._extract_entities(query)
            if entities:
                return await self._route_entities(entities, strategy, confidence)

        elif keyword_kind == "graph":
            if self._is_graph_query(q_lower):
                return await self._route_graph(q_lower, strategy, confidence)

        elif keyword_kind is None:
            results = await self._rag.search(query, self.user_id, strategy="hybrid", limit=3)
            if results:
                return RouterResult(strategy, results, confidence)

        return None

    async def _route_wiki(self, query: str, strategy: Strategy, confidence: float) -> RouterResult:
        results = await self._rag.search(query, self.user_id, strategy="hybrid", limit=3)
        if results:
            page_id = results[0]["id"]
            relations = await self._rag.get_relations(page_id, depth=1)
            if relations:
                results.append(
                    {
                        "title": "Relations",
                        "content": "\n".join([f"- {r['title']} [{r['relation']}]" for r in relations]),
                        "score": 0.7,
                    }
                )
            return RouterResult(strategy, results, confidence)
        return RouterResult(strategy, [], 0.0)

    async def _route_entities(self, entities: set[str], strategy: Strategy, confidence: float) -> RouterResult | None:
        from graph.epistemic import EpistemicGraph

        graph = EpistemicGraph(layer=self.layer)
        entity_results = []
        for entity in entities:
            nodes = await graph.query_by_tag(self.user_id, entity, limit=3)
            entity_results.extend([{"title": n.content, "type": n.node_type, "tags": n.tags, "entity": entity} for n in nodes])
        if entity_results:
            return RouterResult(strategy, entity_results, confidence)
        return None

    async def _route_graph(self, query: str, strategy: Strategy, confidence: float) -> RouterResult | None:
        from graph.epistemic import EpistemicGraph

        graph = EpistemicGraph(layer=self.layer)
        graph_kw = self._flat_keywords("graph")
        for tag in graph_kw:
            if tag in query:
                nodes = await graph.query_by_tag(self.user_id, tag, limit=5)
                if nodes:
                    context = [{"title": n.content, "type": n.node_type, "tags": n.tags} for n in nodes]
                    return RouterResult(strategy, context, confidence)
        for node_type in ["decision_log", "error_analysis", "fact"]:
            if node_type.replace("_", " ") in query:
                nodes = await graph.query_by_type(self.user_id, node_type, limit=5)
                if nodes:
                    context = [{"title": n.content, "type": n.node_type, "tags": n.tags} for n in nodes]
                    return RouterResult(strategy, context, confidence)
        return None

    def _extract_entities(self, query: str) -> set[str]:
        """B2.4: Extract named entities — stopword whitelist instead of length filter."""
        entities: set[str] = set()
        for pat, _ in self._entity_patterns:
            for m in re.finditer(pat, query):
                tok = m.group(0).lower().rstrip(".")
                if len(tok) < 2:
                    continue
                if tok in _ENTITY_STOPWORDS:
                    continue
                entities.add(tok)
        return entities

    def _is_recent_query(self, query: str) -> bool:
        if len(query) > self.recent_max_chars:
            return False
        q = query.lower()
        return any(kw in q for kw in self._flat_keywords("recent"))

    def _is_wiki_query(self, query: str) -> bool:
        q = query.lower()
        return any(kw in q for kw in self._flat_keywords("wiki"))

    def _is_graph_query(self, query: str) -> bool:
        q = query.lower()
        return any(kw in q for kw in self._flat_keywords("graph"))
