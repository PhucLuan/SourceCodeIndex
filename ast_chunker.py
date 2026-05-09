import logging
import sys
import tree_sitter
import tree_sitter_python
import tree_sitter_c_sharp
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Khởi tạo Parsers
parsers = {}
try:
    PY_LANGUAGE = tree_sitter.Language(tree_sitter_python.language())
    parsers["python"] = tree_sitter.Parser(PY_LANGUAGE)
except Exception as e:
    logger.warning(f"Could not load python parser: {e}")

try:
    CS_LANGUAGE = tree_sitter.Language(tree_sitter_c_sharp.language())
    parsers["csharp"] = tree_sitter.Parser(CS_LANGUAGE)
    parsers["c_sharp"] = parsers["csharp"]  # Support both variants
except Exception as e:
    logger.warning(f"Could not load csharp parser: {e}")

@dataclass
class AstChunk:
    text: str
    node_type: str
    node_name: str
    start_line: int
    end_line: int

def get_node_name(node, source_bytes: bytes) -> str:
    """Lấy tên của node class hoặc function bằng cách tìm field 'name' hoặc 'identifier'."""
    # Thử lấy qua field name (Python) hoặc identifier (C#)
    for field in ['name', 'identifier']:
        name_node = node.child_by_field_name(field)
        if name_node:
            return source_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8')
    
    # Fallback: Tìm con trực tiếp có kiểu 'identifier'
    for child in node.children:
        if child.type == 'identifier':
            return source_bytes[child.start_byte:child.end_byte].decode('utf-8')
            
    return "unknown"

def extract_ast_nodes(text: str, lang: str) -> List[AstChunk]:
    """
    Parse AST từ text và trích xuất các Node như class_definition, function_definition.
    """
    parser = parsers.get(lang)
    chunks = []

    if not parser:
        # Fallback if language not supported
        chunks = [AstChunk(
            text=text, 
            node_type="file", 
            node_name="global", 
            start_line=1, 
            end_line=max(1, text.count('\n') + 1)
        )]
    else:
        source_bytes = text.encode('utf-8')
        tree = parser.parse(source_bytes)
        
        # Mapping node types by language
        target_types = {
            "python": {"class_definition", "function_definition"},
            "c_sharp": {"class_declaration", "method_declaration", "interface_declaration", "enum_declaration", "struct_declaration"},
            "csharp": {"class_declaration", "method_declaration", "interface_declaration", "enum_declaration", "struct_declaration"}
        }.get(lang, set())
        
        def traverse(node):
            if node.type in target_types:
                node_name = get_node_name(node, source_bytes)
                node_text = source_bytes[node.start_byte:node.end_byte].decode('utf-8')
                start_line = node.start_point.row + 1
                end_line = node.end_point.row + 1
                
                chunks.append(AstChunk(
                    text=node_text,
                    node_type=node.type.replace("_definition", "").replace("_declaration", ""),
                    node_name=node_name,
                    start_line=start_line,
                    end_line=end_line
                ))
            for child in node.children:
                traverse(child)

        traverse(tree.root_node)
        
        if not chunks:
            chunks.append(AstChunk(
                text=text,
                node_type="file",
                node_name="global",
                start_line=1,
                end_line=max(1, text.count('\n') + 1)
            ))

    # === [DEBUG_LOG_START] ===
    sys.stderr.write(f"\n=============================================\n")
    sys.stderr.write(f"[Tree-sitter] Đã parse {len(chunks)} nodes từ file (lang={lang})\n")
    for c in chunks:
        sys.stderr.write(f"  - {c.node_type}: {c.node_name} (L{c.start_line}-L{c.end_line})\n")
    sys.stderr.write(f"=============================================\n")
    sys.stderr.flush()
    # === [DEBUG_LOG_END] ===

    return chunks
