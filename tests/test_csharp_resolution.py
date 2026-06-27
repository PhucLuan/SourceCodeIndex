import unittest
from unittest.mock import patch

import graph_edge_extractor as gee


CSHARP_SOURCE = """
namespace MyApp.Services
{
    public class AssignmentService
    {
        private readonly IAssignmentRepository _assignmentRepository;
        private readonly IUserRepository _userRepository;

        public AssignmentService(IAssignmentRepository assignmentRepository, IUserRepository userRepository)
        {
            _assignmentRepository = assignmentRepository;
            _userRepository = userRepository;
        }

        public async Task CreateAsync()
        {
            await _assignmentRepository.AddAsync();
        }
    }
}
"""


class CsharpFieldTypeExtractionTests(unittest.TestCase):
    def test_extract_csharp_field_types_maps_di_fields(self):
        field_types = gee._extract_csharp_field_types(CSHARP_SOURCE)
        self.assertEqual(field_types.get("_assignmentRepository"), "IAssignmentRepository")
        self.assertEqual(field_types.get("_userRepository"), "IUserRepository")

    def test_csharp_receiver_type_resolves_field_call(self):
        field_types = {"_assignmentRepository": "IAssignmentRepository"}
        self.assertEqual(
            gee._csharp_receiver_type("_assignmentRepository.AddAsync", field_types),
            "IAssignmentRepository",
        )
        self.assertEqual(gee._csharp_receiver_type("DoSomething", field_types), "")



class CsharpCallExtractionIntegrationTests(unittest.TestCase):
    def test_invocation_edge_carries_receiver_type_metadata(self):
        try:
            from ast_chunker import parsers
        except Exception as ex:  # pragma: no cover - skip if native parser unavailable
            self.skipTest(f"tree-sitter c# parser unavailable: {ex}")
        if "csharp" not in parsers:
            self.skipTest("csharp tree-sitter grammar not loaded")

        class _Info:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        chunks = [
            _Info(node_type="file", node_name="AssignmentService.cs", qualified_name="AssignmentService.cs",
                  parent_node_type="", parent_qualified_name="", start_line=1, end_line=200, is_skeleton=False),
            _Info(node_type="class", node_name="AssignmentService", qualified_name="AssignmentService",
                  parent_node_type="file", parent_qualified_name="AssignmentService.cs",
                  start_line=4, end_line=20, is_skeleton=False),
            _Info(node_type="method", node_name="CreateAsync", qualified_name="AssignmentService.CreateAsync",
                  parent_node_type="class", parent_qualified_name="AssignmentService",
                  start_line=16, end_line=19, is_skeleton=False),
        ]

        def normalize_puid_fn(repo, path, kind, qname):
            return f"{repo}::{path}::{kind}::{qname}"

        edges = gee.extract_graph_edges(
            filepath="AssignmentService.cs",
            text=CSHARP_SOURCE,
            lang="csharp",
            chunks=chunks,
            repo_name="repo",
            normalize_puid_fn=normalize_puid_fn,
        )

        call_edges = [e for e in edges if e.edge_type == "calls" and "AddAsync" in e.target_symbol]
        self.assertTrue(call_edges, "expected a calls edge for _assignmentRepository.AddAsync()")
        self.assertIn("receiver_type=IAssignmentRepository", call_edges[0].metadata)


if __name__ == "__main__":
    unittest.main()
