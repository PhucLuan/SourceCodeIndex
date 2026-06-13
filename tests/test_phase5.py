import sys
import sys
import types
import unittest
from unittest.mock import patch


if "langchain_core" not in sys.modules:
    langchain_core = types.ModuleType("langchain_core")
    langchain_core_documents = types.ModuleType("langchain_core.documents")
    langchain_core_prompts = types.ModuleType("langchain_core.prompts")

    class Document:
        def __init__(self, page_content="", metadata=None):
            self.page_content = page_content
            self.metadata = metadata or {}

    class PromptTemplate:
        @staticmethod
        def from_template(template):
            return template

    langchain_core_documents.Document = Document
    langchain_core_prompts.PromptTemplate = PromptTemplate
    sys.modules["langchain_core"] = langchain_core
    sys.modules["langchain_core.documents"] = langchain_core_documents
    sys.modules["langchain_core.prompts"] = langchain_core_prompts

if "langchain_ollama" not in sys.modules:
    langchain_ollama = types.ModuleType("langchain_ollama")

    class OllamaLLM:
        def __init__(self, *args, **kwargs):
            pass

    langchain_ollama.OllamaLLM = OllamaLLM
    sys.modules["langchain_ollama"] = langchain_ollama

if "streamlit" not in sys.modules:
    streamlit = types.ModuleType("streamlit")

    class _SessionState(dict):
        def clear(self):
            super().clear()

    streamlit.session_state = _SessionState()

    def _noop(*args, **kwargs):
        return None

    streamlit.error = _noop
    sys.modules["streamlit"] = streamlit

if "asyncpg" not in sys.modules:
    asyncpg = types.ModuleType("asyncpg")

    async def _unavailable(*args, **kwargs):
        raise RuntimeError("asyncpg is not available in this test stub")

    asyncpg.create_pool = _unavailable
    sys.modules["asyncpg"] = asyncpg

if "indexer_flow" not in sys.modules:
    indexer_flow = types.ModuleType("indexer_flow")
    indexer_flow.DATABASE_URL = "postgres://stub"
    indexer_flow.PG_SCHEMA = "public"
    indexer_flow.TABLE_NAME = "nodes"
    indexer_flow.fetch_edges_by_puid = lambda *args, **kwargs: []
    indexer_flow.fetch_nodes = lambda *args, **kwargs: []
    indexer_flow.fulltext_search = lambda *args, **kwargs: []
    indexer_flow.fulltext_search_per_repo = lambda *args, **kwargs: []
    indexer_flow.get_all_repo_names = lambda: []
    indexer_flow.get_graph_edge_table_name = lambda table_name: f"{table_name}_graph_edges"
    indexer_flow.rrf_merge = lambda left, right, k=60: list(left) + [x for x in right if x not in left]
    indexer_flow.search = lambda *args, **kwargs: []
    indexer_flow.search_per_repo = lambda *args, **kwargs: []
    sys.modules["indexer_flow"] = indexer_flow

import rag
import graph_traversal
from langchain_core.documents import Document


class Phase5CommandParsingTests(unittest.TestCase):
    def test_parse_slash_command_supports_tagged_inputs(self):
        self.assertEqual(rag.parse_slash_command("/calls <symbol>RequestService</symbol>"), ("call_flow", "<symbol>RequestService</symbol>"))
        self.assertEqual(rag.parse_slash_command("/impact <file>auth_service.py</file>"), ("impact_analysis", "<file>auth_service.py</file>"))
        self.assertEqual(rag.parse_slash_command("/tour <module>auth</module>"), ("architecture_tour", "<module>auth</module>"))
        self.assertEqual(rag.parse_slash_command("/search <query>which parts handle token refresh?</query>"), ("semantic", "<query>which parts handle token refresh?</query>"))

    def test_extract_tagged_payload_uses_expected_tag_first(self):
        self.assertEqual(rag.extract_tagged_payload("<symbol>RequestService</symbol>", intent="call_flow"), ("symbol", "RequestService"))
        self.assertEqual(rag.extract_tagged_payload("<file>auth_service.py</file>", intent="dependency"), ("file", "auth_service.py"))
        self.assertEqual(rag.extract_tagged_payload("<query>hello</query>", intent="semantic"), ("query", "hello"))

    def test_extract_tagged_payload_falls_back_to_raw_text(self):
        self.assertEqual(rag.extract_tagged_payload("RequestService", intent="call_flow"), ("", "RequestService"))
        self.assertEqual(rag.extract_tagged_payload("", intent="call_flow"), ("", ""))


class Phase5PayloadTests(unittest.TestCase):
    def setUp(self):
        rag.st.session_state.clear()

    def test_build_answer_payload_reports_tagged_payload(self):
        docs = [
            Document(
                page_content="def login(): pass",
                metadata={
                    "filename": "a.py",
                    "start_line": 1,
                    "end_line": 3,
                    "score": 0.8,
                    "is_test": False,
                    "node_type": "function",
                    "node_name": "login",
                    "qualified_name": "login",
                    "signature": "login()",
                    "source_span": "L1-L3",
                    "docstring": "",
                    "modifiers": "",
                    "puid": "repo::a.py::function::login",
                    "is_skeleton": False,
                },
            )
        ]

        payload = rag.build_answer_payload("/calls <symbol>RequestService</symbol>", docs)

        self.assertEqual(payload["intent"], "call_flow")
        self.assertIn("Payload type: symbol", payload["graph_evidence"])
        self.assertIn("RequestService", payload["graph_evidence"])

    def test_lookup_symbol_fuzzy_uses_pg_trgm_similarity(self):
        captured = {}

        class _Pool:
            async def fetch(self, query, *params):
                captured["query"] = query
                captured["params"] = params
                return []

        class _PoolCtx:
            async def __aenter__(self):
                return _Pool()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with patch("rag.asyncpg.create_pool", return_value=_PoolCtx()):
            rag.lookup_symbol("RequestService", fuzzy=True)

        self.assertIn("similarity(", captured["query"])
        self.assertIn("% $2", captured["query"])
        self.assertEqual(captured["params"][0], "RequestService")

    def test_semantic_queries_seed_graph_context(self):
        seed_result = [
            {
                "puid": "repo::a.py::function::login",
                "filename": "a.py",
                "lang": "python",
                "text": "def login(): pass",
                "score": 0.9,
                "start_line": 1,
                "end_line": 3,
                "node_type": "function",
                "node_name": "login",
                "qualified_name": "login",
                "signature": "login()",
                "docstring": "",
                "modifiers": "",
                "repo_name": "repo",
            }
        ]

        with patch("rag._search", return_value=seed_result), \
             patch("rag._fulltext_search", return_value=[]), \
             patch("rag.fetch_edges_by_puid", return_value=[
                 {
                     "source_puid": "repo::a.py::function::login",
                     "target_puid": "repo::b.py::function::validate",
                     "edge_type": "contains",
                     "resolution_status": "resolved",
                     "confidence": 0.9,
                 }
             ]), \
             patch("rag.get_all_repo_names", return_value=[]):
            docs = rag.query_cocoindex_db(
                "/search <query>which parts handle token refresh?</query>",
                top_k=5,
                use_query_expansion=False,
                use_hybrid=True,
            )

        self.assertGreaterEqual(len(docs), 1)
        self.assertTrue(rag.st.session_state.graph_seed_edges)
        self.assertEqual(rag.st.session_state.last_query_intent, "semantic")

    def test_run_impact_bfs_finds_callers(self):
        with patch("graph_traversal._fetch_all_edges_sync", return_value=[
            {
                "source_puid": "repo::a.py::function::handle",
                "target_puid": "repo::b.py::function::RequestService",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.9,
                "source_symbol": "handle",
                "target_symbol": "RequestService",
                "filename": "a.py",
            }
        ]):
            result = graph_traversal.run_impact_bfs(["repo::b.py::function::RequestService"], max_depth=2)

        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["affected_nodes"][0]["puid"], "repo::a.py::function::handle")

    def test_impact_query_runs_bfs_and_populates_session_state(self):
        seed_nodes = [
            {
                "puid": "repo::b.py::function::RequestService",
                "filename": "b.py",
                "repo_name": "repo",
                "node_type": "function",
                "node_name": "RequestService",
                "qualified_name": "RequestService",
                "text": "def RequestService(): pass",
                "score": 1.0,
            }
        ]
        impact_result = {
            "affected_nodes": [
                {
                    "puid": "repo::a.py::function::handle",
                    "node_name": "handle",
                    "filename": "a.py",
                    "depth": 1,
                    "via_edge_type": "calls",
                    "path": ["repo::b.py::function::RequestService", "repo::a.py::function::handle"],
                }
            ],
            "edges": [
                ("repo::a.py::function::handle", "repo::b.py::function::RequestService", "calls", 1)
            ],
            "max_depth_reached": False,
            "total_count": 1,
        }
        with patch("rag.lookup_symbol", return_value=seed_nodes), \
             patch("rag.run_impact_bfs", return_value=impact_result), \
             patch("rag.fetch_nodes", return_value=[
                 {
                     "puid": "repo::a.py::function::handle",
                     "filename": "a.py",
                     "node_name": "handle",
                     "qualified_name": "handle",
                     "score": 0.96,
                     "score_type": "graph_impact",
                     "query_intent": "impact_analysis",
                 }
             ]), \
             patch("rag.get_all_repo_names", return_value=["repo"]):
            docs = rag.query_cocoindex_db(
                "/impact <symbol>RequestService</symbol>",
                top_k=5,
                use_query_expansion=False,
                use_hybrid=True,
            )

        self.assertGreaterEqual(len(docs), 1)
        self.assertEqual(rag.st.session_state.last_query_intent, "impact_analysis")
        self.assertEqual(rag.st.session_state.impact_result["total_count"], 1)
        self.assertTrue(rag.st.session_state.graph_seed_edges)
