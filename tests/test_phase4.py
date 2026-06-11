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
from langchain_core.documents import Document


class Phase4IntentTests(unittest.TestCase):
    def test_detect_query_intent_symbol_lookup(self):
        self.assertEqual(rag.detect_query_intent("Ham AddAsync o dau?"), "symbol lookup")

    def test_detect_query_intent_call_flow(self):
        self.assertEqual(rag.detect_query_intent("Ham nay goi gi?"), "call flow")

    def test_detect_query_intent_impact_analysis(self):
        self.assertEqual(rag.detect_query_intent("Sua file nay anh huong gi?"), "impact analysis")

    def test_detect_query_intent_business_flow(self):
        self.assertEqual(rag.detect_query_intent("Luong xu ly login"), "domain/business flow")

    def test_edge_types_per_intent(self):
        self.assertIn("calls", rag.get_graph_edge_types_for_intent("call flow"))
        self.assertIn("imports", rag.get_graph_edge_types_for_intent("dependency"))
        self.assertIn("contains", rag.get_graph_edge_types_for_intent("symbol lookup"))


class Phase4PromptTests(unittest.TestCase):
    def setUp(self):
        rag.st.session_state.clear()

    def test_build_answer_payload_includes_graph_evidence_and_mermaid(self):
        rag.st.session_state.graph_seed_edges = [
            {
                "source_puid": "repo::a.py::function::login",
                "target_puid": "repo::b.py::function::validate",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.91,
            }
        ]
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

        payload = rag.build_answer_payload("Ham login goi nhung ham nao?", docs)

        self.assertIn("Intent:", payload["graph_evidence"])
        self.assertIn("calls", payload["graph_evidence"])
        self.assertIn("graph LR", payload["mermaid_graph"])
        self.assertIn("login", payload["context"])
        self.assertEqual(payload["intent"], "call flow")

    def test_build_answer_payload_fetches_edges_for_doc_puid(self):
        docs = [
            Document(
                page_content="def validate(): pass",
                metadata={
                    "filename": "b.py",
                    "start_line": 10,
                    "end_line": 20,
                    "score": 0.6,
                    "is_test": False,
                    "node_type": "function",
                    "node_name": "validate",
                    "qualified_name": "validate",
                    "signature": "validate()",
                    "source_span": "L10-L20",
                    "docstring": "",
                    "modifiers": "",
                    "puid": "repo::b.py::function::validate",
                    "is_skeleton": False,
                },
            )
        ]

        with patch("rag.fetch_edges_by_puid", return_value=[
            {
                "source_puid": "repo::b.py::function::validate",
                "target_puid": "repo::c.py::function::save",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.8,
            }
        ]):
            payload = rag.build_answer_payload("validate goi gi?", docs)

        self.assertIn("validate", payload["context"])
        self.assertIn("save", payload["graph_evidence"])
        self.assertIn("graph LR", payload["mermaid_graph"])


if __name__ == "__main__":
    unittest.main()
