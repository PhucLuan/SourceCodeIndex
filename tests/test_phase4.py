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
from langchain_core.documents import Document


class Phase4IntentTests(unittest.TestCase):
    def test_parse_slash_command_supports_core_modes(self):
        self.assertEqual(rag.parse_slash_command("/calls Ham nay goi gi?"), ("call_flow", "Ham nay goi gi?"))
        self.assertEqual(rag.parse_slash_command("/callers RequestService"), ("call_flow_reverse", "RequestService"))
        self.assertEqual(rag.parse_slash_command("/impact validateCredentials"), ("impact_analysis", "validateCredentials"))
        self.assertEqual(rag.parse_slash_command("/tour auth"), ("architecture_tour", "auth"))
        self.assertEqual(rag.parse_slash_command("/deps auth_service.py"), ("dependency", "auth_service.py"))
        self.assertEqual(rag.parse_slash_command("/search which parts handle token refresh?"), ("semantic", "which parts handle token refresh?"))

    def test_extract_tagged_payload_supports_tagged_inputs(self):
        self.assertEqual(rag.extract_tagged_payload("<symbol>RequestService</symbol>", intent="call_flow"), ("symbol", "RequestService"))
        self.assertEqual(rag.extract_tagged_payload("<file>auth_service.py</file>", intent="dependency"), ("file", "auth_service.py"))
        self.assertEqual(rag.extract_tagged_payload("<module>auth</module>", intent="architecture_tour"), ("module", "auth"))

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

        payload = rag.build_answer_payload("/calls <symbol>login</symbol>", docs)

        self.assertIn("Intent:", payload["graph_evidence"])
        self.assertIn("calls", payload["graph_evidence"])
        self.assertIn("graph LR", payload["mermaid_graph"])
        self.assertIn("login", payload["context"])
        self.assertEqual(payload["intent"], "call_flow")

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
            payload = rag.build_answer_payload("/calls <symbol>validate</symbol>", docs)

        self.assertIn("validate", payload["context"])
        self.assertIn("save", payload["graph_evidence"])
        self.assertIn("graph LR", payload["mermaid_graph"])


class Phase4RetrievalTests(unittest.TestCase):
    def setUp(self):
        rag.st.session_state.clear()

    def test_callsite_query_uses_call_graph_edges(self):
        edge = {
            "id": "edge-1",
            "repo_name": "repo",
            "filename": "src/client.py",
            "lang": "python",
            "edge_type": "calls",
            "resolution_status": "resolved",
            "confidence": 0.9,
            "source_puid": "repo::src/client.py::function::handle",
            "target_puid": "repo::src/request.py::class::RequestService",
            "source_symbol": "handle",
            "target_symbol": "RequestService",
            "source_line": 42,
            "target_line": 0,
            "metadata": "callee=RequestService",
        }
        caller_node = {
            "filename": "src/client.py",
            "lang": "python",
            "text": "def handle():\n    RequestService()",
            "score": 1.0,
            "start_line": 40,
            "end_line": 45,
            "is_test": False,
            "node_type": "function",
            "node_name": "handle",
            "qualified_name": "handle",
            "signature": "handle()",
            "docstring": "",
            "modifiers": "",
            "export_status": "",
            "source_span": "L40-L45",
            "puid": "repo::src/client.py::function::handle",
            "parent_puid": "",
            "is_skeleton": False,
            "repo_name": "repo",
        }

        with patch("rag.fetch_call_edges_by_symbol", return_value=[edge]) as edge_lookup, \
             patch("rag.fetch_nodes", return_value=[caller_node]):
            docs = rag.query_cocoindex_db(
                "/callers <symbol>RequestService</symbol>",
                top_k=5,
                use_query_expansion=False,
                use_hybrid=True,
            )

        edge_lookup.assert_called_once()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].metadata["node_name"], "handle")
        self.assertEqual(docs[0].metadata["score_type"], "graph_callsite")
        self.assertEqual(rag.st.session_state.last_query_intent, "call_flow_reverse")
        self.assertEqual(rag.st.session_state.graph_seed_edges[0]["source_line"], 42)


if __name__ == "__main__":
    unittest.main()
