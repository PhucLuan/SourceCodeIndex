import logging
import logging
import sys
import tree_sitter
import tree_sitter_python
import tree_sitter_c_sharp
import tree_sitter_javascript
import tree_sitter_typescript
import tree_sitter_html
import tree_sitter_css
from dataclasses import dataclass, field
from typing import List, Optional, Set

from graph_node_contract import (
    resolve_graph_node_kind,
)
from ast_extractors import (
    build_modifiers,
    extract_docstring,
    extract_node_name,
    extract_signature,
)

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

try:
    JS_LANGUAGE = tree_sitter.Language(tree_sitter_javascript.language())
    parsers["javascript"] = tree_sitter.Parser(JS_LANGUAGE)
    parsers["js"] = parsers["javascript"]
except Exception as e:
    logger.warning(f"Could not load javascript parser: {e}")

try:
    TS_LANGUAGE = tree_sitter.Language(tree_sitter_typescript.language_typescript())
    parsers["typescript"] = tree_sitter.Parser(TS_LANGUAGE)
    parsers["ts"] = parsers["typescript"]
except Exception as e:
    logger.warning(f"Could not load typescript parser: {e}")

try:
    TSX_LANGUAGE = tree_sitter.Language(tree_sitter_typescript.language_tsx())
    parsers["tsx"] = tree_sitter.Parser(TSX_LANGUAGE)
except Exception as e:
    logger.warning(f"Could not load tsx parser: {e}")

try:
    HTML_LANGUAGE = tree_sitter.Language(tree_sitter_html.language())
    parsers["html"] = tree_sitter.Parser(HTML_LANGUAGE)
except Exception as e:
    logger.warning(f"Could not load html parser: {e}")

try:
    CSS_LANGUAGE = tree_sitter.Language(tree_sitter_css.language())
    parsers["css"] = tree_sitter.Parser(CSS_LANGUAGE)
    parsers["scss"] = parsers["css"]
    parsers["less"] = parsers["css"]
except Exception as e:
    logger.warning(f"Could not load css parser: {e}")

@dataclass
class AstChunk:
    text: str
    node_type: str
    node_name: str
    start_line: int
    end_line: int
    qualified_name: str = ""
    signature: str = ""
    docstring: str = ""
    modifiers: str = ""
    export_status: str = "unknown"
    source_span: str = ""
    is_skeleton: bool = False
    parent_name: Optional[str] = None
    parent_node_type: Optional[str] = ""
    parent_qualified_name: Optional[str] = ""
    references: List[str] = field(default_factory=list)

def get_node_name(node, source_bytes: bytes) -> str:
    """Backward-compatible wrapper for shared node-name extraction."""
    return extract_node_name(node, source_bytes)

def get_signature(node, source_bytes: bytes) -> str:
    """Backward-compatible wrapper for shared signature extraction."""
    return extract_signature(node, source_bytes)


def _extract_python_docstring(node, source_bytes: bytes) -> str:
    """Backward-compatible wrapper for shared docstring extraction."""
    return extract_docstring(node, source_bytes, "python")


def _extract_modifiers(node_text: str, lang: str, node_type: str, export_status: str) -> str:
    """Backward-compatible wrapper for shared modifier extraction."""
    return build_modifiers(node_text, lang, node_type, export_status)


def _resolve_graph_node_kind(
    lang: str,
    raw_kind: str,
    current_parent_type: Optional[str],
    is_skeleton: bool,
) -> str:
    """Backward-compatible wrapper for the shared graph node resolver."""
    return resolve_graph_node_kind(
        lang,
        raw_kind,
        current_parent_type=current_parent_type,
        is_skeleton=is_skeleton,
    )

def extract_ast_nodes(text: str, lang: str) -> List[AstChunk]:
    """
    Parse AST và trích xuất các Node cùng với Skeleton Index.
    """
    parser = parsers.get(lang)
    chunks = []

    if not parser:
        chunks = []
    else:
        source_bytes = text.encode('utf-8')
        tree = parser.parse(source_bytes)
        
        target_types = {
            "python": {"class_definition", "function_definition"},
            "c_sharp": {"class_declaration", "method_declaration", "interface_declaration", "enum_declaration", "struct_declaration"},
            "csharp": {"class_declaration", "method_declaration", "interface_declaration", "enum_declaration", "struct_declaration"},
            "javascript": {"class_declaration", "function_declaration", "method_definition", "arrow_function"},
            "typescript": {"class_declaration", "function_declaration", "method_definition", "interface_declaration", "enum_declaration", "arrow_function", "type_alias_declaration"},
            "tsx": {"class_declaration", "function_declaration", "method_definition", "interface_declaration", "enum_declaration", "arrow_function", "type_alias_declaration"},
            "html": {"element"},
            "css": {"rule_set"},
            "scss": {"rule_set"},
            "less": {"rule_set"}
        }.get(lang, set())

        # Stage 1: Thu thập tất cả các node và phân cấp
        file_skeletons = []
        
        def traverse(node, current_parent=None, current_parent_type=None, current_qualified=None):
            if node.type in target_types:
                node_name = extract_node_name(node, source_bytes)
                qualified_name = f"{current_qualified}.{node_name}" if current_qualified else node_name
                
                # Bắt đầu và kết thúc mặc định
                start_byte = node.start_byte
                end_byte = node.end_byte
                
                # 1. Xử lý export_statement (TS/JS)
                if node.parent and node.parent.type == 'export_statement':
                    start_byte = node.parent.start_byte
                
                # 2. Xử lý Decorators (Angular/TS)
                # Decorators thường nằm ngay trước node hoặc là con của node tùy version parser
                # Chúng ta sẽ kiểm tra các anh em đứng trước nếu chúng là decorator
                prefix_text = ""
                prev = node.prev_sibling
                while prev and prev.type == 'decorator':
                    prefix_text = source_bytes[prev.start_byte:prev.end_byte].decode('utf-8') + "\n" + prefix_text
                    # Nếu decorator đứng trước, ta mở rộng start_byte
                    if prev.start_byte < start_byte:
                        start_byte = prev.start_byte
                    prev = prev.prev_sibling

                node_text = source_bytes[start_byte:end_byte].decode('utf-8')
                node_type = _resolve_graph_node_kind(
                    lang,
                    node.type,
                    current_parent_type,
                    is_skeleton=False,
                )
                docstring = extract_docstring(node, source_bytes, lang)
                export_status = "exported" if node.parent and node.parent.type == "export_statement" else "internal"
                modifiers = build_modifiers(node_text, lang, node_type, export_status)
                
                # Tạo chunk nội dung đầy đủ
                chunk = AstChunk(
                    text=node_text,
                    node_type=node_type,
                    node_name=node_name,
                    qualified_name=qualified_name,
                    signature=extract_signature(node, source_bytes),
                    docstring=docstring,
                    modifiers=modifiers,
                    export_status=export_status,
                    start_line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    source_span=f"L{node.start_point.row + 1}-L{node.end_point.row + 1}",
                    parent_name=current_parent,
                    parent_node_type=current_parent_type or "",
                    parent_qualified_name=current_qualified or ""
                )
                chunks.append(chunk)
                
                # Lưu chữ ký để làm skeleton
                sig = extract_signature(node, source_bytes)
                file_skeletons.append((node_type, node_name, sig, current_parent, qualified_name))
                
                # Đệ quy vào con với parent mới
                for child in node.children:
                    traverse(child, current_parent=node_name, current_parent_type=node_type, current_qualified=qualified_name)
            else:
                for child in node.children:
                    traverse(child, current_parent=current_parent, current_parent_type=current_parent_type, current_qualified=current_qualified)

        traverse(tree.root_node)

        # Stage 2: Tạo Skeleton Nodes ("Mục lục") cho các Class/Interface
        # Nhóm các signatures theo parent
        skeletons_by_parent = {}
        for n_type, n_name, sig, parent, qualified_name in file_skeletons:
            if parent not in skeletons_by_parent:
                skeletons_by_parent[parent] = []
            skeletons_by_parent[parent].append(f"  - {n_type}: {sig}")

        for parent, items in skeletons_by_parent.items():
            if parent is None:
                s_name = "Global Table of Contents"
                s_type = "concept"
                skeleton_qname = "global"
            else:
                s_name = f"{parent} Skeleton"
                s_type = "concept"
                skeleton_qname = parent
            
            skeleton_text = f"Skeleton for {s_name}:\n" + "\n".join(items)
            
            chunks.append(AstChunk(
                text=skeleton_text,
                node_type=s_type,
                node_name=s_name,
                qualified_name=skeleton_qname,
                signature=items[0] if items else "",
                modifiers="summary",
                export_status="internal",
                start_line=1,
                end_line=1,
                source_span="L1-L1",
                is_skeleton=True
            ))

        if not chunks:
            chunks.append(AstChunk(
                text=text,
                node_type="file",
                node_name="global",
                qualified_name="global",
                modifiers="",
                start_line=1,
                end_line=max(1, text.count('\n') + 1)
            ))

    # === [DEBUG_LOG_START] ===
    sys.stderr.write(f"\n=============================================\n")
    sys.stderr.write(f"[Tree-sitter] Đã parse {len(chunks)} nodes (bao gồm Skeleton) từ file (lang={lang})\n")
    for c in chunks:
        tag = " [SKELETON]" if c.is_skeleton else ""
        parent = f" (parent: {c.parent_name})" if c.parent_name else ""
        sys.stderr.write(f"  - {c.node_type}: {c.node_name}{parent}{tag}\n")
    sys.stderr.write(f"=============================================\n")
    sys.stderr.flush()
    # === [DEBUG_LOG_END] ===

    return chunks
