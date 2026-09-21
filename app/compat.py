"""Static version-compatibility analysis for registered message definitions.

Two versions of the same ``device_type`` are compared without touching any
payload bytes: every conditional constant partitions the referenced field's
raw-value domain into a finite set of equivalence classes (each class is
represented by one concrete value), and the Cartesian product of those
representatives enumerates every reachable branch. For each branch the
analyzer lays fields out sequentially, computing each field's bit offset and
the total bit length.

The comparison is directional (old -> new): the versions are compatible only
if every layout reachable in the old version has a corresponding layout in the
new version at the same conditional representative values in which every
field matches by name, type, bit width and bit offset, the total length
matches, and the checksum algorithm is identical. New branches that only
exist in the new version do not break old senders.
"""

from itertools import product
from typing import Any, Dict, List, Optional, Tuple

MAX_COMBINATIONS = 256


class CompatibilityError(Exception):
    """Raised when the equivalence-class space is too large to enumerate."""


def _raw_domain(field: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """Inclusive raw-value range of a numeric/bool field, or None otherwise."""
    ftype = field.get("type", "uint")
    if ftype == "bool":
        return (0, 1)
    if ftype in ("uint", "int"):
        n = field["bit_length"]
        if ftype == "uint":
            return (0, (1 << n) - 1)
        return (-(1 << (n - 1)), (1 << (n - 1)) - 1)
    return None


def _collect_conditions(fields: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Every condition grouped by the previously-defined field it references."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for field in fields:
        cond = field.get("condition")
        if cond:
            grouped.setdefault(cond["field"], []).append(cond)
    return grouped


def _condition_truth(condition: Dict[str, Any], value: int) -> bool:
    """Truth value of a condition for a hypothetical value of its field."""
    op = condition["op"]
    expected = condition["value"]
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op == "gt":
        return value > expected
    if op == "ge":
        return value >= expected
    if op == "lt":
        return value < expected
    if op == "le":
        return value <= expected
    if op == "in":
        return value in expected
    return False


def _merged_domain(
    name: str,
    old_fields: Dict[str, Dict[str, Any]],
    new_fields: Dict[str, Dict[str, Any]],
) -> Optional[Tuple[int, int]]:
    """Numeric domain of a controlling field after merging both versions.

    The union of both raw ranges is used (not the intersection): layout only
    depends on condition truth values, so a representative outside one
    version's encodable range still lays fields out validly, while the
    type/width change surfaces as a field-level difference. Fields numeric in
    only one version (e.g. later changed to hex) use that version's domain.
    """
    domains = []
    for fmap in (old_fields, new_fields):
        field = fmap.get(name)
        if field is not None:
            domain = _raw_domain(field)
            if domain is not None:
                domains.append(domain)
    if not domains:
        return None
    return (min(d[0] for d in domains), max(d[1] for d in domains))


def _representatives(
    domain: Tuple[int, int], conditions: List[Dict[str, Any]]
) -> List[int]:
    """One representative per finite equivalence class of a controlling field.

    The truth value of every condition referencing the field is constant on
    each class; candidate values are the condition constants clamped into the
    domain plus one interior point on either side of every boundary (c and
    c+1 cover all comparison boundaries). Candidates sharing the same
    condition truth-vector describe the same class and are collapsed, so a
    single ``eq`` constant yields exactly two representatives ({c} and its
    complement) and a bool field at most two.
    """
    lo, hi = domain
    candidates = {lo}
    for cond in conditions:
        raw = cond["value"]
        values = raw if isinstance(raw, list) else [raw]
        for c in values:
            for p in (c, c + 1):
                if lo <= p <= hi:
                    candidates.add(p)

    reps_by_vector: Dict[Tuple[bool, ...], int] = {}
    for p in sorted(candidates):
        vector = tuple(_condition_truth(cond, p) for cond in conditions)
        reps_by_vector.setdefault(vector, p)
    return sorted(reps_by_vector.values())


def _eval_condition(condition: Dict[str, Any], raw_values: Dict[str, int]) -> Optional[bool]:
    """Raw-value condition semantics shared with parser/encoder.

    Returns None when the referenced field is absent (it was skipped by an
    earlier branch, so this branch is unreachable).
    """
    field = condition["field"]
    if field not in raw_values:
        return None
    actual = raw_values[field]
    op = condition["op"]
    expected = condition["value"]
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "gt":
        return actual > expected
    if op == "ge":
        return actual >= expected
    if op == "lt":
        return actual < expected
    if op == "le":
        return actual <= expected
    if op == "in":
        return actual in expected
    return None


def _layout(
    fields: List[Dict[str, Any]], assignment: Dict[str, int]
) -> Optional[Dict[str, Any]]:
    """Lay the definition out for one conditional assignment.

    Returns the reachable layout (field offsets + total length), or None when
    some condition references a field that an earlier branch skipped.
    """
    raw_values: Dict[str, int] = {}
    laid: List[Dict[str, Any]] = []
    pos = 0
    for field in fields:
        cond = field.get("condition")
        if cond is not None:
            matched = _eval_condition(cond, raw_values)
            if matched is None:
                return None
            if not matched:
                continue
        name = field["name"]
        entry = {
            "name": name,
            "type": field.get("type", "uint"),
            "bit_length": field["bit_length"],
            "bit_offset": pos,
        }
        laid.append(entry)
        pos += field["bit_length"]
        if name in assignment:
            raw_values[name] = assignment[name]
        else:
            domain = _raw_domain(field)
            if domain is not None:
                raw_values[name] = domain[0]
    return {
        "fields": laid,
        "total_bits": pos,
    }


def _layout_signature(layout: Optional[Dict[str, Any]]) -> Any:
    if layout is None:
        return None
    return tuple((f["name"], f["type"], f["bit_length"], f["bit_offset"]) for f in layout["fields"])


def _layout_differences(
    old: Dict[str, Any],
    new: Optional[Dict[str, Any]],
    old_checksum: Optional[Dict[str, Any]],
    new_checksum: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Per-field/structural differences for one old -> new branch pairing."""
    diffs: List[Dict[str, Any]] = []
    if new is None:
        diffs.append(
            {
                "kind": "layout_unreachable",
                "detail": "the branch is not reachable under the new definition",
            }
        )
        return diffs

    new_by_name = {f["name"]: f for f in new["fields"]}
    for old_f in old["fields"]:
        new_f = new_by_name.get(old_f["name"])
        if new_f is None:
            diffs.append(
                {
                    "kind": "field_removed",
                    "field": old_f["name"],
                    "old": old_f,
                    "new": None,
                }
            )
            continue
        for attr in ("type", "bit_length", "bit_offset"):
            if old_f[attr] != new_f[attr]:
                diffs.append(
                    {
                        "kind": f"field_{attr}_changed",
                        "field": old_f["name"],
                        "old": old_f[attr],
                        "new": new_f[attr],
                    }
                )

    old_names = {f["name"] for f in old["fields"]}
    for new_f in new["fields"]:
        if new_f["name"] not in old_names:
            diffs.append(
                {
                    "kind": "field_added",
                    "field": new_f["name"],
                    "old": None,
                    "new": new_f,
                }
            )

    if old["total_bits"] != new["total_bits"]:
        diffs.append(
            {
                "kind": "total_length_changed",
                "old": old["total_bits"],
                "new": new["total_bits"],
            }
        )

    old_algo = old_checksum["algorithm"] if old_checksum else None
    new_algo = new_checksum["algorithm"] if new_checksum else None
    if old_algo != new_algo:
        diffs.append(
            {
                "kind": "checksum_algorithm_changed",
                "old": old_algo,
                "new": new_algo,
            }
        )
    return diffs


def analyze_versions(
    old_fields: List[Dict[str, Any]],
    old_checksum: Optional[Dict[str, Any]],
    new_fields: List[Dict[str, Any]],
    new_checksum: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare two registered definitions of the same device type.

    Returns a report with ``compatible``/``breaking`` verdict, the enumerated
    equivalence-class combinations (before and after reachability filtering)
    and one entry per distinct branch. Raises CompatibilityError when the
    Cartesian product exceeds MAX_COMBINATIONS.
    """
    old_by_name = {f["name"]: f for f in old_fields}
    new_by_name = {f["name"]: f for f in new_fields}

    conditions_by_field: Dict[str, List[Dict[str, Any]]] = {}
    for name, conds in _collect_conditions(old_fields).items():
        conditions_by_field[name] = list(conds)
    for name, conds in _collect_conditions(new_fields).items():
        merged = conditions_by_field.setdefault(name, [])
        known = {(c["op"], repr(c["value"])) for c in merged}
        for cond in conds:
            key = (cond["op"], repr(cond["value"]))
            if key not in known:
                merged.append(cond)
                known.add(key)

    controls: Dict[str, List[int]] = {}
    for name, conds in conditions_by_field.items():
        domain = _merged_domain(name, old_by_name, new_by_name)
        if domain is None:
            continue
        reps = _representatives(domain, conds)
        if reps:
            controls[name] = reps

    names = list(controls)
    combination_count = 1
    for reps in controls.values():
        combination_count *= len(reps)
    if combination_count > MAX_COMBINATIONS:
        raise CompatibilityError(
            f"conditional equivalence-class space has {combination_count} combinations; "
            f"limit is {MAX_COMBINATIONS}"
        )

    groups: Dict[Any, Dict[str, Any]] = {}
    reachable_old = reachable_new = 0
    breaking_branches = 0

    for values in product(*(controls[n] for n in names)):
        assignment = dict(zip(names, values))
        old_layout = _layout(old_fields, assignment)
        new_layout = _layout(new_fields, assignment)

        if old_layout is not None:
            reachable_old += 1
        if new_layout is not None:
            reachable_new += 1
        if old_layout is None and new_layout is None:
            status = "unreachable"
            diffs: List[Dict[str, Any]] = []
        elif old_layout is None:
            status = "new_only"
            diffs = []
        else:
            diffs = _layout_differences(
                old_layout, new_layout, old_checksum, new_checksum
            )
            status = "breaking" if diffs else "compatible"

        key = (
            status,
            _layout_signature(old_layout),
            _layout_signature(new_layout),
        )
        group = groups.get(key)
        if group is None:
            group = {
                "status": status,
                "condition_values": [],
                "old_layout": old_layout,
                "new_layout": new_layout,
                "differences": diffs,
            }
            groups[key] = group
            if status == "breaking":
                breaking_branches += 1
        group["condition_values"].append(assignment)

    def _sort_key(item: Tuple[Any, Dict[str, Any]]):
        layout = item[1]["old_layout"] or item[1]["new_layout"]
        length = layout["total_bits"] if layout else -1
        status_order = {
            "breaking": 0,
            "compatible": 1,
            "new_only": 2,
            "unreachable": 3,
        }
        return (status_order[item[1]["status"]], length, item[1]["status"])

    branches = [group for _, group in sorted(groups.items(), key=_sort_key)]

    return {
        "verdict": "breaking" if breaking_branches else "compatible",
        "combination_count": combination_count,
        "reachable_old_branches": reachable_old,
        "reachable_new_branches": reachable_new,
        "distinct_branch_count": len(branches),
        "breaking_branch_count": breaking_branches,
        "branches": branches,
    }
