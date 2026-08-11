"""AppContext definition."""

from core import MemoryManager
from features.audit_trail import AuditTrail
from features.backup import BackupManager
from features.import_export import ImportExport
from features.rate_limiting import RateLimiter
from graph.epistemic import EpistemicGraph
from graph.temporal import TemporalGraph
from lifecycle.emotion import EmotionTrigger, EmotionEngine, load_emotion_config
from lifecycle.forgetting import ForgettingSystem
from lifecycle.consolidation import ConsolidationEngine
from rag.engine import RAGEngine
from rag.multi_source import MultiSourceRAG
from shared.cache import MemoryCache
from wiki import WikiManager


class AppContext:
    def __init__(self):
        self.cache = MemoryCache()
        self.mm = MemoryManager(cache=self.cache)
        self.user_wiki = WikiManager(layer="user")
        self.agent_wiki = WikiManager(layer="agent")
        self.user_rag = RAGEngine(layer="user")
        self.agent_rag = RAGEngine(layer="agent")
        self.user_multi = MultiSourceRAG(self.user_rag, self.user_wiki)
        self.agent_multi = MultiSourceRAG(self.agent_rag, self.agent_wiki)
        self.user_graph = EpistemicGraph(layer="user")
        self.agent_graph = EpistemicGraph(layer="agent")
        self.temporal = TemporalGraph()
        self.forgetting = ForgettingSystem()

        self.emotion_config = load_emotion_config()
        self.emotion_engine = EmotionEngine(config=self.emotion_config)
        self.emotion_trigger = EmotionTrigger(self.emotion_engine)

        self.consolidation = ConsolidationEngine()
        self.audit = AuditTrail()
        self.rate_limiter = RateLimiter()
        self.backup = BackupManager()
        self.import_export = ImportExport()

        from hooks import hook_registry
        from hooks.agent_hooks import AgentHooks
        from hooks.user_hooks import UserHooks

        self.hook_registry = hook_registry

        self.user_hooks = UserHooks()
        self.agent_hooks = AgentHooks()
        self.hook_registry.register_instance(self.user_hooks)
        self.hook_registry.register_instance(self.agent_hooks)
