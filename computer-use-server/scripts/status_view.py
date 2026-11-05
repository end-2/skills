"""CLI-only views of observed state; server snapshot validation stays unchanged."""
from collections import defaultdict
import copy


def filter_status(reply, *, match=None, role=None, actionable=False, limit=None):
    """Return matching nodes and their ancestors without mutating the source reply.

    A limit applies to matches, not ancestor context. Even an empty match result
    retains observation scope and marks omitted UI as incomplete.
    """
    if not reply.get("ok") or (match is None and role is None and not actionable and limit is None):
        return reply
    source = reply["result"]["state"]
    nodes = source.get("nodes", [])
    query = match.casefold() if match is not None else None

    def matches(node):
        if role is not None and node.get("role") != role:
            return False
        if actionable and (node.get("enabled") is False or not (node.get("actions") or node.get("settable"))):
            return False
        if query is not None and not any(
                query in node[field].casefold() for field in ("title", "description")
                if isinstance(node.get(field), str)):
            return False
        return True

    candidates = [node["id"] for node in nodes if matches(node)]
    selected = candidates[:limit] if limit is not None else candidates
    node_ids = {node["id"] for node in nodes}
    parents = defaultdict(set)
    for node in nodes:
        for child in node.get("children", []):
            if child in node_ids:
                parents[child].add(node["id"])
    included = set(selected)
    pending = list(selected)
    while pending:
        for parent in parents[pending.pop()]:
            if parent not in included:
                included.add(parent)
                pending.append(parent)

    view_nodes = []
    for node in nodes:
        if node["id"] in included:
            projected = copy.deepcopy(node)
            if "children" in projected:
                projected["children"] = [child for child in projected["children"] if child in included]
            view_nodes.append(projected)

    # Keep all other state, including app identities, native capabilities,
    # secure markers, and full-text truncation hashes. Only the tree is projected.
    result = copy.deepcopy({key: value for key, value in reply["result"].items() if key != "state"})
    state = copy.deepcopy({key: value for key, value in source.items() if key not in {"nodes", "roots"}})
    result["state"] = state
    state["nodes"] = view_nodes
    state["roots"] = [node_id for node_id in source.get("roots", []) if node_id in included]
    omitted = len(nodes) - len(view_nodes)
    state["truncated"] = bool(source.get("truncated")) or omitted > 0
    result["view"] = {
        "filtered": True, "match": match, "role": role, "actionable_only": actionable, "limit": limit,
        "total_nodes": len(nodes), "matching_nodes": len(candidates), "returned_matches": len(selected),
        "matched_node_ids": selected, "context_nodes": len(view_nodes) - len(selected),
        "returned_nodes": len(view_nodes), "omitted_nodes": omitted,
        "source_truncated": bool(source.get("truncated")),
    }
    return dict(reply, result=result)
