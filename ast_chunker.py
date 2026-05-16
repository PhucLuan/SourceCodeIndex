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
    is_skeleton: bool = False
    parent_name: Optional[str] = None
    references: List[str] = field(default_factory=list)

def get_node_name(node, source_bytes: bytes) -> str:
    """Lấy tên của node (class, function, arrow function, tag...)."""
    # 1. Thử các field chuẩn
    for field_name in ['name', 'identifier']:
        name_node = node.child_by_field_name(field_name)
        if name_node:
            return source_bytes[name_node.start_byte:name_node.end_byte].decode('utf-8')
    
    # 2. Xử lý Arrow Function (const name = () => ...)
    # Thường arrow_function là con của variable_declarator
    if node.type == 'arrow_function':
        parent = node.parent
        if parent and parent.type == 'variable_declarator':
            id_node = parent.child_by_field_name('name')
            if id_node:
                return source_bytes[id_node.start_byte:id_node.end_byte].decode('utf-8')

    # 3. Xử lý HTML Element
    if node.type == 'element':
        start_tag = node.child_by_field_name('start_tag')
        if start_tag:
            tag_name_node = start_tag.child_by_field_name('name')
            if tag_name_node:
                return f"<{source_bytes[tag_name_node.start_byte:tag_name_node.end_byte].decode('utf-8')}>"
        return "<html>"

    # 4. Xử lý CSS Rule Set
    if node.type == 'rule_set':
        selectors = node.child_by_field_name('selectors')
        if selectors:
            return source_bytes[selectors.start_byte:selectors.end_byte].decode('utf-8').strip()
        return "css_rule"

    # 5. Tìm con là identifier
    for child in node.children:
        if child.type == 'identifier':
            return source_bytes[child.start_byte:child.end_byte].decode('utf-8')
            
    return "unknown"

def get_signature(node, source_bytes: bytes) -> str:
    """Lấy dòng khai báo (chữ ký) của node."""
    # Thường là dòng đầu tiên của node hoặc đến khi gặp '{' hoặc ':'
    text = source_bytes[node.start_byte:node.end_byte].decode('utf-8')
    first_line = text.split('\n')[0].strip()
    return first_line

def extract_ast_nodes(text: str, lang: str) -> List[AstChunk]:
    """
    Parse AST và trích xuất các Node cùng với Skeleton Index.
    """
    parser = parsers.get(lang)
    chunks = []

    if not parser:
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
        
        def traverse(node, current_parent=None):
            if node.type in target_types:
                node_name = get_node_name(node, source_bytes)
                
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
                node_type = node.type.replace("_definition", "").replace("_declaration", "")
                
                # Tạo chunk nội dung đầy đủ
                chunk = AstChunk(
                    text=node_text,
                    node_type=node_type,
                    node_name=node_name,
                    start_line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    parent_name=current_parent
                )
                chunks.append(chunk)
                
                # Lưu chữ ký để làm skeleton
                sig = get_signature(node, source_bytes)
                file_skeletons.append((node_type, node_name, sig, current_parent))
                
                # Đệ quy vào con với parent mới
                for child in node.children:
                    traverse(child, current_parent=node_name)
            else:
                for child in node.children:
                    traverse(child, current_parent=current_parent)

        traverse(tree.root_node)

        # Stage 2: Tạo Skeleton Nodes ("Mục lục") cho các Class/Interface
        # Nhóm các signatures theo parent
        skeletons_by_parent = {}
        for n_type, n_name, sig, parent in file_skeletons:
            if parent not in skeletons_by_parent:
                skeletons_by_parent[parent] = []
            skeletons_by_parent[parent].append(f"  - {n_type}: {sig}")

        for parent, items in skeletons_by_parent.items():
            if parent is None:
                s_name = "Global Table of Contents"
                s_type = "file_skeleton"
            else:
                s_name = f"{parent} Skeleton"
                s_type = "class_skeleton"
            
            skeleton_text = f"Skeleton for {s_name}:\n" + "\n".join(items)
            
            chunks.append(AstChunk(
                text=skeleton_text,
                node_type=s_type,
                node_name=s_name,
                start_line=1,
                end_line=1,
                is_skeleton=True
            ))

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
    sys.stderr.write(f"[Tree-sitter] Đã parse {len(chunks)} nodes (bao gồm Skeleton) từ file (lang={lang})\n")
    for c in chunks:
        tag = " [SKELETON]" if c.is_skeleton else ""
        parent = f" (parent: {c.parent_name})" if c.parent_name else ""
        sys.stderr.write(f"  - {c.node_type}: {c.node_name}{parent}{tag}\n")
    sys.stderr.write(f"=============================================\n")
    sys.stderr.flush()
    # === [DEBUG_LOG_END] ===

    return chunks
