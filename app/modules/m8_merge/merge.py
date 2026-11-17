"""三分支合并：共同基线 + 两组独立改动 → 合成一份候选规格。

口径先定死，页面与发布都只认这里的结果：

- 对齐一律走身份 ``field_id``，不看列名；改名不是删除加新增。
- 同一字段两边各改各的（哪怕改的是不同属性）→ 冲突。
  按属性拼出来的是两份修改都没被确认过的第三种规格，所以只能整条选边。
- 一边删、另一边同一身份有改动 → 冲突；删还是留，整条选边。
- 同名异身份：同一个「表.列名」在两边挂在不同身份上 → 冲突。
  选边以「整组身份」为单位：选左就整组取左版本，选右就整组取右版本，
  绝不把两边的字段各留一半。
- 两边都改且改得一模一样 → 视为双方都确认过，直接采用。
- 没被任何一边改动的基线字段原样保留；只有一边删、另一边没动 → 删除，
  并注明是哪一边删的。
- 裁决落地后若仍出现同表重名 → 硬拒绝，不给选边，直接挡住发布。

同名冲突与同身份冲突用并查集并成一组：一个名字把两个身份连起来，
这个名字的归属就只能整组裁一次，避免「A 选左、B 选右」拼出第三种结果。

本文件只做纯函数，不碰文件与库，便于直接测试。
"""

from __future__ import annotations

import hashlib
import json

TAKE_LEFT = "take-left"
TAKE_RIGHT = "take-right"
CHOICES = (TAKE_LEFT, TAKE_RIGHT)


def field_signature(field: dict) -> str:
    """字段可见属性的稳定指纹：表、名字、类型、必填。"""
    payload = json.dumps(
        [
            (field.get("table") or "").lower(),
            (field.get("name") or "").lower(),
            (field.get("type_raw") or "").lower(),
            bool(field.get("required")),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def name_key(field: dict) -> tuple[str, str]:
    return ((field.get("table") or "").lower(), (field.get("name") or "").lower())


def _index(fields: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for field in fields:
        out.setdefault(str(field["field_id"]), field)
    return out


def _name_index(fields: list[dict]) -> dict[tuple[str, str], str]:
    """同一版本内同表同列名唯一（采信阶段已去重），所以名字只对应一个身份。"""
    out: dict[tuple[str, str], str] = {}
    for field in fields:
        out.setdefault(name_key(field), str(field["field_id"]))
    return out


def describe(field: dict | None) -> dict | None:
    if field is None:
        return None
    return {
        "field_id": str(field["field_id"]),
        "table": field.get("table") or "",
        "name": field.get("name") or "",
        "name_key": (field.get("name") or "").lower(),
        "type_raw": field.get("type_raw") or "",
        "required": bool(field.get("required")),
        "line": field.get("line"),
        "version": field.get("version") or "",
        "source": field.get("source") or "",
        "signature": field_signature(field),
    }


def _order_key(field: dict) -> tuple:
    return ((field.get("table") or "").lower(), int(field.get("line") or 0),
            (field.get("name") or "").lower(), str(field["field_id"]))


def _label(field: dict | None) -> str:
    if field is None:
        return "（删除）"
    return f"{field.get('table') or ''}.{field.get('name') or ''}"


def conflict_key(reason: str, table: str, field_ids: list[str]) -> str:
    seed = "|".join([reason, (table or "").lower(), *sorted(str(x) for x in field_ids)])
    return "cf-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _differs(field: dict | None, other: dict | None) -> bool:
    if field is None and other is None:
        return False
    if field is None or other is None:
        return True
    return field_signature(field) != field_signature(other)


class _Groups:
    """并查集：同身份冲突与同名异身份都并成一组，整组选边。"""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.reason: dict[str, str] = {}
        self.kind: dict[str, str] = {}

    def add(self, field_id: str, reason: str, kind: str) -> None:
        self.parent.setdefault(field_id, field_id)
        self._raise(field_id, reason, kind)

    def find(self, field_id: str) -> str:
        self.parent.setdefault(field_id, field_id)
        root = field_id
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[field_id] != root:
            self.parent[field_id], field_id = root, self.parent[field_id]
        return root

    def union(self, left_id: str, right_id: str, reason: str, kind: str) -> None:
        left_root = self.find(left_id)
        right_root = self.find(right_id)
        inherit = None
        if left_root != right_root:
            # 固定取字典序小的为根，保证合并结果与输入顺序无关
            root, other = (left_root, right_root) if left_root < right_root else (
                right_root, left_root)
            inherit = (self.reason.get(other), self.kind.get(other))
            self.parent[other] = root
            self.reason.pop(other, None)
            self.kind.pop(other, None)
            left_root = root
        self._raise(left_root, reason, kind)
        if inherit and inherit[0]:
            self._raise(left_root, inherit[0], inherit[1] or kind)

    def _raise(self, root: str, reason: str, kind: str) -> None:
        current = self.reason.get(root)
        if current is None or _reason_rank(reason) < _reason_rank(current):
            self.reason[root] = reason
            self.kind[root] = kind

    def members(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for field_id in sorted(self.parent):
            out.setdefault(self.find(field_id), []).append(field_id)
        return out

    def groups(self) -> list[tuple[list[str], str, str]]:
        return [(ids, self.reason.get(root, "MERGE_DOUBLE_EDIT"),
                 self.kind.get(root, "double-edit"))
                for root, ids in self.members().items()]


_REASON_ORDER = {"MERGE_NAME_TAKEOVER": 0, "MERGE_DELETE_VS_EDIT": 1,
                 "MERGE_DOUBLE_EDIT": 2}


def _reason_rank(reason: str) -> int:
    return _REASON_ORDER.get(reason, 9)


def merge(base_fields: list[dict], left_fields: list[dict], right_fields: list[dict],
          left_label: str = "left", right_label: str = "right") -> dict:
    """三方合并，返回合成字段、逐身份来源、冲突组与统计。"""
    base = _index(base_fields)
    left = _index(left_fields)
    right = _index(right_fields)
    base_names = _name_index(base_fields)
    left_names = _name_index(left_fields)
    right_names = _name_index(right_fields)

    rows: dict[str, dict] = {}
    candidates: dict[str, dict] = {}
    actions: dict[str, str] = {}
    groups = _Groups()
    identity_issue: dict[str, tuple[str, str]] = {}

    def row_for(field_id: str) -> dict:
        return rows.setdefault(field_id, {
            "field_id": field_id,
            "base": describe(base.get(field_id)),
            "left": describe(left.get(field_id)),
            "right": describe(right.get(field_id)),
            "kind": "unchanged",
            "reason": "MERGE_CLEAN",
            "conflict": None,
            "deleted_by": None,
        })

    # ---- 1. 按身份判每一处改动 ----
    for field_id in sorted(set(base) | set(left) | set(right)):
        b, l, r = base.get(field_id), left.get(field_id), right.get(field_id)
        row = row_for(field_id)
        left_changed = _differs(l, b)
        right_changed = _differs(r, b)

        if b is None and l is not None and r is not None:
            if field_signature(l) == field_signature(r):
                candidates[field_id] = l
                actions[field_id] = "both"
                row.update({"kind": "both-added", "reason": "MERGE_KEEP_BOTH"})
            else:
                identity_issue[field_id] = ("MERGE_DOUBLE_EDIT", "double-edit")
        elif b is None and (l is not None or r is not None):
            candidates[field_id] = l if l is not None else r
            actions[field_id] = "left" if l is not None else "right"
            row.update({"kind": "added", "reason": "MERGE_CLEAN"})
        elif b is not None and l is None and r is None:
            row.update({"kind": "deleted", "reason": "MERGE_CLEAN",
                        "deleted_by": "both"})
        elif b is not None and (l is None or r is None):
            survivor = l if l is not None else r
            if _differs(survivor, b):
                identity_issue[field_id] = ("MERGE_DELETE_VS_EDIT", "delete-vs-edit")
            else:
                row.update({"kind": "deleted", "reason": "MERGE_CLEAN",
                            "deleted_by": "right" if l is not None else "left"})
        else:  # 基线存在、两边都还在
            if not left_changed and not right_changed:
                candidates[field_id] = b
                actions[field_id] = "base"
                row.update({"kind": "unchanged", "reason": "MERGE_CLEAN"})
            elif left_changed and not right_changed:
                candidates[field_id] = l
                actions[field_id] = "left"
                row.update({"kind": "left-only", "reason": "MERGE_CLEAN"})
            elif right_changed and not left_changed:
                candidates[field_id] = r
                actions[field_id] = "right"
                row.update({"kind": "right-only", "reason": "MERGE_CLEAN"})
            elif field_signature(l) == field_signature(r):
                candidates[field_id] = l
                actions[field_id] = "both"
                row.update({"kind": "both-same", "reason": "MERGE_KEEP_BOTH"})
            else:
                identity_issue[field_id] = ("MERGE_DOUBLE_EDIT", "double-edit")

    for field_id, (reason, kind) in identity_issue.items():
        groups.add(field_id, reason, kind)

    # ---- 2. 同名异身份：一个名字在两边挂到不同身份上，并成一组 ----
    for key in sorted(set(left_names) | set(right_names)):
        left_id = left_names.get(key)
        right_id = right_names.get(key)
        if left_id is None or right_id is None or left_id == right_id:
            continue
        groups.union(left_id, right_id, "MERGE_NAME_TAKEOVER", "name-owner")

    # ---- 2b. 基线里的名字换了主人：旧身份改名/被删，新身份占了这个名字 ----
    # 下游按名字找的是旧身份，不能悄悄绑到新身份上，留待整条选边。
    for key, base_id in sorted(base_names.items()):
        claimed = [field_id for field_id, field in candidates.items()
                   if name_key(field) == key]
        if not claimed or (base_id in candidates
                           and name_key(candidates[base_id]) == key):
            continue
        for field_id in claimed:
            if field_id == base_id:
                continue
            groups.union(base_id, field_id, "MERGE_NAME_TAKEOVER", "name-rebind")

    # ---- 3. 冲突组：整组按一边取 ----
    conflicts: list[dict] = []
    blocked: set[str] = set()
    for ids, reason, kind in groups.groups():
        table = ((left.get(ids[0]) or right.get(ids[0]) or base.get(ids[0])
                  or {}).get("table") or "")
        left_fields = [left[field_id] for field_id in ids if left.get(field_id)]
        right_fields = [right[field_id] for field_id in ids if right.get(field_id)]
        conflict = {
            "key": conflict_key(reason, table, ids),
            "kind": kind,
            "reason": reason,
            "table": table,
            "field_ids": ids,
            "left": [describe(field) for field in left_fields],
            "right": [describe(field) for field in right_fields],
            "left_label": " / ".join(_label(field) for field in left_fields) or "（删除）",
            "right_label": " / ".join(_label(field) for field in right_fields) or "（删除）",
            "options": list(CHOICES),
            "labels": {"left": left_label, "right": right_label},
        }
        conflicts.append(conflict)
        for field_id in ids:
            blocked.add(field_id)
            candidates.pop(field_id, None)
            actions.pop(field_id, None)
            row_for(field_id).update({"kind": "conflict", "reason": reason,
                                      "conflict": conflict["key"]})

    conflicts.sort(key=lambda item: (item["table"], item["key"]))

    fields = [_materialize(field, actions[field_id], left_label, right_label)
              for field_id, field in candidates.items()]
    fields.sort(key=_order_key)
    duplicates = _duplicates(fields)
    ordered_rows = sorted(rows.values(), key=lambda item: (
        (item.get("base") or item.get("left") or item.get("right") or {}).get("table") or "",
        item["field_id"],
    ))
    return {
        "fields": fields,
        "rows": ordered_rows,
        "conflicts": conflicts,
        "duplicates": duplicates,
        "stats": _stats(base_fields, left_fields, right_fields, fields, conflicts),
    }


def _duplicates(fields: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for field in fields:
        groups.setdefault(name_key(field), []).append(field)
    out: list[dict] = []
    for key, items in sorted(groups.items()):
        ids = sorted(str(item["field_id"]) for item in items)
        if len(ids) < 2:
            continue
        out.append({"table": items[0].get("table") or "",
                    "name": items[0].get("name") or "",
                    "field_ids": ids, "reason": "MERGE_DUPLICATE_NAME"})
    return out


def _stats(base_fields: list[dict], left_fields: list[dict], right_fields: list[dict],
           fields: list[dict], conflicts: list[dict]) -> dict:
    return {
        "base_fields": len(base_fields),
        "left_fields": len(left_fields),
        "right_fields": len(right_fields),
        "merged_fields": len(fields),
        "conflicts": len(conflicts),
        "from_left": sum(1 for field in fields if field["sources"]["picked"] == "left"),
        "from_right": sum(1 for field in fields if field["sources"]["picked"] == "right"),
        "from_both": sum(1 for field in fields if field["sources"]["picked"] == "both"),
        "from_base": sum(1 for field in fields if field["sources"]["picked"] == "base"),
    }


def _materialize(field: dict, side: str, left_label: str, right_label: str,
                 action: str = "kept") -> dict:
    item = dict(field)
    item["name_key"] = (item.get("name") or "").lower()
    item["sources"] = {
        "picked": side,
        "action": action,
        "source": field.get("source") or "",
        "version": field.get("version") or "",
        "labels": {"left": left_label, "right": right_label},
    }
    return item


def _from_description(desc: dict) -> dict:
    return {
        "field_id": str(desc["field_id"]),
        "table": desc.get("table") or "",
        "name": desc.get("name") or "",
        "name_key": (desc.get("name") or "").lower(),
        "type_raw": desc.get("type_raw") or "",
        "required": bool(desc.get("required")),
        "line": desc.get("line"),
        "version": desc.get("version") or "",
        "source": desc.get("source") or "",
    }


def apply_choices(merged: dict, choices: dict[str, str]) -> dict:
    """把整组选边套进合成结果，返回新结果与未裁决 / 非法 / 重名清单。

    choices: ``{conflict_key: "take-left" | "take-right"}``。
    入参不被修改；未命中的选择记为非法；同一字段被两组以不同方向选走记非法。
    """
    conflicts = [dict(item) for item in merged.get("conflicts") or []]
    by_key = {item["key"]: item for item in conflicts}
    fields = [dict(field) for field in merged.get("fields") or []]
    resolved: list[dict] = []
    invalid: list[dict] = []
    used: set[str] = set()
    placed: dict[str, str] = {}

    for key, choice in sorted((choices or {}).items()):
        conflict = by_key.get(key)
        if conflict is None:
            invalid.append({"key": key, "reason": "MERGE_DECISION_INVALID",
                            "detail": "没有这个冲突"})
            continue
        if choice not in CHOICES:
            invalid.append({"key": key, "reason": "MERGE_DECISION_INVALID",
                            "detail": f"未知裁决 {choice!r}"})
            continue
        side = "left" if choice == TAKE_LEFT else "right"
        ids = {str(value) for value in conflict["field_ids"]}
        clash = sorted(field_id for field_id in ids
                       if field_id in placed and placed[field_id] != side)
        if clash:
            invalid.append({"key": key, "reason": "MERGE_DECISION_INVALID",
                            "detail": f"与已落地的选边互相矛盾：{','.join(clash)}"})
            continue
        used.add(key)
        fields = [field for field in fields if str(field["field_id"]) not in ids]
        picked = conflict.get(side) or []
        # 按行序还原，组内顺序稳定
        picked = sorted(picked, key=lambda desc: (desc.get("line") or 0,
                                                  desc.get("name") or ""))
        labels = conflict.get("labels") or {}
        left_label = labels.get("left") or "left"
        right_label = labels.get("right") or "right"
        for desc in picked:
            item = _materialize(_from_description(desc), side, left_label, right_label)
            item["sources"]["action"] = "resolved"
            fields.append(item)
            placed[str(desc["field_id"])] = side
        resolved.append({
            "key": key, "choice": choice,
            "outcome": "chosen" if picked else "deleted",
            "field_ids": sorted(ids),
            "fields": [{"field_id": desc["field_id"], "table": desc.get("table"),
                        "name": desc.get("name")} for desc in picked],
        })

    fields.sort(key=_order_key)
    remaining = [item for item in conflicts if item["key"] not in used]
    duplicates = _duplicates(fields)
    return {
        "fields": fields,
        "conflicts_remaining": remaining,
        "unresolved": [
            {"key": item["key"], "kind": item["kind"], "table": item["table"],
             "reason": item["reason"], "field_ids": item["field_ids"],
             "left_label": item["left_label"], "right_label": item["right_label"]}
            for item in remaining
        ],
        "resolved": resolved,
        "invalid": invalid,
        "duplicates": duplicates,
        "blocked": bool(remaining) or bool(invalid) or bool(duplicates),
    }


def blocked_reason(result: dict) -> str | None:
    """发布前的统一闸门：未裁决、非法裁决、合成后重名，任一命中即拒。"""
    if result.get("duplicates"):
        return "MERGE_DUPLICATE_NAME"
    if result.get("invalid"):
        return "MERGE_DECISION_INVALID"
    if result.get("unresolved") or result.get("conflicts_remaining"):
        return "MERGE_UNRESOLVED"
    return None
