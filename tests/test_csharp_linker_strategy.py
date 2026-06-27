import sys
import types
import unittest
from unittest.mock import patch


if "asyncpg" not in sys.modules:
    asyncpg = types.ModuleType("asyncpg")

    async def _unavailable(*args, **kwargs):
        raise RuntimeError("asyncpg is not available in this test stub")

    asyncpg.create_pool = _unavailable
    sys.modules["asyncpg"] = asyncpg

import graph_symbol_linker as linker


class _FakeConn:
    def __init__(self, node_rows, edge_rows):
        self.node_rows = node_rows
        self.edge_rows = edge_rows
        self.executemany_calls = []

    async def fetchval(self, query, *args):
        return True

    async def fetch(self, query, *args):
        if "graph_edges" in query:
            return list(self.edge_rows)
        return list(self.node_rows)

    async def executemany(self, query, params_list):
        self.executemany_calls.append((query, list(params_list)))


class _FakeConnCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeConnCtx(self._conn)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class CsharpStrategyA0Tests(unittest.TestCase):
    def test_di_field_call_resolves_to_implementing_class_not_ambiguous(self):
        node_rows = [
            {"puid": "repo::AssignmentService.cs::file::AssignmentService.cs", "filename": "AssignmentService.cs",
             "repo_name": "repo", "node_type": "file", "node_name": "AssignmentService.cs",
             "qualified_name": "AssignmentService.cs", "parent_puid": "", "is_skeleton": False},
            {"puid": "repo::AssignmentService.cs::class::AssignmentService", "filename": "AssignmentService.cs",
             "repo_name": "repo", "node_type": "class", "node_name": "AssignmentService",
             "qualified_name": "AssignmentService", "parent_puid": "repo::AssignmentService.cs::file::AssignmentService.cs",
             "is_skeleton": False},
            {"puid": "repo::AssignmentService.cs::method::AssignmentService.CreateAsync", "filename": "AssignmentService.cs",
             "repo_name": "repo", "node_type": "method", "node_name": "CreateAsync",
             "qualified_name": "AssignmentService.CreateAsync", "parent_puid": "repo::AssignmentService.cs::class::AssignmentService",
             "is_skeleton": False},
            {"puid": "repo::AssignmentRepository.cs::class::AssignmentRepository", "filename": "AssignmentRepository.cs",
             "repo_name": "repo", "node_type": "class", "node_name": "AssignmentRepository",
             "qualified_name": "AssignmentRepository", "parent_puid": "repo::AssignmentRepository.cs::file::AssignmentRepository.cs",
             "is_skeleton": False},
            {"puid": "repo::AssignmentRepository.cs::method::AssignmentRepository.AddAsync", "filename": "AssignmentRepository.cs",
             "repo_name": "repo", "node_type": "method", "node_name": "AddAsync",
             "qualified_name": "AssignmentRepository.AddAsync", "parent_puid": "repo::AssignmentRepository.cs::class::AssignmentRepository",
             "is_skeleton": False},
            {"puid": "repo::UserRepository.cs::class::UserRepository", "filename": "UserRepository.cs",
             "repo_name": "repo", "node_type": "class", "node_name": "UserRepository",
             "qualified_name": "UserRepository", "parent_puid": "repo::UserRepository.cs::file::UserRepository.cs",
             "is_skeleton": False},
            {"puid": "repo::UserRepository.cs::method::UserRepository.AddAsync", "filename": "UserRepository.cs",
             "repo_name": "repo", "node_type": "method", "node_name": "AddAsync",
             "qualified_name": "UserRepository.AddAsync", "parent_puid": "repo::UserRepository.cs::class::UserRepository",
             "is_skeleton": False},
        ]

        edge_rows = [
            {"id": "e1", "source_puid": "repo::AssignmentService.cs::method::AssignmentService.CreateAsync",
             "target_puid": "", "edge_type": "calls", "resolution_status": "unresolved", "confidence": 0.55,
             "source_symbol": "AssignmentService.CreateAsync", "target_symbol": "_assignmentRepository.AddAsync",
             "source_line": 18, "target_line": 0,
             "metadata": "callee=_assignmentRepository.AddAsync;receiver_type=IAssignmentRepository",
             "repo_name": "repo", "filename": "AssignmentService.cs", "lang": "csharp"},
            {"id": "e2", "source_puid": "repo::AssignmentRepository.cs::class::AssignmentRepository",
             "target_puid": "", "edge_type": "implements", "resolution_status": "unresolved", "confidence": 0.4,
             "source_symbol": "AssignmentRepository", "target_symbol": "IAssignmentRepository",
             "source_line": 4, "target_line": 0, "metadata": "csharp_base_list",
             "repo_name": "repo", "filename": "AssignmentRepository.cs", "lang": "csharp"},
            {"id": "e3", "source_puid": "repo::UserRepository.cs::class::UserRepository",
             "target_puid": "", "edge_type": "implements", "resolution_status": "unresolved", "confidence": 0.4,
             "source_symbol": "UserRepository", "target_symbol": "IUserRepository",
             "source_line": 4, "target_line": 0, "metadata": "csharp_base_list",
             "repo_name": "repo", "filename": "UserRepository.cs", "lang": "csharp"},
        ]

        conn = _FakeConn(node_rows, edge_rows)
        pool = _FakePool(conn)

        # graph_symbol_linker does `await asyncpg.create_pool(...)`. Since the
        # stub asyncpg module's create_pool is an `async def`, mock.patch
        # auto-detects it and creates an AsyncMock, which already awaits for
        # us and yields `return_value` directly - so return_value must be the
        # pool itself, not something that needs a second await.
        with patch("graph_symbol_linker.DATABASE_URL", "postgres://stub"), \
             patch("graph_symbol_linker.load_active_profile", return_value=types.SimpleNamespace(table_name="nodes_tbl")), \
             patch("graph_symbol_linker.asyncpg.create_pool", return_value=pool):
            import asyncio
            result = asyncio.get_event_loop().run_until_complete(linker.resolve_unresolved_edges())

        self.assertGreaterEqual(result["updated"], 1)

        calls_update = None
        for _query, params_list in conn.executemany_calls:
            for params in params_list:
                if params[3] == "e1":
                    calls_update = params

        self.assertIsNotNone(calls_update, "expected an UPDATE for the DI call edge e1")
        resolved_puid, status, confidence, _edge_id = calls_update
        self.assertEqual(status, "resolved")
        self.assertEqual(resolved_puid, "repo::AssignmentRepository.cs::method::AssignmentRepository.AddAsync")
        self.assertGreaterEqual(confidence, 0.8)


if __name__ == "__main__":
    unittest.main()
