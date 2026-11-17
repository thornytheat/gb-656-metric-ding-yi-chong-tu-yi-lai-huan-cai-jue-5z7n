"""合并会话：建会话、记裁决、发布新批次。

一次合并的生命周期：

1. ``open_session`` 读三份规格（共同基线 + 左右两组改动），按身份合并，
   把冲突与可选边全部落到 ``merge_sessions``。会话记录三份文件的摘要，
   分支内容一旦变动，会话里的旧选择就作废（``MERGE_STALE``）。
2. ``save_selections`` 记录页面上的整组选边。每次写入都做一次摘要复核，
   并用版本号原子认领，过期基线一律拒绝。
3. ``publish`` 套用裁决 → 再体检（未裁决 / 非法 / 合成后重名都拒）→ 把合成
   字段落成一份批次 → 复用批次流水线发布。发布前先落「未完成」标记，
   成功后清掉；写入失败时标记保留，重试能接着恢复，且批次键保证不重复生成。
"""

from __future__ import annotations

import hashlib
import json

from app import config, errors, manifest as manifest_mod, store
from app.modules.m1_intake import service as intake
from app.modules.m2_types import service as types
from app.modules.m3_compat import service as compat
from app.modules.m4_mapping import service as mapping
from app.modules.m5_impact import service as impact
from app.modules.m6_batches import guard, history
from app.modules.m6_batches import window as window_mod
from app.modules.m7_migration import service as migration
from app.modules.m8_merge import merge as merge_mod


def _digest(path) -> str:
    return manifest_mod.sha256_file(path)


def _ingest_prepared(rel_name: str) -> tuple[dict, str]:
    snapshot = types.prepare(intake.ingest(rel_name))
    return snapshot, _digest(config.resolve_sample(rel_name))


def _content_digest(base: dict, left: dict, right: dict) -> str:
    return manifest_mod.sha256_bytes(
        "|".join([base["source_sha256"], left["source_sha256"],
                  right["source_sha256"]]).encode("utf-8")
    )


def _merge_key(base_digest: str, left_digest: str, right_digest: str,
               ruleset: str) -> str:
    return manifest_mod.sha256_bytes(
        f"merge|{ruleset}|{base_digest}|{left_digest}|{right_digest}".encode("utf-8")
    )


def _json_safe(resolution: dict) -> dict:
    out = dict(resolution)
    out["by_field"] = {key: sorted(value)
                       for key, value in (resolution.get("by_field") or {}).items()}
    out["ref_counts"] = {f"{table}|{column}": count
                         for (table, column), count
                         in (resolution.get("ref_counts") or {}).items()}
    return out


# ---------- 会话 ----------

def open_session(base_file: str, left_file: str, right_file: str) -> dict:
    """读三份文件，建立（或复用）一次合并会话。"""
    base_snapshot, base_digest = _ingest_prepared(base_file)
    left_snapshot, left_digest = _ingest_prepared(left_file)
    right_snapshot, right_digest = _ingest_prepared(right_file)

    key = _merge_key(base_digest, left_digest, right_digest, config.RULESET_VERSION)
    existing = store.find_merge_session_by_key(key)
    if existing is not None:
        return existing

    merged = merge_mod.merge(base_snapshot["fields"], left_snapshot["fields"],
                             right_snapshot["fields"],
                             left_label=left_snapshot["version_label"],
                             right_label=right_snapshot["version_label"])
    now = config.now_utc().isoformat()
    merge_id = "mg-" + key[:12]
    session = {
        "merge_id": merge_id,
        "merge_key": key,
        "status": "open",
        "base_ref": base_file,
        "left_ref": left_file,
        "right_ref": right_file,
        "base_digest": base_digest,
        "left_digest": left_digest,
        "right_digest": right_digest,
        "content_digest": _content_digest(base_snapshot, left_snapshot, right_snapshot),
        "selections": {},
        "selection_revision": 0,
        "options": {"stats": merged["stats"]},
        "conflicts": merged["conflicts"],
        "pending": None,
        "published_run_id": None,
        "published_version_seq": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "labels": {
            "base": base_snapshot["version_label"],
            "left": left_snapshot["version_label"],
            "right": right_snapshot["version_label"],
        },
        "snapshots": {
            "base": intake.snapshot_summary(base_snapshot),
            "left": intake.snapshot_summary(left_snapshot),
            "right": intake.snapshot_summary(right_snapshot),
        },
        # 合成结果按需重算；这里存一份当时的口径供审计
        "merge": {"stats": merged["stats"]},
        "stats": merged["stats"],
    }
    store.save_merge_session(session)
    return session


def get_session(merge_id: str) -> dict:
    session = store.load_merge_session(merge_id)
    if session is None:
        raise errors.NotFound("没有这个合并会话", detail={"merge_id": merge_id})
    return session


def _recheck(session: dict) -> dict:
    """分支内容变动就让旧选择失效；返回最新的摘要。"""
    digests = {
        "base": _digest(config.resolve_sample(session["base_ref"])),
        "left": _digest(config.resolve_sample(session["left_ref"])),
        "right": _digest(config.resolve_sample(session["right_ref"])),
    }
    current = {
        "base_digest": session["base_digest"],
        "left_digest": session["left_digest"],
        "right_digest": session["right_digest"],
    }
    stale = [side for side in ("base", "left", "right")
             if digests[side] != current[f"{side}_digest"]]
    if stale:
        raise errors.MergeStale(
            "分支内容已变，旧选择失效，请重新建会话",
            detail={"reason": "MERGE_STALE", "changed": stale,
                    "merge_id": session["merge_id"],
                    "selections": {"revision": session["selection_revision"]}},
        )
    return digests


def _current_merged(session: dict) -> dict:
    """按当前分支文件重算合并，保证裁决与最新内容一致。"""
    base_snapshot, _ = _ingest_prepared(session["base_ref"])
    left_snapshot, _ = _ingest_prepared(session["left_ref"])
    right_snapshot, _ = _ingest_prepared(session["right_ref"])
    return merge_mod.merge(base_snapshot["fields"], left_snapshot["fields"],
                           right_snapshot["fields"],
                           left_label=left_snapshot["version_label"],
                           right_label=right_snapshot["version_label"]), {
        "base": base_snapshot, "left": left_snapshot, "right": right_snapshot,
    }


def save_selections(merge_id: str, selections: dict, expected_revision=None) -> dict:
    """记录整条选边。内容变动则拒绝；并发写入的版本号必须是当前版本。"""
    session = get_session(merge_id)
    _recheck(session)
    if expected_revision is not None:
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError):
            raise errors.BadInput("expected_revision 必须是整数",
                                  detail={"value": repr(expected_revision)})
        if expected != int(session["selection_revision"]):
            raise errors.MergeStale(
                "会话已被别的写入推进，请刷新后重试",
                detail={"reason": "MERGE_STALE",
                        "expected_revision": expected,
                        "current_revision": session["selection_revision"]},
            )
    if not isinstance(selections, dict) or not selections:
        raise errors.BadInput("selections 必须是非空对象", detail={"field": "selections"})
    if len(selections) > 200:
        raise errors.BadInput("一次最多提交 200 条裁决", detail={"count": len(selections)})

    merged, _ = _current_merged(session)
    applied = merge_mod.apply_choices(merged, selections)
    if applied["invalid"]:
        raise errors.BadInput("裁决值不合法",
                              detail={"reason": "MERGE_DECISION_INVALID",
                                      "invalid": applied["invalid"]})

    updated = store.update_merge_selections(
        merge_id, selections, session["selection_revision"],
        config.now_utc().isoformat(),
    )
    if updated is None:
        # 版本号被别的写入抢先推进，旧基线的这次写入作废
        raise errors.MergeStale(
            "会话已被别的写入推进，请刷新后重试",
            detail={"reason": "MERGE_STALE",
                    "merge_id": merge_id,
                    "expected_revision": session["selection_revision"]},
        )
    updated["labels"] = session.get("labels") or {}
    updated["snapshots"] = session.get("snapshots") or {}
    updated["merge"] = session.get("merge") or {}
    return updated


def preview(merge_id: str, selections: dict | None = None) -> dict:
    session = get_session(merge_id)
    merged, _ = _current_merged(session)
    chosen = dict(session.get("selections") or {})
    if selections:
        chosen.update(selections)
    applied = merge_mod.apply_choices(merged, chosen)
    return {
        "merge_id": session["merge_id"],
        "status": session["status"],
        "revision": session["selection_revision"],
        "selections": chosen,
        "pending": session.get("pending"),
        "conflicts": merged["conflicts"],
        "result": applied,
        "stats": merged["stats"],
        "labels": session.get("labels") or {},
    }


# ---------- 发布 ----------

def publish(merge_id: str, expected_revision=None, effective_from: str | None = None,
            effective_to: str | None = None, base_version_seq=None,
            recovering: bool = False) -> dict:
    """套用已存裁决，发布成一个新批次；同一内容重试不重复生成批次。"""
    session = get_session(merge_id)
    digests = _recheck(session)

    if expected_revision is not None:
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError):
            raise errors.BadInput("expected_revision 必须是整数",
                                  detail={"value": repr(expected_revision)})
        if expected != int(session["selection_revision"]):
            raise errors.MergeStale(
                "会话已被别的写入推进，请刷新后重试",
                detail={"reason": "MERGE_STALE",
                        "expected_revision": expected,
                        "current_revision": session["selection_revision"]},
            )

    merged, snapshots = _current_merged(session)
    applied = merge_mod.apply_choices(merged, session.get("selections") or {})
    blocked = merge_mod.blocked_reason(applied)
    if blocked:
        raise errors.MergeBlocked(
            "合并还有未解决的问题，不能发布",
            detail={"reason": blocked,
                    "unresolved": applied["unresolved"],
                    "invalid": applied["invalid"],
                    "duplicates": applied["duplicates"]},
        )

    if base_version_seq is not None:
        guard.check_stale(base_version_seq, store.current_version_seq())
    fields = applied["fields"]
    if not fields:
        raise errors.MergeBlocked(
            "合成结果没有字段，不能发布",
            detail={"reason": "MERGE_UNRESOLVED"},
        )

    window = window_mod.normalize(effective_from, effective_to,
                                 snapshots["right"]["version_label"])
    merge_content = _merge_content_digest(fields)
    # 批次键只看真正的输入：三份摘要 + 生效区间 + 合成内容。
    # 同一内容重试（含发布失败后的恢复）必然命中同一个键，不会重复生成批次；
    # 选择改了但合成内容一样，也仍然复用同一批次。
    batch_key = manifest_mod.batch_key(
        {f"merge-{key}": value for key, value in sorted(digests.items())},
        {"window": f"{window['from']}..{window['to'] or ''}",
         "merge_content": merge_content,
         "merge_key": session["merge_key"]},
        config.RULESET_VERSION,
    )
    params = {
        "merge_id": session["merge_id"],
        "merge_key": session["merge_key"],
        "merge_content": merge_content,
    }

    existing = store.find_run_by_key(batch_key)
    if existing is not None:
        # 只有上次写入失败留下了同键的「未完成」标记，才算恢复；普通重发只是复用
        was_pending = (session.get("pending") or {}).get("batch_key") == batch_key
        _record_publish(session, existing, recovered=was_pending)
        return _publish_body(session, existing, reused=True)

    # 先落「未完成」标记，并在同一步里校验版本号：并发发布拿到过期基线即拒
    pending = {
        "batch_key": batch_key,
        "content_digest": merge_content,
        "selection_revision": session["selection_revision"],
        "effective_from": window["from"],
        "effective_to": window["to"],
        "started_at": config.now_utc().isoformat(),
    }
    # 上一次写入失败留下的标记：同一批次键就沿用，接着恢复
    unfinished = (session.get("pending") or {}).get("batch_key") == batch_key
    claimed = store.claim_merge_publish(
        merge_id, session["selection_revision"], pending,
        config.now_utc().isoformat(), allow_pending=recovering or unfinished,
    )
    if not claimed:
        raise errors.MergeStale(
            "会话已被别的发布推进，拒绝过期基线的发布",
            detail={"reason": "MERGE_STALE", "merge_id": merge_id,
                    "expected_revision": session["selection_revision"]},
        )
    session["pending"] = pending
    session["updated_at"] = pending["started_at"]
    run = _build_run(session, snapshots, fields, applied, window, digests, params,
                     batch_key, merge_content)
    # 认领后到落库前，分支仍可能被改；写之前最后一次复核，过期就不写
    _recheck(session)
    try:
        stored = store.insert_run_if_absent(run)
    except Exception as exc:  # 写库失败：保留 pending，交给重试恢复
        message = f"{type(exc).__name__}: {exc}"
        store.mark_merge_failure(merge_id, message, config.now_utc().isoformat())
        raise errors.Conflict(
            "批次写入失败，可重试发布",
            detail={"reason": "MERGE_WRITE_FAILED", "merge_id": merge_id,
                    "batch_key": batch_key, "error": message},
        )

    _record_publish(session, stored, recovered=False)
    return _publish_body(session, stored, reused=False)


def _record_publish(session: dict, run: dict, recovered: bool) -> None:
    store.mark_merge_published(session["merge_id"], run["run_id"], run["version_seq"],
                               config.now_utc().isoformat(), recovered)
    session["status"] = "published"
    session["published_run_id"] = run["run_id"]
    session["published_version_seq"] = run["version_seq"]
    session["last_error"] = None
    session["pending"] = None
    session["recovered"] = bool(recovered)


def _publish_body(session: dict, run: dict, reused: bool) -> dict:
    return {
        "merge_id": session["merge_id"],
        "status": "published",
        "run_id": run["run_id"],
        "version_seq": run["version_seq"],
        "reused": bool(reused),
        "recovered": bool(session.get("recovered")),
        "selection_revision": session["selection_revision"],
        "sources": {
            "base": {"ref": session["base_ref"], "digest": session["base_digest"]},
            "left": {"ref": session["left_ref"], "digest": session["left_digest"]},
            "right": {"ref": session["right_ref"], "digest": session["right_digest"]},
        },
        "merged_fields": len((run.get("merge") or {}).get("fields") or []),
        "counts": (run.get("ledger") or {}).get("counts") or {},
        "balanced": (run.get("ledger") or {}).get("balanced") or {},
        "warnings": run.get("warnings") or [],
        "window": run.get("window") or {},
    }


def _merge_content_digest(fields: list[dict]) -> str:
    payload = [
        [field["field_id"], field["table"], field["name"], field["type_raw"],
         bool(field["required"])]
        for field in fields
    ]
    return manifest_mod.sha256_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    )


def _build_run(session: dict, snapshots: dict, fields: list[dict], applied: dict,
               window: dict, digests: dict, params: dict, batch_key: str,
               merge_content: str) -> dict:
    base_snapshot = snapshots["base"]
    left_snapshot = snapshots["left"]
    right_snapshot = snapshots["right"]

    # 合成字段要过一遍类型格，后续判定与导出都按统一结构走
    normalized = types.normalize_fields(fields)
    for field in normalized:
        field["version"] = f"merged:{left_snapshot['version_label']}+{right_snapshot['version_label']}"
        field["source"] = "merge"

    refs = mapping.load_refs(config.DEFAULT_REFS)
    ref_counts: dict[tuple[str, str], int] = {}
    for consumer in refs["consumers"]:
        for ref in consumer["refs"]:
            table, _, column = ref.rpartition(".")
            key = (table.lower(), column.lower())
            ref_counts[key] = ref_counts.get(key, 0) + 1

    diff = compat.diff_snapshots(base_snapshot, {"fields": normalized}, ref_counts)
    # 合并批次给的是「基线 → 合成」的两向判定，来源另存
    resolution = mapping.resolve(refs["consumers"], base_snapshot["fields"],
                                 normalized, {})
    mapping_table = mapping.build_mapping(base_snapshot["fields"], normalized, diff)
    impact_payload = impact.build(diff, refs["consumers"], resolution)
    ledger = migration.build_ledger(diff, refs["consumers"], resolution)

    warnings = guard.overlap_warnings(window, store.list_windows())
    warnings.append({
        "code": "MERGE_CLEAN",
        "detail": (f"合并 {left_snapshot['version_label']} + "
                   f"{right_snapshot['version_label']}，"
                   f"基于 {base_snapshot['version_label']}"),
    })
    created_at = config.now_utc().isoformat()
    input_sha = manifest_mod.sha256_bytes(
        "|".join(f"{role}:{digest}" for role, digest in sorted(digests.items())).encode()
    )
    merge_fields = [
        {"field_id": field["field_id"], "table": field["table"], "name": field["name"],
         "type_raw": field["type_raw"], "required": bool(field["required"]),
         "sources": field.get("sources") or {}}
        for field in normalized
    ]
    run = {
        "run_id": manifest_mod.run_id_for(batch_key),
        "batch_key": batch_key,
        "merge_id": session["merge_id"],
        "merge_key": session["merge_key"],
        "merge_content": merge_content,
        "merge": {
            "fields": merge_fields,
            "resolved": applied["resolved"],
            "sources": {
                "base": {"ref": session["base_ref"], "label": base_snapshot["version_label"],
                         "digest": digests["base"]},
                "left": {"ref": session["left_ref"], "label": left_snapshot["version_label"],
                         "digest": digests["left"]},
                "right": {"ref": session["right_ref"], "label": right_snapshot["version_label"],
                          "digest": digests["right"]},
            },
            "stats": {"merged_fields": len(merge_fields),
                      "resolved": len(applied["resolved"])},
        },
        "old_schema_id": base_snapshot["schema_id"],
        "new_schema_id": _schema_id_of(fields, left_snapshot["version_label"],
                                       right_snapshot["version_label"]),
        "version_label_old": base_snapshot["version_label"],
        "version_label_new": f"{left_snapshot['version_label']}+{right_snapshot['version_label']}",
        "input_sha256": input_sha,
        "digests": {f"merge-{role}": digest for role, digest in sorted(digests.items())},
        "input_names": {"base": session["base_ref"], "left": session["left_ref"],
                        "right": session["right_ref"]},
        "params": params,
        "ruleset": config.RULESET_VERSION,
        "created_at": created_at,
        "effective_from": window["from"],
        "effective_to": window["to"],
        "window": {key: value for key, value in window.items() if not key.endswith("_dt")},
        "fields_old": base_snapshot["fields"],
        "fields_new": normalized,
        "type_stats_old": base_snapshot["type_stats"],
        "type_stats_new": types.type_stats(normalized),
        "diff": diff["rows"],
        "buckets": diff["buckets"],
        "changes": diff["changes"],
        "verdicts": diff["verdicts"],
        "counts": diff["counts"],
        "name_clashes": diff["name_clashes"],
        "mapping": mapping_table,
        "refs": {"source": refs["source"], "sha256": refs["sha256"],
                 "registered_at": refs["registered_at"], "ref_total": refs["ref_total"],
                 "consumers": refs["consumers"]},
        "resolution": _json_safe(resolution),
        "impact": impact_payload,
        "ledger": ledger,
        "quarantine": ([dict(row, side="base") for row in base_snapshot["diagnostics"]]
                       + [dict(row, side="left") for row in left_snapshot["diagnostics"]]
                       + [dict(row, side="right") for row in right_snapshot["diagnostics"]]),
        "quarantine_old": base_snapshot["diagnostics"],
        "quarantine_new": left_snapshot["diagnostics"] + right_snapshot["diagnostics"],
        "warnings": warnings,
        "reused": False,
        # 序号由写入时在锁内分配，这里先占位，落库后回填
        "version_seq": 0,
    }
    run["summary"] = history.summary_of(run)
    return run


def _schema_id_of(fields: list[dict], left_label: str, right_label: str) -> str:
    canonical = json.dumps(
        {"version": f"merged:{left_label}+{right_label}",
         "fields": [[field["table"], field["field_id"], field["name"],
                     field["type_raw"], bool(field["required"])]
                    for field in fields]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return "sch-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def list_sessions(limit: int = 30) -> list[dict]:
    return store.list_merge_sessions(limit)


def recover(merge_id: str, effective_from: str | None = None,
            effective_to: str | None = None, base_version_seq=None) -> dict:
    """写入失败后的恢复：保留原选择，重跑一次发布；批次键保证不重复生成。

    恢复时把「未完成」标记里的区间原样带上，避免窗口变了导致批次键漂移。
    """
    session = get_session(merge_id)
    if session.get("status") == "published" and session.get("published_run_id"):
        run = store.run_by_id(session["published_run_id"])
        if run is not None:
            return _publish_body(session, run, reused=True)
    pending = session.get("pending") or {}
    from_value = effective_from or pending.get("effective_from")
    to_value = effective_to or pending.get("effective_to")
    return publish(merge_id, expected_revision=None,
                   effective_from=from_value, effective_to=to_value,
                   base_version_seq=base_version_seq, recovering=True)
