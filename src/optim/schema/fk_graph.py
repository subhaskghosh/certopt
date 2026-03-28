"""Foreign-key graph for join-path enumeration.

Builds a networkx graph from FK edges and provides join-tree enumeration
(top-k shortest paths, Steiner tree approximation) for binding.
"""

from __future__ import annotations

from typing import Optional

import networkx as nx

from .catalog import Catalog, ForeignKey


def build_fk_graph(catalog: Catalog) -> nx.Graph:
    """Build an undirected graph where nodes are tables and edges are FK relationships.

    Edge attributes include the FK columns and direction.
    """
    g = nx.Graph()

    for table_name in catalog.tables:
        g.add_node(table_name.lower())

    for fk in catalog.foreign_keys:
        src = fk.src_table.lower()
        dst = fk.dst_table.lower()
        if g.has_edge(src, dst):
            existing = g.edges[src, dst]
            fk_list = existing.get("fk_list", [existing.get("fk")])
            fk_list.append(fk)
            existing["fk_list"] = fk_list
        else:
            g.add_edge(src, dst, fk=fk, fk_list=[fk], weight=1)

    return g


def find_join_paths(
    graph: nx.Graph,
    source: str,
    target: str,
    max_paths: int = 5,
    max_hops: int = 6,
) -> list[list[str]]:
    """Find up to max_paths shortest paths between two tables.

    Returns a list of paths, where each path is a list of table names.
    """
    src = source.lower()
    tgt = target.lower()

    if src not in graph or tgt not in graph:
        return []

    if src == tgt:
        return [[src]]

    try:
        paths = list(nx.all_shortest_paths(graph, src, tgt))
        # Filter by max_hops and take top-k
        paths = [p for p in paths if len(p) - 1 <= max_hops]
        return paths[:max_paths]
    except nx.NetworkXNoPath:
        return []


def find_join_tree(
    graph: nx.Graph,
    tables: list[str],
    max_trees: int = 5,
) -> list[list[str]]:
    """Find connecting subgraphs (approximate Steiner trees) for a set of tables.

    For 2 tables: simple shortest paths.
    For >2 tables: greedy Steiner approximation — iteratively connect the
    nearest unconnected table.

    Returns a list of candidate table orderings (join sequences).
    """
    tables_lower = [t.lower() for t in tables]

    if len(tables_lower) <= 1:
        return [tables_lower]

    if len(tables_lower) == 2:
        return find_join_paths(graph, tables_lower[0], tables_lower[1], max_paths=max_trees)

    # Greedy Steiner: start from each table, greedily add nearest
    candidates: list[list[str]] = []

    for start in tables_lower:
        tree_nodes: list[str] = [start]
        remaining = set(tables_lower) - {start}

        while remaining:
            best_path: list[str] | None = None
            best_target: str | None = None
            best_len = float("inf")

            for target in remaining:
                for node in tree_nodes:
                    try:
                        path = nx.shortest_path(graph, node, target)
                        if len(path) < best_len:
                            best_len = len(path)
                            best_path = path
                            best_target = target
                    except nx.NetworkXNoPath:
                        continue

            if best_path is None:
                break  # Disconnected tables

            # Add intermediate nodes to the tree
            for n in best_path:
                if n not in tree_nodes:
                    tree_nodes.append(n)
            if best_target is not None:
                remaining.discard(best_target)

        if len(tree_nodes) >= len(tables_lower):
            candidates.append(tree_nodes)

    # Deduplicate by sorted content
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for c in candidates:
        key = tuple(sorted(c))
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique[:max_trees]


def get_fk_for_edge(
    graph: nx.Graph,
    table_a: str,
    table_b: str,
) -> Optional[ForeignKey]:
    """Get the FK metadata for an edge between two tables."""
    a = table_a.lower()
    b = table_b.lower()
    if graph.has_edge(a, b):
        data = graph.edges[a, b]
        return data.get("fk")
    return None


def score_join_path(
    graph: nx.Graph,
    path: list[str],
    catalog: Catalog,
) -> float:
    """Score a join path. Lower is better.

    Scoring:
    - +1 per hop
    - +5 if a many-to-many is detected (both sides lack unique keys)
    """
    score = len(path) - 1  # Number of joins = hops

    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        fk = get_fk_for_edge(graph, a, b)
        if fk:
            # Check if either side has a PK on the join column → 1:many
            src_table = catalog.get_table(fk.src_table)
            dst_table = catalog.get_table(fk.dst_table)
            src_is_pk = src_table and fk.src_column in src_table.primary_keys if src_table else False
            dst_is_pk = dst_table and fk.dst_column in dst_table.primary_keys if dst_table else False
            if not src_is_pk and not dst_is_pk:
                score += 5  # Possible many-to-many penalty

    return score
