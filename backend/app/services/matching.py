"""Distinct-item matching for assemblies.

A stock count per line is wrong when alternatives overlap: one physical item must never satisfy two
required lines (or two units of one line). This is a bipartite matching of items to line slots; the
same function backs build verification now and the WO readiness check in a later phase."""
from dataclasses import dataclass, field


@dataclass
class Line:
    line_id: int
    label: str                       # design code, for messages
    quantity: int                    # number of distinct items required
    # design_id -> set of acceptable design-revision ids (primary design and approved alternatives)
    accepts: dict[int, set[int]] = field(default_factory=dict)


@dataclass
class Candidate:
    item_id: int
    design_id: int
    revision_id: int


def accepts(line: Line, item: Candidate) -> bool:
    return item.revision_id in line.accepts.get(item.design_id, ())


def match(lines: list[Line], items: list[Candidate]):
    """Maximum matching of items onto line slots (Kuhn's augmenting paths).

    Returns (assignment {item_id: line_id}, unmatched item ids, unfilled {line_id: missing count}).
    All requirements are met iff there are no unfilled lines; every given item is used iff there are
    no unmatched items."""
    slots = [(ln.line_id, k) for ln in lines for k in range(ln.quantity)]
    by_line = {ln.line_id: ln for ln in lines}
    owner: dict[tuple[int, int], int] = {}          # slot -> index of item holding it
    compat = [[s for s in slots if accepts(by_line[s[0]], it)] for it in items]

    def augment(i: int, seen: set) -> bool:
        for s in compat[i]:
            if s in seen:
                continue
            seen.add(s)
            if s not in owner or augment(owner[s], seen):
                owner[s] = i
                return True
        return False

    matched = [augment(i, set()) for i in range(len(items))]
    assignment = {items[i].item_id: s[0] for s, i in owner.items()}
    unmatched = [it.item_id for it, ok in zip(items, matched) if not ok and it.item_id not in assignment]
    filled: dict[int, int] = {}
    for s in owner:
        filled[s[0]] = filled.get(s[0], 0) + 1
    unfilled = {ln.line_id: ln.quantity - filled.get(ln.line_id, 0) for ln in lines
                if ln.quantity - filled.get(ln.line_id, 0) > 0}
    return assignment, unmatched, unfilled
