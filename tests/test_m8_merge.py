"""M8 分支合并：按共同基线三方合并、整条选边、发布与恢复。"""

from __future__ import annotations

from app import store
from app.modules.m2_types import service as types
from app.modules.m8_merge import merge as merge_mod

BASE = "merge_base.sql"
LEFT = "merge_left.sql"
RIGHT = "merge_right.sql"


def _fields(rows: list[dict]) -> list[dict]:
    return types.normalize_fields([
        {
            "field_id": row["field_id"],
            "table": row.get("table") or "dim",
            "name": row["name"],
            "type_raw": row.get("type") or "bigint",
            "required": bool(row.get("required", True)),
            "line": index,
            "version": "v",
            "source": "fixture",
        }
        for index, row in enumerate(rows, start=1)
    ])


def _key(merged: dict, kind: str) -> str:
    return next(item["key"] for item in merged["conflicts"] if item["kind"] == kind)


# ---------- 纯规则 ----------

def test_independent_changes_are_all_kept():
    """一边改名、一边换类型、两边各加字段：合成保留全部独立改动。"""
    base = _fields([{"field_id": "1", "name": "a"},
                    {"field_id": "2", "name": "b", "type": "varchar(10)"},
                    {"field_id": "3", "name": "c"}])
    left = _fields([{"field_id": "1", "name": "a"},
                    {"field_id": "2", "name": "b", "type": "varchar(10)"},
                    {"field_id": "3", "name": "c"},
                    {"field_id": "4", "name": "from_left"}])
    right = _fields([{"field_id": "1", "name": "a_renamed"},
                     {"field_id": "2", "name": "b", "type": "varchar(20)",
                      "required": False},
                     {"field_id": "3", "name": "c"},
                     {"field_id": "5", "name": "from_right"}])
    merged = merge_mod.merge(base, left, right)
    assert merged["conflicts"] == []
    by_id = {field["field_id"]: field for field in merged["fields"]}
    assert by_id["1"]["name"] == "a_renamed"
    assert by_id["2"]["type_raw"] == "varchar(20)"
    assert by_id["2"]["required"] is False
    assert by_id["4"]["sources"]["picked"] == "left"
    assert by_id["5"]["sources"]["picked"] == "right"
    assert by_id["3"]["sources"]["picked"] == "base"


def test_same_field_two_edits_must_choose_one_side_not_splice():
    """同一字段两边各改一个属性：冲突整条选边，不能拼成第三种规格。"""
    base = _fields([{"field_id": "1", "name": "amount", "type": "decimal(18,2)"}])
    left = _fields([{"field_id": "1", "name": "amount", "type": "decimal(20,4)"}])
    right = _fields([{"field_id": "1", "name": "amount", "type": "decimal(18,2)",
                      "required": False}])
    merged = merge_mod.merge(base, left, right)
    assert len(merged["conflicts"]) == 1
    conflict = merged["conflicts"][0]
    assert conflict["reason"] == "MERGE_DOUBLE_EDIT"
    assert merged["fields"] == []

    chosen = merge_mod.apply_choices(merged, {conflict["key"]: "take-left"})
    assert chosen["blocked"] is False
    assert len(chosen["fields"]) == 1
    # 选左边就整条用左边，右边放宽的必填不掺进来
    assert chosen["fields"][0]["type_raw"] == "decimal(20,4)"
    assert chosen["fields"][0]["required"] is True
    assert chosen["fields"][0]["sources"]["picked"] == "left"


def test_delete_vs_edit_is_conflict_not_silent_keep():
    """一边删、另一边改：不能静默保留，也不能静默删除，必须整条选边。"""
    base = _fields([{"field_id": "1", "name": "legacy", "type": "varchar(8)"}])
    left = _fields([])
    right = _fields([{"field_id": "1", "name": "legacy", "type": "varchar(16)"}])
    merged = merge_mod.merge(base, left, right)
    assert merged["conflicts"][0]["kind"] == "delete-vs-edit"
    assert merged["conflicts"][0]["reason"] == "MERGE_DELETE_VS_EDIT"

    delete_side = merge_mod.apply_choices(merged, {merged["conflicts"][0]["key"]: "take-left"})
    assert delete_side["fields"] == []
    keep_side = merge_mod.apply_choices(merged, {merged["conflicts"][0]["key"]: "take-right"})
    assert keep_side["fields"][0]["type_raw"] == "varchar(16)"


def test_same_name_different_identity_needs_whole_field_choice():
    """同名异身份：两边各自指向不同身份，留待整条选边，不合成一个字段。"""
    base = _fields([{"field_id": "1", "name": "status", "type": "varchar(16)"}])
    left = _fields([{"field_id": "1", "name": "status", "type": "varchar(32)"}])
    right = _fields([{"field_id": "2", "name": "status", "type": "varchar(16)"}])
    merged = merge_mod.merge(base, left, right)
    assert merged["fields"] == []
    assert merged["conflicts"][0]["reason"] == "MERGE_NAME_TAKEOVER"
    assert set(merged["conflicts"][0]["field_ids"]) == {"1", "2"}

    chosen = merge_mod.apply_choices(merged, {merged["conflicts"][0]["key"]: "take-right"})
    assert [field["field_id"] for field in chosen["fields"]] == ["2"]


def test_identical_edits_on_both_sides_are_taken():
    base = _fields([{"field_id": "1", "name": "a"}])
    left = _fields([{"field_id": "1", "name": "a", "type": "varchar(24)"}])
    right = _fields([{"field_id": "1", "name": "a", "type": "varchar(24)"}])
    merged = merge_mod.merge(base, left, right)
    assert merged["conflicts"] == []
    assert merged["fields"][0]["sources"]["picked"] == "both"


def test_two_renames_to_same_new_name_form_one_choice():
    """两个身份各自改名撞到同一个新名字：同一个名字只能整组选边一次。"""
    base = _fields([{"field_id": "1", "name": "a"}, {"field_id": "2", "name": "b"}])
    left = _fields([{"field_id": "1", "name": "z"}, {"field_id": "2", "name": "b"}])
    right = _fields([{"field_id": "1", "name": "a"}, {"field_id": "2", "name": "z"}])
    merged = merge_mod.merge(base, left, right)
    assert len(merged["conflicts"]) == 1
    assert merged["conflicts"][0]["reason"] == "MERGE_NAME_TAKEOVER"
    assert set(merged["conflicts"][0]["field_ids"]) == {"1", "2"}

    for side in ("take-left", "take-right"):
        applied = merge_mod.apply_choices(merged, {merged["conflicts"][0]["key"]: side})
        assert applied["blocked"] is False
        assert applied["duplicates"] == []
        assert len(applied["fields"]) == 2


def test_post_merge_duplicate_is_refused_by_gate():
    """合成后同表重名是发布硬闸门：不允许靠选边消化，直接拒。"""
    blocked = {"fields": [], "conflicts_remaining": [], "unresolved": [],
               "invalid": [], "resolved": [],
               "duplicates": [{"table": "dim", "name": "a", "field_ids": ["1", "2"],
                               "reason": "MERGE_DUPLICATE_NAME"}]}
    assert merge_mod.blocked_reason(blocked) == "MERGE_DUPLICATE_NAME"

    clean = {"fields": [], "conflicts_remaining": [], "unresolved": [],
             "invalid": [], "resolved": [], "duplicates": []}
    assert merge_mod.blocked_reason(clean) is None


def test_duplicate_detection_catches_same_name_twice():
    fields = _fields([{"field_id": "1", "name": "dup"},
                      {"field_id": "2", "name": "dup"}])
    assert merge_mod._duplicates(fields)[0]["field_ids"] == ["1", "2"]


def test_unresolved_merge_is_blocked():
    base = _fields([{"field_id": "1", "name": "a"}])
    merged = merge_mod.merge(base, _fields([{"field_id": "1", "name": "x"}]),
                             _fields([{"field_id": "1", "name": "y"}]))
    applied = merge_mod.apply_choices(merged, {})
    assert applied["blocked"] is True
    assert merge_mod.blocked_reason(applied) == "MERGE_UNRESOLVED"


# ---------- 会话与发布 ----------

def _open(client):
    return client.post("/api/merges", json={
        "base_file": BASE, "left_file": LEFT, "right_file": RIGHT,
    }).json()


def _choices(session: dict, side: str = "take-left") -> dict:
    return {item["key"]: side for item in session["conflicts"]}


def test_sample_session_reports_three_whole_record_conflicts(client):
    session = _open(client)
    kinds = {item["kind"] for item in session["conflicts"]}
    assert kinds == {"delete-vs-edit", "double-edit", "name-owner"}
    assert session["selection_revision"] == 0
    assert session["stats"]["merged_fields"] >= 1

    preview = client.get(f"/api/merges/{session['merge_id']}/preview").json()
    assert preview["result"]["blocked"] is True
    assert len(preview["result"]["unresolved"]) == 3


def test_reopening_same_three_inputs_reuses_session(client):
    first = _open(client)
    second = _open(client)
    assert first["merge_id"] == second["merge_id"]


def test_publish_without_resolution_is_refused(client):
    session = _open(client)
    response = client.post(f"/api/merges/{session['merge_id']}/publish", json={})
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "MERGE_BLOCKED"
    assert body["detail"]["reason"] == "MERGE_UNRESOLVED"
    assert store.count_runs() == 1


def test_resolve_then_publish_creates_new_batch_with_both_sources(client):
    session = _open(client)
    merge_id = session["merge_id"]
    saved = client.post(f"/api/merges/{merge_id}/selections",
                        json={"selections": _choices(session, "take-left"),
                              "expected_revision": 0})
    assert saved.status_code == 200
    revision = saved.json()["revision"]
    assert revision == 1

    published = client.post(f"/api/merges/{merge_id}/publish",
                            json={"expected_revision": revision})
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published"
    assert body["reused"] is False
    assert body["version_seq"] == 2
    assert body["balanced"]["all"] is True

    run = store.run_by_id(body["run_id"])
    sources = run["merge"]["sources"]
    assert sources["base"]["ref"] == BASE
    assert sources["left"]["ref"] == LEFT
    assert sources["right"]["ref"] == RIGHT
    assert all(field["sources"].get("picked") for field in run["merge"]["fields"])

    # 再发布一次：同一合成内容复用批次，不重复生成
    again = client.post(f"/api/merges/{merge_id}/publish", json={}).json()
    assert again["run_id"] == body["run_id"]
    assert again["reused"] is True
    assert store.count_runs() == 2


def test_branch_change_invalidates_saved_selections(client, monkeypatch):
    session = _open(client)
    merge_id = session["merge_id"]
    client.post(f"/api/merges/{merge_id}/selections",
                json={"selections": _choices(session), "expected_revision": 0})

    from app import manifest
    from app.modules.m8_merge import service as merge_service

    real_digest = merge_service._digest

    def shifted(path):
        digest = real_digest(path)
        if path.name == LEFT:
            return manifest.sha256_bytes(b"branch-moved")
        return digest

    monkeypatch.setattr(merge_service, "_digest", shifted)
    response = client.post(f"/api/merges/{merge_id}/publish", json={})
    assert response.status_code == 409
    assert response.json()["error"] == "MERGE_STALE"
    assert store.count_runs() == 1


def test_concurrent_selection_write_loses_to_newer_revision(client):
    session = _open(client)
    merge_id = session["merge_id"]
    choices = _choices(session)
    first = client.post(f"/api/merges/{merge_id}/selections",
                        json={"selections": choices, "expected_revision": 0})
    assert first.status_code == 200
    stale = client.post(f"/api/merges/{merge_id}/selections",
                        json={"selections": choices, "expected_revision": 0})
    assert stale.status_code == 409
    assert stale.json()["error"] == "MERGE_STALE"


def test_publish_rejects_stale_revision(client):
    session = _open(client)
    response = client.post(f"/api/merges/{session['merge_id']}/publish",
                           json={"expected_revision": 5})
    assert response.status_code == 409
    assert response.json()["error"] == "MERGE_STALE"


def test_invalid_choice_is_rejected(client):
    session = _open(client)
    response = client.post(f"/api/merges/{session['merge_id']}/selections",
                           json={"selections": {"cf-nope": "take-left"}})
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "MERGE_DECISION_INVALID"


def test_write_failure_then_recover_does_not_duplicate_batch(client, monkeypatch):
    session = _open(client)
    merge_id = session["merge_id"]
    revision = client.post(f"/api/merges/{merge_id}/selections",
                           json={"selections": _choices(session),
                                 "expected_revision": 0}).json()["revision"]

    from app import store as store_mod

    real_insert = store_mod.insert_run_if_absent
    state = {"failed": False}

    def boom_once(run):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("disk full")
        return real_insert(run)

    monkeypatch.setattr(store_mod, "insert_run_if_absent", boom_once)
    failed = client.post(f"/api/merges/{merge_id}/publish",
                         json={"expected_revision": revision})
    assert failed.status_code == 409
    assert failed.json()["detail"]["reason"] == "MERGE_WRITE_FAILED"
    assert store.count_runs() == 1

    stored = store.load_merge_session(merge_id)
    assert stored["pending"] is not None
    assert stored["last_error"]

    recovered = client.post(f"/api/merges/{merge_id}/recover", json={})
    assert recovered.status_code == 200, recovered.text
    body = recovered.json()
    assert body["reused"] is False
    assert store.count_runs() == 2

    again = client.post(f"/api/merges/{merge_id}/recover", json={}).json()
    assert again["run_id"] == body["run_id"]
    assert again["reused"] is True
    assert store.count_runs() == 2


def test_concurrent_publish_claim_rejects_second_writer(client):
    """一个发布已落未完成标记时，另一个并发发布不能套旧基线再发一次。"""
    session = _open(client)
    merge_id = session["merge_id"]
    revision = client.post(f"/api/merges/{merge_id}/selections",
                           json={"selections": _choices(session),
                                 "expected_revision": 0}).json()["revision"]
    claimed = store.claim_merge_publish(merge_id, revision, {"batch_key": "x"},
                                        "2026-11-17T00:00:00+00:00")
    assert claimed is True
    second = store.claim_merge_publish(merge_id, revision, {"batch_key": "x"},
                                       "2026-11-17T00:00:01+00:00")
    assert second is False


def test_merge_page_lists_sessions(client):
    _open(client)
    page = client.get("/merge")
    assert page.status_code == 200
    assert "分支合并" in page.text
    assert "mg-" in page.text


def test_unknown_merge_is_404(client):
    assert client.get("/api/merges/mg-missing").status_code == 404
    assert client.post("/api/merges/mg-missing/publish", json={}).status_code == 404
