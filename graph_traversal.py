from __future__ import annotations

import sys
from collections import deque
from typing import Any, Optional

import asyncpg

from indexer_flow import DATABASE_URL, PG_SCHEMA, TABLE_NAME, fetch_nodes, get_graph_edge_table_name


DEFAULT_IMPACT_EDGE_TYPES = {"calls", "imports", "inherits", "implements", "reads", "writes"}
STRICT_MIN_CONFIDENCE = 0.80

# Node types treated as their own "container" for impact-tree display purposes
# (B2/B3: the tree is shown at component/service/class granularity, not raw
# method/field nodes). Anything else (method, function, field, property, ...)
# gets rolled up to whichever one of these contains it.
CONTAINER_NODE_TYPES = {"class", "interface", "struct", "module", "file"}


def _run_sync(coro_factory):
    """Run an async DB call from sync code, reusing/threading the event loop as needed."""
    import asyncio
    import threading

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        result = []

        def thread_target():
            new_loop = asyncio.new_event_loop()
            result.append(new_loop.run_until_complete(coro_factory()))
            new_loop.close()

        t = threading.Thread(target=thread_target)
        t.start()
        t.join()
        return result[0] if result else None
    else:
        return loop.run_until_complete(coro_factory())


def _fetch_owner_map_sync(member_puids: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve each puid's immediate containing class/module/file via a reverse 'contains' edge.

    Returns {member_puid: owner_node_dict}. A puid with no contains-parent
    (e.g. a top-level function) is simply absent from the result.
    """
    if not member_puids:
        return {}

    async def _run() -> dict[str, dict[str, Any]]:
        edge_table = get_graph_edge_table_name(TABLE_NAME)
        query = f"""
            SELECT e.target_puid AS member_puid, n.puid, n.node_name, n.node_type,
                   n.qualified_name, n.filename, n.repo_name
            FROM "{PG_SCHEMA}"."{edge_table}" e
            JOIN "{PG_SCHEMA}"."{TABLE_NAME}" n ON n.puid = e.source_puid
            WHERE e.edge_type = 'contains' AND e.target_puid = ANY($1)
        """
        async with await asyncpg.create_pool(DATABASE_URL) as pool:
            rows = await pool.fetch(query, member_puids)
            return {r["member_puid"]: dict(r) for r in rows}

    try:
        return _run_sync(_run) or {}
    except Exception as ex:
        sys.stderr.write(f"[WARN] _fetch_owner_map_sync failed: {ex}\n")
        return {}


# Member node types considered when expanding a class/interface seed to its
# public surface (Phase 5A class-level /impact).
PUBLIC_MEMBER_NODE_TYPES = {"method", "function", "field", "property", "attribute", "variable"}

# Languages where an unannotated class member defaults to public visibility
# (TS/JS). C#/Java-style languages default to private/package-private, so
# they are NOT listed here - they must have an explicit 'public' modifier.
_DEFAULT_PUBLIC_LANGS = {"typescript", "ts", "javascript", "js", "tsx", "jsx"}


def _is_public_member(modifiers: str, lang: str) -> bool:
    mods = {m.strip().lower() for m in (modifiers or "").split(",") if m.strip()}
    if "public" in mods:
        return True
    if mods & {"private", "protected", "internal"}:
        return False
    return (lang or "").strip().lower() in _DEFAULT_PUBLIC_LANGS


def resolve_class_public_surface(class_puid: str) -> list[dict[str, Any]]:
    """Expand a class/interface seed to its full public surface for impact analysis.

    When `/impact` targets a whole class/component (not a specific method),
    a single seed puid is not enough: callers may invoke any public member,
    and - critically - callers using dependency injection against an
    implemented interface (e.g. `IAssignmentService svc` then `svc.UpdateAsync()`)
    record their 'calls' edge against the *interface* method's puid, not the
    concrete class method's puid. Matching interface/base methods by name to a
    member declared directly on the class (regardless of that member's own
    detected modifier - it must be public to satisfy the interface) and
    seeding BOTH puids keeps those DI-routed callers from being missed.
    """
    async def _run() -> dict[str, Any]:
        edge_table = get_graph_edge_table_name(TABLE_NAME)
        async with await asyncpg.create_pool(DATABASE_URL) as pool:
            async with pool.acquire() as conn:
                direct_rows = await conn.fetch(
                    f"""
                    SELECT n.puid, n.node_name, n.node_type, n.modifiers, n.lang, n.filename
                    FROM "{PG_SCHEMA}"."{edge_table}" e
                    JOIN "{PG_SCHEMA}"."{TABLE_NAME}" n ON n.puid = e.target_puid
                    WHERE e.source_puid = $1 AND e.edge_type = 'contains'
                    """,
                    class_puid,
                )

                base_edges = await conn.fetch(
                    f"""
                    SELECT target_puid, target_symbol, resolution_status, edge_type
                    FROM "{PG_SCHEMA}"."{edge_table}"
                    WHERE source_puid = $1 AND edge_type IN ('inherits', 'implements')
                    """,
                    class_puid,
                )
                base_puids = [r["target_puid"] for r in base_edges if r["target_puid"]]

                base_member_rows = []
                if base_puids:
                    base_member_rows = await conn.fetch(
                        f"""
                        SELECT n.puid, n.node_name, n.node_type, n.filename, e.source_puid AS owner_puid
                        FROM "{PG_SCHEMA}"."{edge_table}" e
                        JOIN "{PG_SCHEMA}"."{TABLE_NAME}" n ON n.puid = e.target_puid
                        WHERE e.source_puid = ANY($1) AND e.edge_type = 'contains'
                        """,
                        base_puids,
                    )

        return {
            "direct": [dict(r) for r in direct_rows],
            "base_edges": [dict(r) for r in base_edges],
            "base_members": [dict(r) for r in base_member_rows],
        }

    try:
        data = _run_sync(_run) or {"direct": [], "base_edges": [], "base_members": []}
    except Exception as ex:
        sys.stderr.write(f"[WARN] resolve_class_public_surface failed: {ex}\n")
        return []

    def _fmt_direct(r: dict[str, Any]) -> str:
        return f"{r.get('node_name')}:{r.get('node_type')}:mods={r.get('modifiers')!r}:lang={r.get('lang')!r}"

    def _fmt_base(r: dict[str, Any]) -> str:
        return f"{r.get('node_name')}:{r.get('node_type')}"

    def _fmt_base_edge(r: dict[str, Any]) -> str:
        return (
            f"{r.get('edge_type')}->{r.get('target_symbol')!r} "
            f"status={r.get('resolution_status')!r} target_puid={r.get('target_puid')!r}"
        )

    sys.stderr.write(
        f"[IMPACT_SURFACE] class_puid={class_puid!r} -> "
        f"{len(data['direct'])} raw contains-member row(s) "
        f"[{', '.join(_fmt_direct(r) for r in data['direct'][:20])}], "
        f"{len(data['base_edges'])} raw inherits/implements edge(s) "
        f"[{', '.join(_fmt_base_edge(r) for r in data['base_edges'][:10])}], "
        f"{len(data['base_members'])} base/interface member row(s) "
        f"[{', '.join(_fmt_base(r) for r in data['base_members'][:20])}]\n"
    )

    direct = [d for d in data["direct"] if (d.get("node_type") or "") in PUBLIC_MEMBER_NODE_TYPES]
    base_members = [b for b in data["base_members"] if (b.get("node_type") or "") in PUBLIC_MEMBER_NODE_TYPES]

    direct_by_name = {d["node_name"]: d for d in direct if d.get("node_name")}
    interface_matched_names = {b["node_name"] for b in base_members if b.get("node_name") in direct_by_name}

    surface: list[dict[str, Any]] = []
    for d in direct:
        name = d.get("node_name", "")
        is_public = _is_public_member(d.get("modifiers", ""), d.get("lang", "")) or name in interface_matched_names
        if is_public:
            surface.append({
                "puid": d["puid"],
                "node_name": name,
                "node_type": d.get("node_type", ""),
                "filename": d.get("filename", ""),
                "via": "own",
            })
    for b in base_members:
        name = b.get("node_name", "")
        if name in interface_matched_names:
            surface.append({
                "puid": b["puid"],
                "node_name": name,
                "node_type": b.get("node_type", ""),
                "filename": b.get("filename", ""),
                "via": f"interface:{b.get('owner_puid', '')}",
                # The interface method is only a DI routing detail - any
                # caller found through it is really impacting this concrete
                # class, so force it to roll up to the SAME owner instead of
                # showing up as a separate "IAssetService"-style node.
                "owner_override": class_puid,
            })

    return surface


def resolve_method_interface_seeds(method_puid: str, method_name: str) -> list[dict[str, Any]]:
    """Find the interface/base-class method(s) that a concrete method's owning
    class implements/overrides, matched by name.

    The DI-via-interface problem isn't limited to whole-class seeds: even when
    `/impact` names one exact method (e.g. `AssetService.AddAsync`), if callers
    hold a reference typed as the interface (`IAssetService _assetService`),
    their 'calls' edge targets the *interface* method's puid, not this
    concrete one. Without also seeding that interface method, such callers
    are invisible to the BFS even though the seed itself resolved correctly.
    """
    owner_map = _fetch_owner_map_sync([method_puid])
    owner = owner_map.get(method_puid)
    if not owner or (owner.get("node_type") or "") not in ("class", "interface", "struct"):
        return []

    full_surface = resolve_class_public_surface(owner["puid"])
    return [
        m for m in full_surface
        if m.get("node_name") == method_name and (m.get("via") or "").startswith("interface:")
    ]


def roll_up_to_owners(
    seed_puids: list[str],
    affected_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    known_node_info: dict[str, dict[str, Any]] | None = None,
    owner_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Collapse method/field-level BFS results up to their owning class/component.

    B2/B3 requirement: the impact tree must read at component/service/class
    granularity with file+line as metadata, not as a flat list of individual
    methods. This walks every puid involved (seeds, affected nodes, edge
    endpoints) up to its nearest container (class/interface/struct/module/file)
    via a 'contains' reverse-edge lookup, then re-keys affected_nodes/edges to
    those container puids. Only 'calls'/'reads'/'writes' edges are kept in the
    final result - the user wants a pure caller/reference tree, not structural
    contains/inherits/implements noise. Self-loop edges (caller and callee
    collapsing to the same container, e.g. a private helper calling a public
    method in the same class) are dropped since they carry no cross-boundary
    impact information.

    `known_node_info` is an optional {puid: {node_name/qualified_name, filename}}
    map of data the caller already resolved (e.g. seed candidates from the
    exact-match lookup). It is used as a fallback for display metadata when a
    puid turns out to be its own container (no 'contains' parent found) so a
    fresh DB lookup isn't required just to re-derive a name/file already known.

    `owner_overrides` is an optional {puid: forced_owner_puid} map for seeds
    that are themselves a DI routing detail (e.g. an interface method seeded
    only so its callers aren't missed) - their owner is forced to the
    concrete class being analyzed instead of their own natural container, so
    they don't show up as a separate "IInterfaceName" node in the tree.
    """
    known_node_info = known_node_info or {}
    owner_overrides = owner_overrides or {}

    all_puids: set[str] = {p for p in seed_puids if p}
    for n in affected_nodes:
        if n.get("puid"):
            all_puids.add(n["puid"])
    for e in edges:
        if e.get("source_puid"):
            all_puids.add(e["source_puid"])
        if e.get("target_puid"):
            all_puids.add(e["target_puid"])

    if not all_puids:
        return {
            "start_puids": list(dict.fromkeys(p for p in seed_puids if p)),
            "seed_info": {},
            "affected_nodes": affected_nodes,
            "edges": edges,
        }

    try:
        nodes = fetch_nodes(list(all_puids))
    except Exception as ex:
        sys.stderr.write(f"[WARN] roll_up_to_owners fetch_nodes failed: {ex}\n")
        nodes = []
    node_type_map = {n.get("puid", ""): (n.get("node_type") or "") for n in nodes}
    node_meta_map = {n.get("puid", ""): n for n in nodes}

    non_container = [p for p in all_puids if node_type_map.get(p, "") not in CONTAINER_NODE_TYPES]
    owner_map = _fetch_owner_map_sync(non_container) if non_container else {}

    def owner_of(puid: str) -> str:
        if not puid:
            return puid
        if puid in owner_overrides:
            return owner_overrides[puid]
        if node_type_map.get(puid, "") in CONTAINER_NODE_TYPES:
            return puid
        owner = owner_map.get(puid)
        if owner and owner.get("puid"):
            return owner["puid"]
        return puid  # no contains-parent found; treat the node as its own container

    owner_info: dict[str, dict[str, Any]] = {}

    def register_owner(puid: str, fallback: dict[str, Any] | None = None) -> None:
        if not puid or puid in owner_info:
            return
        meta = node_meta_map.get(puid) or owner_map.get(puid) or fallback or known_node_info.get(puid)
        owner_info[puid] = {
            "node_name": (meta or {}).get("node_name") or (meta or {}).get("qualified_name") or "",
            "filename": (meta or {}).get("filename", ""),
            "node_type": (meta or {}).get("node_type", ""),
        }

    new_start_puids: list[str] = []
    for p in seed_puids:
        if not p:
            continue
        o = owner_of(p)
        register_owner(o)
        if o not in new_start_puids:
            new_start_puids.append(o)

    # Register display metadata for every owner that could possibly surface,
    # before we know yet which ones survive edge filtering below.
    for n in affected_nodes:
        puid = n.get("puid")
        if not puid:
            continue
        o = owner_of(puid)
        register_owner(o, fallback=n if o == puid else None)
        for step in n.get("path") or ():
            register_owner(owner_of(step))

    CALLER_EDGE_TYPES = {"calls", "reads", "writes"}
    new_edges: list[dict[str, Any]] = []
    for e in edges:
        # Only actual caller/reference relationships count: 'calls' (method
        # invocation) and 'reads'/'writes' (property/field access). 'contains'
        # only ever connects a member to its own owner (pure structural noise
        # once rolled up - e.g. "file contains class", or "X called by
        # itself"). 'inherits'/'implements' are structural too, not callers -
        # the interface/base-class methods they'd otherwise surface were
        # already folded into the concrete class via owner_overrides above.
        if (e.get("edge_type") or "") not in CALLER_EDGE_TYPES:
            continue
        src_owner = owner_of(e.get("source_puid", ""))
        tgt_owner = owner_of(e.get("target_puid", ""))
        if not src_owner or not tgt_owner or src_owner == tgt_owner:
            continue
        remapped = dict(e)
        remapped["source_puid"] = src_owner
        remapped["target_puid"] = tgt_owner
        # Display label = the class/component owner; the specific method
        # (source_symbol/target_symbol, untouched above) stays as metadata
        # rather than becoming its own node in the diagram.
        remapped["source_node"] = owner_info.get(src_owner, {}).get("node_name") or src_owner
        remapped["target_node"] = owner_info.get(tgt_owner, {}).get("node_name") or tgt_owner
        new_edges.append(remapped)

    # Rebuild affected_nodes by walking the *filtered* edges themselves
    # (parent=target_puid/callee, child=source_puid/caller), rooted at the
    # seed owners. This guarantees every surviving node's path is backed
    # entirely by real, cross-owner edges - no leftover hops through a
    # container that only had a 'contains'/self-loop connection (which would
    # otherwise render as a blank ghost node in the tree).
    children_of: dict[str, list[dict[str, Any]]] = {}
    for e in new_edges:
        children_of.setdefault(e["target_puid"], []).append(e)

    new_affected_nodes: list[dict[str, Any]] = []
    visited_owner: set[str] = set(new_start_puids)
    queue: deque[tuple[str, int, list[str]]] = deque((root, 0, [root]) for root in new_start_puids)
    while queue:
        cur, depth, path = queue.popleft()
        for e in children_of.get(cur, []):
            child = e["source_puid"]
            if not child or child in visited_owner:
                continue
            visited_owner.add(child)
            next_path = path + [child]
            new_affected_nodes.append(
                {
                    "puid": child,
                    "node_name": owner_info.get(child, {}).get("node_name", ""),
                    "filename": owner_info.get(child, {}).get("filename", ""),
                    "depth": depth + 1,
                    "via_edge_type": e.get("edge_type", ""),
                    "path": next_path,
                }
            )
            queue.append((child, depth + 1, next_path))

    seed_info_out = {p: owner_info.get(p, {}) for p in new_start_puids}

    sys.stderr.write(
        f"[IMPACT_ROLLUP] {len(affected_nodes)} raw node(s) -> {len(new_affected_nodes)} owner-level node(s), "
        f"{len(edges)} raw edge(s) -> {len(new_edges)} owner-level edge(s) "
        "(only calls/reads/writes kept; contains/inherits/implements and self-loops excluded)\n"
    )

    return {
        "start_puids": new_start_puids,
        "seed_info": seed_info_out,
        "affected_nodes": new_affected_nodes,
        "edges": new_edges,
    }


def _fetch_all_edges_sync(
    edge_types: set[str],
    repo_name: str | None = None,
) -> list[dict[str, Any]]:
    async def _run() -> list[dict[str, Any]]:
        edge_table = get_graph_edge_table_name(TABLE_NAME)
        params: list[object] = [list(edge_types)]
        clauses = ["edge_type = ANY($1)"]
        if repo_name:
            params.append(repo_name)
            clauses.append(f"repo_name = ${len(params)}")

        query = f"""
            SELECT id, repo_name, filename, lang, edge_type, resolution_status, confidence,
                   source_puid, target_puid, source_symbol, target_symbol, source_line, target_line, metadata
            FROM "{PG_SCHEMA}"."{edge_table}"
            WHERE {" AND ".join(clauses)}
            ORDER BY edge_type, source_puid, target_puid, source_line
        """

        async with await asyncpg.create_pool(DATABASE_URL) as pool:
            rows = await pool.fetch(query, *params)
            return [dict(r) for r in rows]

    def _safe_run():
        import asyncio
        import threading
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        if loop.is_running():
            result = []
            def thread_target():
                new_loop = asyncio.new_event_loop()
                result.extend(new_loop.run_until_complete(_run()))
                new_loop.close()
            t = threading.Thread(target=thread_target)
            t.start()
            t.join()
            return result
        else:
            return loop.run_until_complete(_run())

    try:
        return _safe_run()
    except Exception as ex:
        import sys
        sys.stderr.write(f"[WARN] _fetch_all_edges_sync failed: {ex}\n")
        return []


def run_impact_bfs(
    start_puids: list[str],
    start_symbols: list[str] = None,
    edge_types: set[str] | None = None,
    max_depth: int = 3,
    repo_name: str | None = None,
    mode: str = "strict",
) -> dict[str, Any]:
    """Reverse BFS over the graph edges to find nodes affected by a change.

    mode="strict" (default): only traverse edges with resolution_status=="resolved"
    and confidence >= STRICT_MIN_CONFIDENCE. No substring/name fallback matching is
    used — every traversed hop must be backed by a resolved PUID-to-PUID edge.

    mode="exploratory": also considers ambiguous/unresolved edges and the legacy
    substring-based symbol fallback, but every such hop is flagged with
    is_strict=False so callers can route it into warnings instead of the trusted tree.
    """
    if edge_types is None:
        edge_types = set(DEFAULT_IMPACT_EDGE_TYPES)
        edge_types.add("contains")

    if not start_symbols:
        start_symbols = []

    start_puids = [p for p in start_puids if p]
    strict_mode = mode != "exploratory"
    if not start_puids and (strict_mode or not start_symbols):
        return {
            "affected_nodes": [],
            "edges": [],
            "max_depth_reached": False,
            "total_count": 0,
        }

    strict = strict_mode

    all_edges = _fetch_all_edges_sync(edge_types, repo_name)
    fetched_count = len(all_edges)
    if strict:
        all_edges = [
            e
            for e in all_edges
            if (e.get("resolution_status") or "") == "resolved"
            and float(e.get("confidence") or 0.0) >= STRICT_MIN_CONFIDENCE
        ]
    sys.stderr.write(
        f"[IMPACT_TRAVERSAL] mode={mode} repo_name={repo_name!r} edge_types={sorted(edge_types)} "
        f"fetched={fetched_count} edges, kept={len(all_edges)} after strict-filter, "
        f"start_puids={start_puids}\n"
    )

    reverse_adj: dict[str, list[dict[str, Any]]] = {}
    forward_adj: dict[str, list[dict[str, Any]]] = {}

    for edge in all_edges:
        tgt = edge.get("target_puid")
        src = edge.get("source_puid")

        if tgt:
            reverse_adj.setdefault(tgt, []).append(edge)
        elif not strict:
            tgt_sym = edge.get("target_symbol")
            if tgt_sym:
                for sym in start_symbols:
                    if sym and (sym.lower() in tgt_sym.lower()):
                        reverse_adj.setdefault(sym, []).append(edge)
                        break

        if src:
            forward_adj.setdefault(src, []).append(edge)

    queue = deque()
    for puid in start_puids:
        queue.append((puid, 0, [puid]))
    if not strict:
        for sym in start_symbols:
            if sym:
                queue.append((sym, 0, [sym]))

    visited: set[str] = set(start_puids)
    if not strict:
        visited.update(s for s in start_symbols if s)
    affected: list[dict[str, Any]] = []
    result_edges: list[dict[str, Any]] = []
    max_depth_reached = False

    def _node_ref(puid: str) -> dict[str, str]:
        node = node_lookup.get(puid, {})
        return {
            "puid": puid,
            "symbol": node.get("node_name") or node.get("qualified_name") or "",
            "file": node.get("filename") or "",
        }

    node_lookup: dict[str, dict[str, Any]] = {}
    all_puids = {p for p in (start_puids or [])}
    for edge in all_edges:
        for key in ("source_puid", "target_puid"):
            puid = edge.get(key)
            if puid:
                all_puids.add(puid)
    if all_puids:
        try:
            for node in fetch_nodes(list(all_puids)):
                node_lookup[node.get("puid", "")] = node
        except Exception as ex:
            sys.stderr.write(f"[WARN] run_impact_bfs node lookup failed: {ex}\n")

    def _make_evidence_edge(edge: dict[str, Any], source_puid: str, target_puid: str, depth: int, path: list[str]) -> dict[str, Any]:
        source_node = node_lookup.get(source_puid, {})
        target_node = node_lookup.get(target_puid, {})
        # Prefer the qualified name (e.g. "AssetController.AddAsync") so the
        # *specific* method involved survives as metadata once nodes get
        # rolled up to class/component granularity for display.
        return {
            "source_puid": source_puid,
            "source_symbol": source_node.get("qualified_name") or source_node.get("node_name") or edge.get("source_symbol") or "",
            "source_file": source_node.get("filename") or edge.get("filename") or "",
            "source_line": edge.get("source_line") or "line_unknown",
            "target_puid": target_puid,
            "target_symbol": target_node.get("qualified_name") or target_node.get("node_name") or edge.get("target_symbol") or "",
            "target_file": target_node.get("filename") or edge.get("filename") or "",
            "target_line": edge.get("target_line") or "line_unknown",
            "edge_type": edge.get("edge_type", ""),
            "resolution_status": edge.get("resolution_status", ""),
            "confidence": float(edge.get("confidence") or 0.0),
            "metadata": edge.get("metadata", ""),
            "depth": depth,
            "path": list(path),
        }

    while queue:
        node, depth, path = queue.popleft()
        if depth >= max_depth:
            max_depth_reached = True
            continue

        for edge in reverse_adj.get(node, []):
            caller = edge.get("source_puid")
            if not caller:
                continue

            next_path = path + [caller]
            # `visited` only gates re-enqueueing/re-adding the node (cycle safety).
            # The edge itself is always recorded so a caller reached via two
            # different call sites (or via two different seed members) keeps
            # both pieces of evidence instead of silently dropping the second.
            result_edges.append(_make_evidence_edge(edge, caller, node, depth + 1, next_path))
            if caller in visited:
                continue

            visited.add(caller)
            ref = _node_ref(caller)
            affected.append(
                {
                    "puid": caller,
                    "node_name": ref["symbol"] or edge.get("source_symbol", ""),
                    "filename": ref["file"] or edge.get("filename", ""),
                    "depth": depth + 1,
                    "via_edge_type": edge.get("edge_type", ""),
                    "path": next_path,
                }
            )
            queue.append((caller, depth + 1, next_path))

        for edge in forward_adj.get(node, []):
            if edge.get("edge_type") == "contains":
                child = edge.get("target_puid")
                if not child or child in visited:
                    continue
                visited.add(child)
                next_path = path + [child]
                ref = _node_ref(child)
                affected.append(
                    {
                        "puid": child,
                        "node_name": ref["symbol"] or edge.get("target_symbol", ""),
                        "filename": ref["file"] or edge.get("filename", ""),
                        "depth": depth + 1,
                        "via_edge_type": "contains",
                        "path": next_path,
                    }
                )
                result_edges.append(_make_evidence_edge(edge, node, child, depth + 1, next_path))
                queue.append((child, depth + 1, next_path))

    sys.stderr.write(
        f"[IMPACT_TRAVERSAL] BFS finished: {len(affected)} affected node(s), "
        f"{len(result_edges)} evidence edge(s), max_depth_reached={max_depth_reached}\n"
    )

    return {
        "affected_nodes": affected,
        "edges": result_edges,
        "max_depth_reached": max_depth_reached,
        "total_count": len(affected),
        "mode": mode,
    }


def build_impact_tree(
    seed_puids: list[str],
    affected_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    seed_info: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Phase 5A.7 - Turn the flat reverse-BFS result into a nested ripple tree.

    Each affected node carries its root-to-node `path` (list of PUIDs) from
    run_impact_bfs(). We use those paths to build a parent->children trie
    rooted at the seed(s), so the LLM receives an explicit "oil-spill" tree:
    seed -> direct callers -> their callers -> ... instead of a flat list.
    """
    seed_info = seed_info or {}

    node_meta: dict[str, dict[str, Any]] = {}
    for puid in seed_puids:
        meta = seed_info.get(puid, {})
        node_meta[puid] = {
            "puid": puid,
            "symbol": meta.get("node_name") or meta.get("qualified_name") or "",
            "file": meta.get("filename") or "",
        }
    for node in affected_nodes:
        puid = node.get("puid")
        if not puid:
            continue
        node_meta[puid] = {
            "puid": puid,
            "symbol": node.get("node_name", ""),
            "file": node.get("filename", ""),
        }

    # edge evidence is recorded as source_puid=child(caller), target_puid=parent
    # (the node being called). Index by (parent, child) for quick lookup. A
    # single (parent, child) pair can carry multiple edges (e.g. once the
    # rollup collapses several methods into one class/component node, or a
    # caller invokes the same target from more than one line) - keep all of
    # them so metadata isn't silently dropped to the last one seen.
    edge_lookup: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for edge in edges:
        key = (edge.get("target_puid"), edge.get("source_puid"))
        edge_lookup.setdefault(key, []).append(edge)

    children_order: dict[str, list[str]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for node in affected_nodes:
        path = node.get("path") or []
        for i in range(len(path) - 1):
            parent, child = path[i], path[i + 1]
            pair = (parent, child)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            children_order.setdefault(parent, []).append(child)

    def build_node(puid: str) -> dict[str, Any]:
        meta = node_meta.get(puid, {"puid": puid, "symbol": "", "file": ""})
        node_dict = dict(meta)
        children = []
        for child in children_order.get(puid, []):
            edge_list = edge_lookup.get((puid, child), [])
            primary = edge_list[0] if edge_list else {}
            child_node = build_node(child)
            child_node["edge_type"] = primary.get("edge_type", "")
            child_node["line"] = primary.get("source_line", "line_unknown")
            child_node["resolution_status"] = primary.get("resolution_status", "")
            child_node["confidence"] = primary.get("confidence", 0.0)
            # The specific methods involved (e.g. "AssetController.AddAsync"
            # calls "AssetService.AddAsync") - kept as metadata on the
            # class/component-level edge, per the caller-tree requirement.
            child_node["caller_method"] = primary.get("source_symbol", "")
            child_node["callee_method"] = primary.get("target_symbol", "")
            if len(edge_list) > 1:
                child_node["refs"] = [
                    {
                        "edge_type": e.get("edge_type", ""),
                        "line": e.get("source_line", "line_unknown"),
                        "caller_method": e.get("source_symbol", ""),
                        "callee_method": e.get("target_symbol", ""),
                    }
                    for e in edge_list
                ]
            children.append(child_node)
        node_dict["children"] = children
        return node_dict

    return [build_node(seed) for seed in seed_puids]


def render_impact_tree_text(tree: list[dict[str, Any]], label: str = "CHANGED") -> str:
    """Render a nested impact tree (from build_impact_tree) as indented text for an LLM prompt."""
    lines: list[str] = []

    def walk(node: dict[str, Any], depth: int, prefix: str) -> None:
        indent = "  " * depth
        symbol = node.get("symbol") or node.get("puid", "")
        file_ref = node.get("file", "")
        if depth == 0:
            lines.append(f"{indent}[{label}] {symbol} ({file_ref})")
        else:
            edge_type = node.get("edge_type", "")
            line = node.get("line", "line_unknown")
            caller_method = node.get("caller_method", "")
            callee_method = node.get("callee_method", "")
            method_info = f" — {caller_method} {edge_type} {callee_method}" if caller_method and callee_method else ""
            lines.append(f"{indent}{prefix} [{edge_type}] {symbol} ({file_ref}:L{line}){method_info}")
            for ref in node.get("refs", [])[1:]:
                ref_caller = ref.get("caller_method", "")
                ref_callee = ref.get("callee_method", "")
                ref_method_info = f" — {ref_caller} {ref.get('edge_type', '')} {ref_callee}" if ref_caller and ref_callee else ""
                lines.append(
                    f"{indent}    + [{ref.get('edge_type', '')}] also at {file_ref}:L{ref.get('line', 'line_unknown')}{ref_method_info}"
                )
        for child in node.get("children", []):
            walk(child, depth + 1, "affected by ->")

    for root in tree:
        walk(root, 0, "")
    return "\n".join(lines) if lines else "No impact tree could be built."


def impact_puids_to_nodes(impact_result: dict[str, Any]) -> list[dict[str, Any]]:
    affected = impact_result.get("affected_nodes", [])
    puids = [node.get("puid") for node in affected if node.get("puid")]
    if not puids:
        return []

    nodes = fetch_nodes(puids)
    depth_map = {node.get("puid", ""): node.get("depth", 0) for node in affected}
    for node in nodes:
        node["depth"] = depth_map.get(node.get("puid", ""), 0)
    nodes.sort(key=lambda x: depth_map.get(x.get("puid", ""), 99))
    return nodes
