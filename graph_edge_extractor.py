from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Optional

from graph_edge_contract import (
    GraphEdge,
    canonicalize_edge_type,
    canonicalize_resolution_status,
    make_edge_id,
)
from graph_node_contract import canonicalize_node_kind


@dataclass(frozen=True)
class _ChunkInfo:
    puid: str
    node_type: str
    node_name: str
    qualified_name: str
    parent_puid: str
    start_line: int
    end_line: int
    is_skeleton: bool


def _decode(source_bytes: bytes, start_byte: int, end_byte: int) -> str:
    return source_bytes[start_byte:end_byte].decode("utf-8")


def _workspace_relative_path(filepath: str) -> str:
    normalized = (filepath or "").replace("\\", "/")
    prefix = "/tmp/workspace/"
    if normalized.startswith(prefix):
        relative = normalized[len(prefix):]
    else:
        parts = normalized.split("/")
        if "workspace" in parts:
            idx = parts.index("workspace")
            relative = "/".join(parts[idx + 1 :])
        else:
            relative = normalized
    parts = relative.split("/", 1)
    if len(parts) == 2 and "_" in parts[0]:
        return parts[1]
    return relative


def _normalize_simple_name(symbol: str) -> str:
    raw = (symbol or "").strip()
    if not raw:
        return ""
    raw = raw.replace("new ", "")
    raw = raw.split("(")[0].strip()
    raw = raw.split(" as ")[0].strip()
    raw = raw.split(".")[-1].strip()
    raw = raw.split("::")[-1].strip()
    return raw


def _build_chunk_infos(
    filepath: str,
    repo_name: str,
    chunks,
    normalize_puid_fn: Callable[[str, str, str, str], str],
) -> tuple[str, list[_ChunkInfo], dict[str, list[_ChunkInfo]], dict[str, _ChunkInfo]]:
    relative_path = _workspace_relative_path(filepath)
    infos: list[_ChunkInfo] = []
    by_name: dict[str, list[_ChunkInfo]] = {}
    by_qname: dict[str, _ChunkInfo] = {}

    for chunk in chunks:
        kind = canonicalize_node_kind(
            getattr(chunk, "node_type", ""),
            is_file_node=getattr(chunk, "node_type", "") == "file",
            is_skeleton=getattr(chunk, "is_skeleton", False),
        )
        qname = getattr(chunk, "qualified_name", "") or getattr(chunk, "node_name", "") or ""
        puid = normalize_puid_fn(repo_name, relative_path, kind, qname)
        parent_kind = getattr(chunk, "parent_node_type", "") or "node"
        parent_qname = getattr(chunk, "parent_qualified_name", "") or ""
        parent_puid = ""
        if parent_qname:
            parent_puid = normalize_puid_fn(repo_name, relative_path, parent_kind, parent_qname)
        elif kind != "file":
            parent_puid = normalize_puid_fn(repo_name, relative_path, "file", relative_path)
        info = _ChunkInfo(
            puid=puid,
            node_type=kind,
            node_name=getattr(chunk, "node_name", "") or "",
            qualified_name=qname,
            parent_puid=parent_puid,
            start_line=getattr(chunk, "start_line", 0) or 0,
            end_line=getattr(chunk, "end_line", 0) or 0,
            is_skeleton=getattr(chunk, "is_skeleton", False),
        )
        infos.append(info)
        if info.node_name:
            by_name.setdefault(info.node_name, []).append(info)
        if info.qualified_name:
            by_qname[info.qualified_name] = info

    return relative_path, infos, by_name, by_qname


def _resolve_local_symbol(
    symbol: str,
    by_name: dict[str, list[_ChunkInfo]],
    by_qname: dict[str, _ChunkInfo],
) -> tuple[str, str]:
    raw = (symbol or "").strip()
    if not raw:
        return "", "unresolved"

    if raw in by_qname:
        return by_qname[raw].puid, "resolved"

    simple = _normalize_simple_name(raw)
    if not simple:
        return "", "unresolved"

    matches = by_name.get(simple, [])
    if len(matches) == 1:
        return matches[0].puid, "resolved"
    if len(matches) > 1:
        return "", "ambiguous"
    return "", "unresolved"


def _edge(
    *,
    source_puid: str,
    target_puid: str = "",
    edge_type: str,
    resolution_status: str,
    source_symbol: str = "",
    target_symbol: str = "",
    source_line: int = 0,
    target_line: int = 0,
    metadata: str = "",
    confidence: float = 1.0,
    repo_name: str = "",
    filename: str = "",
    lang: str = "",
) -> GraphEdge:
    normalized_edge_type = canonicalize_edge_type(edge_type)
    normalized_status = canonicalize_resolution_status(resolution_status)
    return GraphEdge(
        id=make_edge_id(
            source_puid,
            target_puid,
            normalized_edge_type,
            target_symbol=target_symbol,
            source_line=source_line,
            target_line=target_line,
        ),
        source_puid=source_puid,
        target_puid=target_puid,
        edge_type=normalized_edge_type,
        resolution_status=normalized_status,
        confidence=confidence,
        source_symbol=source_symbol,
        target_symbol=target_symbol,
        source_line=source_line,
        target_line=target_line,
        metadata=metadata,
        repo_name=repo_name,
        filename=filename,
        lang=lang,
    )


def _find_owner_chunk(infos: list[_ChunkInfo], line: int) -> Optional[_ChunkInfo]:
    owner = None
    for info in infos:
        if info.is_skeleton:
            continue
        if info.start_line <= line <= info.end_line:
            if owner is None:
                owner = info
            elif (info.end_line - info.start_line) <= (owner.end_line - owner.start_line):
                owner = info
    if owner:
        return owner
    for info in infos:
        if info.node_type == "file":
            return info
    return infos[0] if infos else None


_CSHARP_FIELD_DECL = re.compile(
    r"(?:private|protected|internal|public)\s+(?:readonly\s+)?"
    r"([A-Za-z_]\w*)(?:<[^>]*>)?(?:\[\])?\s+"
    r"(_?[A-Za-z]\w*)\s*;"
)


def _extract_csharp_field_types(text: str) -> dict[str, str]:
    """Best-effort map of `field_name -> declared_type_name` for C# classes.

    Used to resolve constructor-injected dependency calls like
    `_assignmentRepository.AddAsync()` to the concrete/interface type so the
    linker can disambiguate same-named methods across unrelated classes
    (e.g. multiple repositories implementing AddAsync).
    """
    field_types: dict[str, str] = {}
    for match in _CSHARP_FIELD_DECL.finditer(text):
        type_name, field_name = match.group(1), match.group(2)
        if type_name in {"return", "new", "var", "void"}:
            continue
        field_types[field_name] = type_name
    return field_types


def _csharp_receiver_type(callee: str, field_types: dict[str, str]) -> str:
    if "." not in callee:
        return ""
    receiver = callee.rsplit(".", 1)[0].strip()
    receiver = receiver.replace("this.", "").strip()
    return field_types.get(receiver, "")


def _call_node_types(lang: str) -> set[str]:
    lang = (lang or "").lower()
    if lang == "python":
        return {"call"}
    if lang in {"javascript", "js", "typescript", "ts", "tsx"}:
        return {"call_expression", "new_expression", "subscript_expression"}
    if lang in {"csharp", "c_sharp"}:
        return {"invocation_expression", "object_creation_expression"}
    return {"call_expression", "call", "invocation_expression", "new_expression"}


def _call_callee_text(node, source_bytes: bytes) -> str:
    for field_name in ("function", "expression", "callee", "constructor", "type", "name", "object"):
        child = node.child_by_field_name(field_name)
        if child:
            return _decode(source_bytes, child.start_byte, child.end_byte).strip()
    if node.children:
        first = node.children[0]
        return _decode(source_bytes, first.start_byte, first.end_byte).strip()
    return ""


def _collect_calls(
    text: str,
    lang: str,
    infos: list[_ChunkInfo],
    by_name: dict[str, list[_ChunkInfo]],
    by_qname: dict[str, _ChunkInfo],
    filename: str,
    repo_name: str,
) -> list[GraphEdge]:
    from ast_chunker import parsers

    parser = parsers.get(lang)
    if not parser:
        return []

    source_bytes = text.encode("utf-8")
    tree = parser.parse(source_bytes)
    call_types = _call_node_types(lang)
    edges: list[GraphEdge] = []

    lang_lower = (lang or "").lower()
    is_csharp = lang_lower in {"csharp", "c_sharp"}
    field_types = _extract_csharp_field_types(text) if is_csharp else {}

    def visit(node):
        if node.type in call_types:
            callee = _call_callee_text(node, source_bytes)
            owner = _find_owner_chunk(infos, node.start_point.row + 1)
            if owner:
                target_puid, status = _resolve_local_symbol(callee, by_name, by_qname)
                metadata = f"callee={callee}"
                if is_csharp:
                    receiver_type = _csharp_receiver_type(callee, field_types)
                    if receiver_type:
                        metadata += f";receiver_type={receiver_type}"
                edges.append(
                    _edge(
                        source_puid=owner.puid,
                        target_puid=target_puid,
                        edge_type="calls",
                        resolution_status=status,
                        source_symbol=owner.qualified_name or owner.node_name,
                        target_symbol=callee,
                        source_line=node.start_point.row + 1,
                        target_line=0,
                        metadata=metadata,
                        confidence=0.85 if status == "resolved" else 0.55,
                        repo_name=repo_name,
                        filename=filename,
                        lang=lang,
                    )
                )
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return edges


def _member_access_node_types(lang: str) -> set[str]:
    lang = (lang or "").lower()
    if lang in {"csharp", "c_sharp"}:
        return {"member_access_expression"}
    if lang in {"javascript", "js", "typescript", "ts", "tsx"}:
        return {"member_expression"}
    return set()


def _is_call_callee(node) -> bool:
    """True if `node` is the callee/function part of an invocation/object-creation
    expression - already captured as a 'calls' edge by `_collect_calls`, so it
    must be skipped here to avoid a redundant/incorrect reads edge (e.g. the
    `dto.GetCode` in `dto.GetCode()` is a call, not a property read)."""
    parent = node.parent
    if not parent:
        return False
    if parent.type not in {"invocation_expression", "object_creation_expression", "call_expression", "new_expression"}:
        return False
    # NOTE: tree-sitter's Python binding does not guarantee `is` identity
    # across separate child_by_field_name()/parent calls (each may return a
    # fresh wrapper object for the same underlying node) - compare with `==`.
    for field_name in ("function", "expression", "callee", "constructor", "type"):
        child = parent.child_by_field_name(field_name)
        if child is not None and child == node:
            return True
    return False


def _is_write_target(node) -> bool:
    """True if `node` is the left-hand side of an assignment (plain or compound)."""
    parent = node.parent
    if not parent or parent.type != "assignment_expression":
        return False
    left = parent.child_by_field_name("left")
    return left is not None and left == node


def _member_access_parts(node, lang: str, source_bytes: bytes) -> tuple[str, str]:
    """Returns (receiver_text, member_name_text) for a member-access node."""
    lang_lower = (lang or "").lower()
    if lang_lower in {"csharp", "c_sharp"}:
        obj = node.child_by_field_name("expression")
        name = node.child_by_field_name("name")
    else:
        obj = node.child_by_field_name("object")
        name = node.child_by_field_name("property")
    if obj is None or name is None:
        # Fallback if the grammar's field names differ from expected: the
        # member name is typically the last named child, the receiver the rest.
        named_children = [c for c in node.children if c.is_named]
        if len(named_children) >= 2:
            obj = obj or named_children[0]
            name = name or named_children[-1]
    obj_text = _decode(source_bytes, obj.start_byte, obj.end_byte).strip() if obj else ""
    name_text = _decode(source_bytes, name.start_byte, name.end_byte).strip() if name else ""
    return obj_text, name_text


def _collect_member_access(
    text: str,
    lang: str,
    infos: list[_ChunkInfo],
    by_name: dict[str, list[_ChunkInfo]],
    by_qname: dict[str, _ChunkInfo],
    filename: str,
    repo_name: str,
) -> list[GraphEdge]:
    """Find All References, "Code"-style: property/field reads & writes.

    `_collect_calls` only captures method invocations - a plain `dto.Code` or
    `dto.Code = x` member access (no call parens) is invisible to it. Without
    this, /impact on a DTO-style class with no methods (just properties) finds
    nothing, even though it's read/written everywhere.
    """
    from ast_chunker import parsers

    parser = parsers.get(lang)
    if not parser:
        return []

    member_types = _member_access_node_types(lang)
    if not member_types:
        return []

    source_bytes = text.encode("utf-8")
    tree = parser.parse(source_bytes)
    edges: list[GraphEdge] = []

    lang_lower = (lang or "").lower()
    is_csharp = lang_lower in {"csharp", "c_sharp"}
    field_types = _extract_csharp_field_types(text) if is_csharp else {}

    def visit(node):
        if node.type in member_types and not _is_call_callee(node):
            receiver_text, member_name = _member_access_parts(node, lang, source_bytes)
            if member_name:
                owner = _find_owner_chunk(infos, node.start_point.row + 1)
                if owner:
                    target_puid, status = _resolve_local_symbol(member_name, by_name, by_qname)
                    edge_type = "writes" if _is_write_target(node) else "reads"
                    metadata = f"receiver={receiver_text};member={member_name}"
                    if is_csharp:
                        receiver_type = _csharp_receiver_type(f"{receiver_text}.{member_name}", field_types)
                        if receiver_type:
                            metadata += f";receiver_type={receiver_type}"
                    edges.append(
                        _edge(
                            source_puid=owner.puid,
                            target_puid=target_puid,
                            edge_type=edge_type,
                            resolution_status=status,
                            source_symbol=owner.qualified_name or owner.node_name,
                            target_symbol=member_name,
                            source_line=node.start_point.row + 1,
                            target_line=0,
                            metadata=metadata,
                            confidence=0.8 if status == "resolved" else 0.5,
                            repo_name=repo_name,
                            filename=filename,
                            lang=lang,
                        )
                    )
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return edges


def _collect_contains(
    infos: list[_ChunkInfo],
    filename: str,
    repo_name: str,
    lang: str,
) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    for info in infos:
        if info.node_type == "file" or info.is_skeleton:
            continue
        source_puid = info.parent_puid
        if not source_puid:
            continue
        edges.append(
            _edge(
                source_puid=source_puid,
                target_puid=info.puid,
                edge_type="contains",
                resolution_status="resolved",
                source_symbol="",
                target_symbol=info.qualified_name or info.node_name,
                source_line=info.start_line,
                target_line=info.start_line,
                metadata=f"parent_kind={info.node_type}",
                confidence=1.0,
                repo_name=repo_name,
                filename=filename,
                lang=lang,
            )
        )
    return edges


_PY_FROM_IMPORT = re.compile(r"^\s*from\s+([.\w]+)\s+import\s+(.+?)\s*$")
_PY_IMPORT = re.compile(r"^\s*import\s+(.+?)\s*$")
_JS_IMPORT = re.compile(r"^\s*import\s+(.+?)\s+from\s+[\"']([^\"']+)[\"']\s*;?\s*$")
_REQUIRE = re.compile(r"require\(\s*[\"']([^\"']+)[\"']\s*\)")
_EXPORT_NAMED = re.compile(r"^\s*export\s+\{([^}]+)\}\s*;?\s*$")
_EXPORT_DECL = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|interface|enum|type)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
_MODULE_EXPORTS = re.compile(r"module\.exports\s*=\s*([A-Za-z_]\w*)")
_EXPORTS_DOT = re.compile(r"exports\.([A-Za-z_]\w*)\s*=")
_C_SHARP_USING = re.compile(r"^\s*using\s+([A-Za-z_][\w\.]*)(?:\s*=\s*[^;]+)?;\s*$", re.MULTILINE)
_PY_ALL = re.compile(r"__all__\s*=\s*\[(.*?)\]", re.DOTALL)
_PY_STRING = re.compile(r"[\"']([^\"']+)[\"']")


def _collect_imports_and_exports(
    text: str,
    lang: str,
    file_info: _ChunkInfo,
    infos: list[_ChunkInfo],
    by_name: dict[str, list[_ChunkInfo]],
    by_qname: dict[str, _ChunkInfo],
    filename: str,
    repo_name: str,
) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    lang = (lang or "").lower()

    def add_import(target_symbol: str, status: str, metadata: str, line: int = 1) -> None:
        edges.append(
            _edge(
                source_puid=file_info.puid,
                target_puid="",
                edge_type="imports",
                resolution_status=status,
                source_symbol=file_info.qualified_name or file_info.node_name,
                target_symbol=target_symbol,
                source_line=line,
                metadata=metadata,
                confidence=0.7 if status == "external" else 0.5,
                repo_name=repo_name,
                filename=filename,
                lang=lang,
            )
        )

    def add_export(
        source_puid: str,
        exported_name: str,
        metadata: str,
        line: int = 1,
        *,
        resolution_status: str = "resolved",
        target_puid: str = "",
    ) -> None:
        edges.append(
            _edge(
                source_puid=source_puid,
                target_puid=target_puid,
                edge_type="exports",
                resolution_status=resolution_status,
                source_symbol=exported_name,
                target_symbol=file_info.qualified_name or file_info.node_name,
                source_line=line,
                target_line=file_info.start_line,
                metadata=metadata,
                confidence=1.0,
                repo_name=repo_name,
                filename=filename,
                lang=lang,
            )
        )

    if lang == "python":
        for match in _PY_FROM_IMPORT.finditer(text):
            module = match.group(1).strip()
            imports = [part.strip() for part in match.group(2).split(",")]
            for imp in imports:
                alias = imp.split(" as ")[0].strip()
                target_symbol = f"{module}.{alias}" if alias else module
                status = "external" if not module.startswith(".") else "unresolved"
                add_import(target_symbol, status, f"from_import={module}:{alias}")

        for match in _PY_IMPORT.finditer(text):
            modules = [part.strip() for part in match.group(1).split(",")]
            for module in modules:
                base = module.split(" as ")[0].strip()
                if base:
                    status = "external" if not base.startswith(".") else "unresolved"
                    add_import(base, status, "import")

        all_match = _PY_ALL.search(text)
        if all_match:
            for name in _PY_STRING.findall(all_match.group(1)):
                target_puid, status = _resolve_local_symbol(name, by_name, by_qname)
                if target_puid:
                    add_export(target_puid, name, "__all__", target_puid=file_info.puid)
                else:
                    add_export(file_info.puid, name, "__all__", resolution_status=status)

    elif lang in {"javascript", "js", "typescript", "ts", "tsx"}:
        for match in _JS_IMPORT.finditer(text):
            spec = match.group(1).strip()
            module = match.group(2).strip()
            status = "external" if not module.startswith(".") else "unresolved"
            add_import(module, status, f"import={spec}")

        for require_target in _REQUIRE.finditer(text):
            module = require_target.group(1).strip()
            status = "external" if not module.startswith(".") else "unresolved"
            add_import(module, status, "require")

        for match in _EXPORT_NAMED.finditer(text):
            for part in match.group(1).split(","):
                token = part.strip()
                if not token:
                    continue
                local_name = token.split(" as ")[0].strip()
                target_puid, status = _resolve_local_symbol(local_name, by_name, by_qname)
                if target_puid:
                    add_export(target_puid, local_name, "export_named", target_puid=file_info.puid)
                else:
                    add_export(file_info.puid, local_name, "export_named", resolution_status=status)

        for match in _EXPORT_DECL.finditer(text):
            local_name = match.group(1).strip()
            target_puid, status = _resolve_local_symbol(local_name, by_name, by_qname)
            if target_puid:
                add_export(target_puid, local_name, "export_decl", target_puid=file_info.puid)
            else:
                add_export(file_info.puid, local_name, "export_decl", resolution_status=status)

        for match in _MODULE_EXPORTS.finditer(text):
            local_name = match.group(1).strip()
            target_puid, status = _resolve_local_symbol(local_name, by_name, by_qname)
            if target_puid:
                add_export(target_puid, local_name, "module_exports", target_puid=file_info.puid)
            else:
                add_export(file_info.puid, local_name, "module_exports_unresolved", resolution_status=status)

        for match in _EXPORTS_DOT.finditer(text):
            local_name = match.group(1).strip()
            target_puid, status = _resolve_local_symbol(local_name, by_name, by_qname)
            if target_puid:
                add_export(target_puid, local_name, "exports_dot", target_puid=file_info.puid)
            else:
                add_export(file_info.puid, local_name, "exports_dot", resolution_status=status)

    elif lang in {"csharp", "c_sharp"}:
        for match in _C_SHARP_USING.finditer(text):
            # Let the symbol linker classify external vs. local namespaces later
            # (it already checks EXTERNAL_PACKAGES and matches local files by name).
            add_import(match.group(1).strip(), "unresolved", "using")

        # Public top-level classes/methods are treated as exported surface.
        for info in infos:
            if info.node_type in {"class", "function", "method"}:
                chunk_text = text
                if re.search(rf"\bpublic\b.*\b{re.escape(info.node_name)}\b", chunk_text, re.IGNORECASE):
                    add_export(info.puid, info.node_name, "public_surface", info.start_line, target_puid=file_info.puid)

    else:
        # Best-effort generic imports.
        for match in re.finditer(r"^\s*(import|using)\s+(.+?)\s*$", text, re.MULTILINE):
            add_import(match.group(2).strip(), "external", match.group(1))

    return edges


def extract_graph_edges(
    filepath: str,
    text: str,
    lang: str,
    chunks,
    repo_name: str,
    normalize_puid_fn: Callable[[str, str, str, str], str],
) -> list[GraphEdge]:
    _relative_path, infos, by_name, by_qname = _build_chunk_infos(
        filepath,
        repo_name,
        chunks,
        normalize_puid_fn,
    )
    if not infos:
        return []

    file_info = next((info for info in infos if info.node_type == "file"), infos[0])
    edges: list[GraphEdge] = []
    edges.extend(_collect_contains(infos, filepath, repo_name, lang))
    edges.extend(
        _collect_imports_and_exports(
            text=text,
            lang=lang,
            file_info=file_info,
            infos=infos,
            by_name=by_name,
            by_qname=by_qname,
            filename=filepath,
            repo_name=repo_name,
        )
    )
    edges.extend(
        _collect_calls(
            text=text,
            lang=lang,
            infos=infos,
            by_name=by_name,
            by_qname=by_qname,
            filename=filepath,
            repo_name=repo_name,
        )
    )
    edges.extend(
        _collect_member_access(
            text=text,
            lang=lang,
            infos=infos,
            by_name=by_name,
            by_qname=by_qname,
            filename=filepath,
            repo_name=repo_name,
        )
    )

    # Inheritance / implementation edges.
    lang_lower = (lang or "").lower()
    for info in infos:
        if info.node_type != "class" or info.is_skeleton:
            continue
        if lang_lower == "python":
            m = re.search(
                rf"class\s+{re.escape(info.node_name)}\s*\((.*?)\)\s*:",
                text,
                re.MULTILINE | re.DOTALL,
            )
            if m:
                bases = [item.strip() for item in m.group(1).split(",") if item.strip()]
                for base in bases:
                    target_puid, status = _resolve_local_symbol(base, by_name, by_qname)
                    edges.append(
                        _edge(
                            source_puid=info.puid,
                            target_puid=target_puid,
                            edge_type="inherits",
                            resolution_status=status,
                            source_symbol=info.qualified_name or info.node_name,
                            target_symbol=base,
                            source_line=info.start_line,
                            metadata="python_bases",
                            confidence=0.7 if status == "resolved" else 0.4,
                            repo_name=repo_name,
                            filename=filepath,
                            lang=lang,
                        )
                    )
        elif lang_lower in {"javascript", "js", "typescript", "ts", "tsx"}:
            m_extends = re.search(
                rf"class\s+{re.escape(info.node_name)}\s*(?:<[^>]*>)?\s+extends\s+([A-Za-z_][\w\.]*)",
                text,
                re.MULTILINE,
            )
            if m_extends:
                base = m_extends.group(1).strip()
                target_puid, status = _resolve_local_symbol(base, by_name, by_qname)
                edges.append(
                    _edge(
                        source_puid=info.puid,
                        target_puid=target_puid,
                        edge_type="inherits",
                        resolution_status=status,
                        source_symbol=info.qualified_name or info.node_name,
                        target_symbol=base,
                        source_line=info.start_line,
                        metadata="extends",
                        confidence=0.75 if status == "resolved" else 0.45,
                        repo_name=repo_name,
                        filename=filepath,
                        lang=lang,
                    )
                )
            m_impl = re.search(
                rf"class\s+{re.escape(info.node_name)}.*?implements\s+([^\{{]+)",
                text,
                re.MULTILINE | re.DOTALL,
            )
            if m_impl:
                interfaces = [item.strip() for item in m_impl.group(1).split(",") if item.strip()]
                for interface in interfaces:
                    target_puid, status = _resolve_local_symbol(interface, by_name, by_qname)
                    edges.append(
                        _edge(
                            source_puid=info.puid,
                            target_puid=target_puid,
                            edge_type="implements",
                            resolution_status=status,
                            source_symbol=info.qualified_name or info.node_name,
                            target_symbol=interface,
                            source_line=info.start_line,
                            metadata="implements",
                            confidence=0.75 if status == "resolved" else 0.45,
                            repo_name=repo_name,
                            filename=filepath,
                            lang=lang,
                        )
                    )
        elif lang_lower in {"csharp", "c_sharp"}:
            # Generic classes (e.g. `class BaseRepository<T> : IBaseRepository<T>`)
            # put the type-parameter list between the class name and the base
            # list's colon - allow an optional single-level `<...>` there.
            m = re.search(
                rf"class\s+{re.escape(info.node_name)}\s*(?:<[^>]*>)?\s*:\s*([^\{{]+)",
                text,
                re.MULTILINE | re.DOTALL,
            )
            if m:
                parents = [item.strip() for item in m.group(1).split(",") if item.strip()]
                for idx, parent in enumerate(parents):
                    target_puid, status = _resolve_local_symbol(parent, by_name, by_qname)
                    edge_type = "inherits" if idx == 0 else "implements"
                    edges.append(
                        _edge(
                            source_puid=info.puid,
                            target_puid=target_puid,
                            edge_type=edge_type,
                            resolution_status=status,
                            source_symbol=info.qualified_name or info.node_name,
                            target_symbol=parent,
                            source_line=info.start_line,
                            metadata="csharp_base_list",
                            confidence=0.7 if status == "resolved" else 0.4,
                            repo_name=repo_name,
                            filename=filepath,
                            lang=lang,
                        )
                    )

    # Guarantee unresolved placeholder edges when we see import-like statements but no local target.
    return edges
