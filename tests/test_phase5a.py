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


class ImpactTagParsingTests(unittest.TestCase):
    def test_extract_tagged_payload_supports_all_impact_tags(self):
        self.assertEqual(
            rag.extract_tagged_payload("<class>AuthService</class>", intent="impact_analysis"),
            ("class", "AuthService"),
        )
        self.assertEqual(
            rag.extract_tagged_payload("<method>AuthService.validate</method>", intent="impact_analysis"),
            ("method", "AuthService.validate"),
        )
        self.assertEqual(
            rag.extract_tagged_payload("<file>src/auth/service.py</file>", intent="impact_analysis"),
            ("file", "src/auth/service.py"),
        )
        self.assertEqual(
            rag.extract_tagged_payload("<field>User.email</field>", intent="impact_analysis"),
            ("field", "User.email"),
        )
        self.assertEqual(
            rag.extract_tagged_payload("validateCredentials", intent="impact_analysis"),
            ("", "validateCredentials"),
        )


class ImpactSeedResolutionTests(unittest.TestCase):
    def test_resolve_impact_seed_candidates_splits_method_qualifier(self):
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
            rag.resolve_impact_seed_candidates("method", "AuthService.validate")

        self.assertIn("node_type = ANY(", captured["query"])
        self.assertIn("validate", captured["params"])
        self.assertIn("%AuthService.validate", captured["params"])


class ImpactAnalysisResultContractTests(unittest.TestCase):
    def setUp(self):
        rag.st.session_state.clear()

    def test_no_seed_status_when_nothing_resolves(self):
        with patch("rag.resolve_impact_seed_candidates", return_value=[]), \
             patch("rag.lookup_symbol", return_value=[]):
            result = rag.build_impact_analysis_result("symbol", "missingThing")

        self.assertEqual(result["status"], "no_seed")
        self.assertTrue(result["warnings"])
        self.assertEqual(result["tree"], [])

    def test_ambiguous_seed_status_with_multiple_candidates(self):
        candidates = [
            {"puid": "repo::a.py::function::validate", "node_type": "function", "node_name": "validate", "filename": "a.py"},
            {"puid": "repo::b.py::function::validate", "node_type": "function", "node_name": "validate", "filename": "b.py"},
        ]
        with patch("rag.resolve_impact_seed_candidates", return_value=candidates):
            result = rag.build_impact_analysis_result("symbol", "validate")

        self.assertEqual(result["status"], "ambiguous_seed")
        self.assertEqual(len(result["seed_candidates"]), 2)

    def test_field_seed_resolves_via_reads_writes_edges(self):
        # Property/field read-write edges are now extracted (member-access
        # tracking) - a <field> seed should reach "ok" via real 'reads'/
        # 'writes' evidence instead of the old hardcoded "not extracted yet"
        # rejection, and must not be flagged as an unsupported change type.
        candidates = [
            {"puid": "repo::a.py::class::User::email", "node_type": "field", "node_name": "email", "filename": "a.py"},
        ]
        bfs_result = {
            "affected_nodes": [
                {"puid": "repo::b.py::function::format_user", "node_name": "format_user", "filename": "b.py", "depth": 1, "via_edge_type": "reads", "path": []}
            ],
            "edges": [
                {
                    "source_puid": "repo::b.py::function::format_user",
                    "source_symbol": "format_user",
                    "source_file": "b.py",
                    "source_line": 5,
                    "target_puid": "repo::a.py::class::User::email",
                    "target_symbol": "email",
                    "target_file": "a.py",
                    "target_line": 1,
                    "edge_type": "reads",
                    "resolution_status": "resolved",
                    "confidence": 0.8,
                    "metadata": "",
                    "depth": 1,
                    "path": [],
                }
            ],
            "max_depth_reached": False,
            "total_count": 1,
        }
        with patch("rag.resolve_impact_seed_candidates", return_value=candidates), \
             patch("rag.run_impact_bfs", return_value=bfs_result):
            result = rag.build_impact_analysis_result("field", "User.email")

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("field_read_write", result["unsupported_change_types"])

    def test_ok_status_with_single_resolved_seed(self):
        seed = [{"puid": "repo::b.py::function::validate", "node_type": "function", "node_name": "validate", "filename": "b.py"}]
        bfs_result = {
            "affected_nodes": [
                {"puid": "repo::a.py::function::handle", "node_name": "handle", "filename": "a.py", "depth": 1, "via_edge_type": "calls", "path": []}
            ],
            "edges": [
                {
                    "source_puid": "repo::a.py::function::handle",
                    "source_symbol": "handle",
                    "source_file": "a.py",
                    "source_line": 5,
                    "target_puid": "repo::b.py::function::validate",
                    "target_symbol": "validate",
                    "target_file": "b.py",
                    "target_line": 1,
                    "edge_type": "calls",
                    "resolution_status": "resolved",
                    "confidence": 0.9,
                    "metadata": "",
                    "depth": 1,
                    "path": [],
                }
            ],
            "max_depth_reached": False,
            "total_count": 1,
        }
        with patch("rag.resolve_impact_seed_candidates", return_value=seed), \
             patch("rag.run_impact_bfs", return_value=bfs_result):
            result = rag.build_impact_analysis_result("function", "validate")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["tree"]), 1)
        self.assertEqual(result["edges"][0]["source_line"], 5)
        self.assertGreaterEqual(result["confidence_summary"]["min_confidence"], 0.8)


class StrictImpactBfsTests(unittest.TestCase):
    def test_strict_mode_excludes_unresolved_and_low_confidence_edges(self):
        edges = [
            {
                "source_puid": "repo::good.py::function::caller",
                "target_puid": "repo::b.py::function::target",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.95,
                "source_symbol": "caller",
                "target_symbol": "target",
                "filename": "good.py",
                "source_line": 12,
            },
            {
                "source_puid": "repo::bad.py::function::low_conf_caller",
                "target_puid": "repo::b.py::function::target",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.5,
                "source_symbol": "low_conf_caller",
                "target_symbol": "target",
                "filename": "bad.py",
                "source_line": 1,
            },
            {
                "source_puid": "repo::amb.py::function::ambiguous_caller",
                "target_puid": "repo::b.py::function::target",
                "edge_type": "calls",
                "resolution_status": "ambiguous",
                "confidence": 0.9,
                "source_symbol": "ambiguous_caller",
                "target_symbol": "target",
                "filename": "amb.py",
                "source_line": 1,
            },
        ]
        with patch("graph_traversal._fetch_all_edges_sync", return_value=edges):
            result = graph_traversal.run_impact_bfs(["repo::b.py::function::target"], max_depth=2, mode="strict")

        puids = {n["puid"] for n in result["affected_nodes"]}
        self.assertEqual(puids, {"repo::good.py::function::caller"})
        self.assertEqual(result["total_count"], 1)

    def test_strict_mode_edges_carry_line_level_evidence(self):
        edges = [
            {
                "source_puid": "repo::good.py::function::caller",
                "target_puid": "repo::b.py::function::target",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.95,
                "source_symbol": "caller",
                "target_symbol": "target",
                "filename": "good.py",
                "source_line": 12,
                "target_line": 1,
            }
        ]
        with patch("graph_traversal._fetch_all_edges_sync", return_value=edges):
            result = graph_traversal.run_impact_bfs(["repo::b.py::function::target"], max_depth=2, mode="strict")

        evidence = result["edges"][0]
        self.assertEqual(evidence["source_puid"], "repo::good.py::function::caller")
        self.assertEqual(evidence["target_puid"], "repo::b.py::function::target")
        self.assertEqual(evidence["source_line"], 12)
        self.assertEqual(evidence["edge_type"], "calls")
        self.assertEqual(evidence["resolution_status"], "resolved")
        self.assertEqual(evidence["depth"], 1)

    def test_strict_mode_does_not_use_substring_symbol_fallback(self):
        edges = [
            {
                "source_puid": "repo::a.py::function::caller",
                "target_puid": "",
                "edge_type": "calls",
                "resolution_status": "unresolved",
                "confidence": 0.3,
                "source_symbol": "caller",
                "target_symbol": "targetHelperLongName",
                "filename": "a.py",
                "source_line": 3,
            }
        ]
        with patch("graph_traversal._fetch_all_edges_sync", return_value=edges):
            result = graph_traversal.run_impact_bfs(
                ["repo::b.py::function::target"], start_symbols=["target"], max_depth=2, mode="strict"
            )

        self.assertEqual(result["total_count"], 0)

    def test_exploratory_mode_allows_substring_symbol_fallback(self):
        edges = [
            {
                "source_puid": "repo::a.py::function::caller",
                "target_puid": "",
                "edge_type": "calls",
                "resolution_status": "unresolved",
                "confidence": 0.3,
                "source_symbol": "caller",
                "target_symbol": "targetHelperLongName",
                "filename": "a.py",
                "source_line": 3,
            }
        ]
        with patch("graph_traversal._fetch_all_edges_sync", return_value=edges):
            result = graph_traversal.run_impact_bfs(
                [], start_symbols=["target"], max_depth=2, mode="exploratory"
            )

        self.assertEqual(result["total_count"], 1)


class RippleImpactTreeTests(unittest.TestCase):
    """Phase 5A.7 - 'vet dau loang': dig past direct callers into callers-of-callers,
    and assemble the whole reverse-call chain into a nested tree for the LLM."""

    def test_multi_hop_ripple_builds_nested_tree(self):
        # Chain: D calls C calls B calls A. Changing A should surface B, then C, then D.
        edges = [
            {
                "source_puid": "repo::b.py::function::B",
                "target_puid": "repo::a.py::function::A",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.95,
                "source_symbol": "B",
                "target_symbol": "A",
                "filename": "b.py",
                "source_line": 10,
            },
            {
                "source_puid": "repo::c.py::function::C",
                "target_puid": "repo::b.py::function::B",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.9,
                "source_symbol": "C",
                "target_symbol": "B",
                "filename": "c.py",
                "source_line": 20,
            },
            {
                "source_puid": "repo::d.py::function::D",
                "target_puid": "repo::c.py::function::C",
                "edge_type": "calls",
                "resolution_status": "resolved",
                "confidence": 0.9,
                "source_symbol": "D",
                "target_symbol": "C",
                "filename": "d.py",
                "source_line": 30,
            },
        ]
        with patch("graph_traversal._fetch_all_edges_sync", return_value=edges):
            bfs = graph_traversal.run_impact_bfs(["repo::a.py::function::A"], mode="strict")

        puids = {n["puid"] for n in bfs["affected_nodes"]}
        self.assertEqual(
            puids,
            {"repo::b.py::function::B", "repo::c.py::function::C", "repo::d.py::function::D"},
        )

        tree = graph_traversal.build_impact_tree(
            ["repo::a.py::function::A"], bfs["affected_nodes"], bfs["edges"],
            seed_info={"repo::a.py::function::A": {"node_name": "A", "filename": "a.py"}},
        )

        self.assertEqual(len(tree), 1)
        root = tree[0]
        self.assertEqual(root["symbol"], "A")
        self.assertEqual(len(root["children"]), 1)
        b_node = root["children"][0]
        self.assertEqual(b_node["symbol"], "B")
        self.assertEqual(b_node["line"], 10)
        c_node = b_node["children"][0]
        self.assertEqual(c_node["symbol"], "C")
        d_node = c_node["children"][0]
        self.assertEqual(d_node["symbol"], "D")
        self.assertEqual(d_node["children"], [])

        text = graph_traversal.render_impact_tree_text(tree)
        self.assertIn("[CHANGED] A (a.py)", text)
        self.assertIn("[calls] B (b.py:L10)", text)
        self.assertIn("[calls] C (c.py:L20)", text)
        self.assertIn("[calls] D (d.py:L30)", text)
        # D should be nested deeper than B (ripple goes outward, not flat).
        self.assertGreater(text.index("D (d.py"), text.index("C (c.py"))
        self.assertGreater(text.index("C (c.py"), text.index("B (b.py"))

    def test_build_impact_analysis_result_exposes_nested_impact_tree(self):
        seed = [{"puid": "repo::a.py::function::A", "node_type": "function", "node_name": "A", "filename": "a.py"}]
        bfs_result = {
            "affected_nodes": [
                {"puid": "repo::b.py::function::B", "node_name": "B", "filename": "b.py", "depth": 1, "via_edge_type": "calls", "path": ["repo::a.py::function::A", "repo::b.py::function::B"]},
                {"puid": "repo::c.py::function::C", "node_name": "C", "filename": "c.py", "depth": 2, "via_edge_type": "calls", "path": ["repo::a.py::function::A", "repo::b.py::function::B", "repo::c.py::function::C"]},
            ],
            "edges": [
                {"source_puid": "repo::b.py::function::B", "source_symbol": "B", "source_file": "b.py", "source_line": 10,
                 "target_puid": "repo::a.py::function::A", "target_symbol": "A", "target_file": "a.py", "target_line": 1,
                 "edge_type": "calls", "resolution_status": "resolved", "confidence": 0.95, "metadata": "", "depth": 1, "path": []},
                {"source_puid": "repo::c.py::function::C", "source_symbol": "C", "source_file": "c.py", "source_line": 20,
                 "target_puid": "repo::b.py::function::B", "target_symbol": "B", "target_file": "b.py", "target_line": 10,
                 "edge_type": "calls", "resolution_status": "resolved", "confidence": 0.9, "metadata": "", "depth": 2, "path": []},
            ],
            "max_depth_reached": False,
            "total_count": 2,
        }
        with patch("rag.resolve_impact_seed_candidates", return_value=seed), \
             patch("rag.run_impact_bfs", return_value=bfs_result):
            result = rag.build_impact_analysis_result("function", "A")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["impact_tree"]), 1)
        root = result["impact_tree"][0]
        self.assertEqual(root["symbol"], "A")
        self.assertEqual(root["children"][0]["symbol"], "B")
        self.assertEqual(root["children"][0]["children"][0]["symbol"], "C")


if __name__ == "__main__":
    unittest.main()
