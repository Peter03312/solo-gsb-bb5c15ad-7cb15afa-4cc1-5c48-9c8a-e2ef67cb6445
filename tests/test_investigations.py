"""调查封存（疑似交叉接触）端到端测试。

封存传播与过敏原内容无关：沿加工投入链 + 同设备相邻加工残留链（有效清洗
截断）实时传播；封存批次不得签发标签或作为新加工投入；投入纠正、清洗登记/
撤销、调查登记/解除后同步重算，并沿用游标事件流发布实际变化。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="allergen-hold-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    for t in ["material_declarations", "material_batches", "processing_inputs",
              "processing_outputs", "processings", "cleanings", "labels",
              "trace_events", "investigations"]:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'trace_events'")
    conn.close()
    with TestClient(app) as c:
        yield c


def material(c, batch_id, allergens=None, pending=False,
             declared_at="2026-01-01T00:00:00Z"):
    return c.post("/materials", json={
        "batch_id": batch_id, "allergens": allergens or [],
        "pending": pending, "declared_at": declared_at})


def processing(c, pid, eq, start, end, inputs, outputs):
    return c.post("/processings", json={
        "processing_id": pid, "equipment_id": eq,
        "start_utc": start, "end_utc": end,
        "inputs": inputs, "outputs": outputs})


def investigate(c, iid, batch_id, reason="疑似交叉接触"):
    return c.post("/investigations", json={
        "investigation_id": iid, "batch_id": batch_id, "reason": reason})


def lift(c, iid):
    return c.post(f"/investigations/{iid}/lift")


def events(c, cursor=0, **params):
    r = c.get("/changes/events", params={"cursor": cursor, **params})
    assert r.status_code == 200, r.text
    return r.json()


def cursor(c):
    return c.get("/changes/snapshot").json()["cursor"]


# 共线拓扑（无过敏原内容，验证封存与声明无关）：
# EQ-A: P1(RM-H -> A1) 08-09, P2(A1 -> A2) 10-11（残留边），
#       P3(RM-X -> A3) 12-13（残留边），P5(RM-X -> A5) 14-15（投入边为
#       RM-X；残留边 A3->A5）——投入链与残留链在 P5 分离，便于单独验证清洗截断。
# EQ-B: P4(RM-Y -> B1) 08-09
def build_topology(c):
    material(c, "RM-H")
    material(c, "RM-X")
    material(c, "RM-Y")
    assert processing(c, "P1", "EQ-A", "2026-01-02T08:00:00Z",
                      "2026-01-02T09:00:00Z", ["RM-H"], ["A1"]).status_code == 201
    assert processing(c, "P2", "EQ-A", "2026-01-02T10:00:00Z",
                      "2026-01-02T11:00:00Z", ["A1"], ["A2"]).status_code == 201
    assert processing(c, "P3", "EQ-A", "2026-01-02T12:00:00Z",
                      "2026-01-02T13:00:00Z", ["RM-X"], ["A3"]).status_code == 201
    assert processing(c, "P4", "EQ-B", "2026-01-02T08:00:00Z",
                      "2026-01-02T09:00:00Z", ["RM-Y"], ["B1"]).status_code == 201
    assert processing(c, "P5", "EQ-A", "2026-01-02T14:00:00Z",
                      "2026-01-02T15:00:00Z", ["RM-X"], ["A5"]).status_code == 201


# ---------------------------------------------------------------- 登记与传播

def test_hold_independent_of_allergen_declaration(client):
    build_topology(client)
    # 原料无过敏原、产出实时结论为空，仍可封存
    assert client.get("/batches/A3/trace").json()["allergens"] == []
    r = investigate(client, "INV-1", "RM-H", "清洗前共线疑似污染")
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "open" and d["batch_id"] == "RM-H"
    held = {b["batch_id"]: b["paths"] for b in d["affected_batches"]}
    # 投入链 + 残留链（跨 P1->P2->P3->P5）：RM-H, A1, A2, A3, A5
    assert set(held) == {"RM-H", "A1", "A2", "A3", "A5"}
    # B1 在另一设备，不受影响
    assert "B1" not in held
    # 根路径
    assert held["RM-H"][0][0]["via"] == "investigation_root"
    # A5 的传播路径：RM-H-A1（P2 残留）->A2（P3 残留）->A3，
    # P5 经残留边与投入边（投入恰为 RM-X，此处以 P2 投入替代场景外）接收：
    # 主路径末三步残留；另有 A2 作为 P2 投入的一条 input 路径。
    vias_a5 = {tuple(s["via"] for s in p) for p in held["A5"]}
    assert ("investigation_root", "input", "equipment_residue",
            "equipment_residue", "equipment_residue") in vias_a5
    assert ("investigation_root", "input", "input",
            "equipment_residue", "equipment_residue") in vias_a5
    # 残留步骤标注设备
    last_residue = [p for p in held["A5"]
                     if p[-1]["via"] == "equipment_residue"][0]
    assert last_residue[-1]["equipment_id"] == "EQ-A"
    assert [s.get("batch_id") for s in last_residue] == [
        "RM-H", "A1", "A2", "A3", "A5"]


def test_trace_and_release_query_show_hold(client):
    build_topology(client)
    investigate(client, "INV-1", "RM-H")
    t = client.get("/batches/A2/trace").json()
    # 过敏原结论仍为空，但封存状态独立呈现
    assert t["allergens"] == [] and t["pending"] is False
    assert t["release"]["held"] is True
    assert [i["investigation_id"] for i in t["release"]["investigations"]] == ["INV-1"]
    q = client.get("/batches/A2/release").json()
    assert q["release_status"] == "held" and q["held"] is True
    assert q["active_investigations"][0]["root_batch_id"] == "RM-H"
    # 未受影响批次可放行
    free = client.get("/batches/B1/release").json()
    assert free["release_status"] == "releasable" and free["held"] is False
    assert free["active_investigations"] == []


def test_unknown_batch_and_investigation_errors(client):
    build_topology(client)
    assert client.get("/batches/GHOST/release").status_code == 404
    assert client.get("/investigations/NOPE").status_code == 404
    assert lift(client, "NOPE").status_code == 404
    r = investigate(client, "INV-1", "GHOST", "x")
    assert r.status_code == 400  # 未知批次登记调查 -> 400
    assert "未知批次" in r.json()["detail"]


def test_duplicate_investigation_id_rejected(client):
    build_topology(client)
    assert investigate(client, "INV-1", "RM-H").status_code == 201
    r = investigate(client, "INV-1", "RM-Y")
    assert r.status_code == 409 and "已存在" in r.json()["detail"]
    # 失败不留痕：只有第一个调查
    ids = [x["investigation_id"] for x in client.get("/investigations").json()]
    assert ids == ["INV-1"]
    assert client.get("/batches/RM-Y/release").json()["held"] is False


def test_blank_fields_rejected(client):
    build_topology(client)
    r = client.post("/investigations", json={
        "investigation_id": "  ", "batch_id": "RM-H", "reason": "x"})
    assert r.status_code == 422
    r = client.post("/investigations", json={
        "investigation_id": "I", "batch_id": "RM-H", "reason": "   "})
    assert r.status_code == 422


# ---------------------------------------------------------------- 清洗截断

def test_verified_cleaning_truncates_residue_hold(client):
    build_topology(client)
    # 独立隔离 P3/P5 之间的残留传播：先解除调查，在 P3/P5 间登记有效清洗，
    # 再重新封存 RM-H —— 清洗应截断 A3->A5 的残留边（投入链另用隔离场景验证）。
    investigate(client, "INV-1", "RM-H")
    before = {b["batch_id"] for b in
              client.get("/investigations/INV-1").json()["affected_batches"]}
    assert before == {"RM-H", "A1", "A2", "A3", "A5"}
    assert lift(client, "INV-1").status_code == 200
    assert client.post("/cleanings", json={
        "equipment_id": "EQ-A", "cleaned_at": "2026-01-02T13:30:00Z",
        "verified": True}).status_code == 201
    investigate(client, "INV-2", "A3")  # 只沿残留链传播
    held = {b["batch_id"] for b in
            client.get("/investigations/INV-2").json()["affected_batches"]}
    # A3 为根；P3/P5 间有效清洗 -> 残留截断，A5 不再封存
    assert held == {"A3"}
    assert client.get("/batches/A5/release").json()["held"] is False

    # 投入链不受设备清洗影响：对 A1 发起的调查仍沿 A1->A2 投入边封存 A2，
    # 但 A2->A3 是设备残留链（P3 前置 P2），另用 P2/P3 窗口验证：
    # 在 P2/P3 间加清洗后重开调查，A2 经投入仍封存、A3 被残留截断。
    assert client.post("/cleanings", json={
        "equipment_id": "EQ-A", "cleaned_at": "2026-01-02T11:30:00Z",
        "verified": True}).status_code == 201
    investigate(client, "INV-3", "A1")
    held3 = {b["batch_id"] for b in
             client.get("/investigations/INV-3").json()["affected_batches"]}
    assert held3 == {"A1", "A2"}


def test_unverified_cleaning_does_not_truncate(client):
    build_topology(client)
    client.post("/cleanings", json={
        "equipment_id": "EQ-A", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": False})
    investigate(client, "INV-1", "RM-H")
    held = {b["batch_id"] for b in
            client.get("/investigations/INV-1").json()["affected_batches"]}
    assert "A2" in held and "A3" in held and "A5" in held


def test_cleaning_revocation_restores_hold(client):
    build_topology(client)
    # 隔离残留链：对 A3 发起调查；P3/P5 间有效清洗使 A5 不封存
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-A", "cleaned_at": "2026-01-02T13:30:00Z",
        "verified": True}).json()["id"]
    investigate(client, "INV-1", "A3")
    assert client.get("/batches/A5/release").json()["held"] is False
    cur = cursor(client)
    # 撤销清洗 -> 残留恢复，A5 被重新封存
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 200
    page = events(client, cur)
    assert [e["batch_id"] for e in page["events"]] == ["A5"]
    ev = page["events"][0]
    assert ev["after"]["release"]["held"] is True
    assert ev["after"]["release"]["investigations"][0]["investigation_id"] == "INV-1"
    assert client.get("/batches/A5/release").json()["held"] is True


# ---------------------------------------------------------------- 叠加与解除

def test_overlapping_investigations_lift_only_removes_own_effect(client):
    build_topology(client)
    # INV-1 从 RM-H 封存（A1,A2,A3,A5）；INV-2 从 A3 封存（A5）
    investigate(client, "INV-1", "RM-H")
    investigate(client, "INV-2", "A3", "下游疑似")
    a2 = client.get("/batches/A2/release").json()
    assert [i["investigation_id"] for i in a2["active_investigations"]] == ["INV-1"]
    a5 = client.get("/batches/A5/release").json()
    assert sorted(i["investigation_id"] for i in a5["active_investigations"]) == [
        "INV-1", "INV-2"]
    # 解除 INV-1：A1/A2 解除；A3/A5 仍被 INV-2 封存
    assert lift(client, "INV-1").status_code == 200
    assert client.get("/batches/A2/release").json()["held"] is False
    a3 = client.get("/batches/A3/release").json()
    assert a3["held"] is True
    assert [i["investigation_id"] for i in a3["active_investigations"]] == ["INV-2"]
    a5b = client.get("/batches/A5/release").json()
    assert [i["investigation_id"] for i in a5b["active_investigations"]] == ["INV-2"]
    held = {b["batch_id"] for b in
            client.get("/investigations/INV-1").json()["affected_batches"]}
    assert held == set()  # 已解除调查不再封存任何批次
    # 重复解除 -> 409
    assert lift(client, "INV-1").status_code == 409
    # 解除 INV-2 后全部放行
    assert lift(client, "INV-2").status_code == 200
    assert client.get("/batches/A5/release").json()["held"] is False


def test_lift_atomic_and_events(client):
    build_topology(client)
    investigate(client, "INV-1", "RM-H")
    cur = cursor(client)
    r = lift(client, "INV-1")
    assert r.status_code == 200
    assert r.json()["status"] == "lifted" and r.json()["lifted_at"]
    page = events(client, cur)
    held_before = {"RM-H", "A1", "A2", "A3", "A5"}
    assert set(e["batch_id"] for e in page["events"]) == held_before
    assert all(e["op"] == "investigation_lift" for e in page["events"])
    assert all(e["op_ref"] == {"investigation_id": "INV-1"} for e in page["events"])
    assert all(e["before"]["release"]["held"] for e in page["events"])
    assert all(not e["after"]["release"]["held"] for e in page["events"])
    # 事件按批次号顺序
    seq_batches = [e["batch_id"] for e in page["events"]]
    assert seq_batches == sorted(seq_batches)
    # 旧标签快照字段（conclusion/basis）在封存事件中仍存在
    assert "conclusion" in page["events"][0]["after"]


def test_open_events_sorted_and_query_listing(client):
    build_topology(client)
    cur = cursor(client)
    investigate(client, "INV-1", "RM-H")
    page = events(client, cur)
    batches = [e["batch_id"] for e in page["events"]]
    assert batches == sorted(batches)
    assert all(e["op"] == "investigation_open" for e in page["events"])
    by_batch = {e["batch_id"]: e for e in page["events"]}
    assert by_batch["RM-H"]["before"] is None or \
        by_batch["RM-H"]["after"]["release"]["held"]
    # 列表与过滤
    assert len(client.get("/investigations").json()) == 1
    assert client.get("/investigations", params={"status": "open"}).json()
    assert client.get("/investigations", params={"status": "lifted"}).json() == []
    lift(client, "INV-1")
    assert client.get("/investigations", params={"status": "lifted"}).json()
    assert client.get("/investigations", params={"status": "weird"}).status_code == 400


# ---------------------------------------------------------------- 操作约束

def test_held_batch_blocks_label(client):
    build_topology(client)
    # 封存前签发的标签保留
    label = client.post("/batches/A2/labels", json={})
    assert label.status_code == 201
    investigate(client, "INV-1", "RM-H")
    r = client.post("/batches/A2/labels", json={})
    assert r.status_code == 409 and "封存" in r.json()["detail"]
    # 失败不留标签
    labels = client.get("/batches/A2/labels").json()
    assert [l["label_id"] for l in labels] == [label.json()["label_id"]]
    # 旧标签快照不变
    frozen = client.get(f"/labels/{label.json()['label_id']}").json()
    assert "release" not in frozen["conclusion"]
    # 解除后可签发
    lift(client, "INV-1")
    assert client.post("/batches/A2/labels", json={}).status_code == 201


def test_held_input_blocks_new_processing(client):
    build_topology(client)
    material(client, "RM-Z")
    investigate(client, "INV-1", "RM-H")
    # 封存批次作为投入 -> 拒绝
    r = processing(client, "P9", "EQ-B", "2026-01-03T08:00:00Z",
                   "2026-01-03T09:00:00Z", ["A2"], ["Z1"])
    assert r.status_code == 409 and "封存" in r.json()["detail"]
    # 失败不留加工
    assert client.get("/processings/P9").status_code == 404
    # 封存解除后可登记
    lift(client, "INV-1")
    assert processing(client, "P9", "EQ-B", "2026-01-03T08:00:00Z",
                      "2026-01-03T09:00:00Z", ["A2"], ["Z1"]).status_code == 201


def test_held_residue_blocks_new_processing_until_cleaning(client):
    build_topology(client)
    material(client, "RM-Z")
    investigate(client, "INV-1", "RM-H")  # A3 经残留被封存
    # EQ-A 在 P3 之后登记新加工：A3 的残留会封存新产出 -> 拒绝
    r = processing(client, "P9", "EQ-A", "2026-01-02T16:00:00Z",
                   "2026-01-02T17:00:00Z", ["RM-Z"], ["Z1"])
    assert r.status_code == 409 and "残留" in r.json()["detail"]
    assert client.get("/processings/P9").status_code == 404
    # P3/P9 之间补有效清洗（解除调查 -> 登记清洗 -> 重新封存验证截断）
    lift(client, "INV-1")
    client.post("/cleanings", json={
        "equipment_id": "EQ-A", "cleaned_at": "2026-01-02T15:30:00Z",
        "verified": True})
    investigate(client, "INV-2", "RM-H")
    # 清洗截断残留：即使 A3 封存，新加工不再受残留影响，可登记
    assert processing(client, "P9", "EQ-A", "2026-01-02T16:00:00Z",
                      "2026-01-02T17:00:00Z", ["RM-Z"], ["Z1"]).status_code == 201
    # 新产出可放行
    assert client.get("/batches/Z1/release").json()["held"] is False


def test_input_correction_recomputes_hold(client):
    build_topology(client)
    investigate(client, "INV-1", "RM-H")
    assert client.get("/batches/A5/release").json()["held"] is True
    cur = cursor(client)
    # 在 P3/P5 之间补有效清洗：残留边 A3->A5 被截断，A5 解除封存
    # （P5 投入是 RM-X，与 A3 的投入链无关）
    r = client.post("/cleanings", json={
        "equipment_id": "EQ-A", "cleaned_at": "2026-01-02T13:30:00Z",
        "verified": True})
    assert r.status_code == 201, r.text
    assert client.get("/batches/A5/release").json()["held"] is False
    page = events(client, cur)
    assert [e["batch_id"] for e in page["events"]] == ["A5"]
    assert not page["events"][0]["after"]["release"]["held"]
    # 撤销清洗 -> 残留恢复，A5 重新封存
    cid = r.json()["id"]
    cur = cursor(client)
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 200
    page = events(client, cur)
    assert [e["batch_id"] for e in page["events"]] == ["A5"]
    assert page["events"][0]["after"]["release"]["held"] is True


def test_correction_can_use_held_input(client):
    # 纠正允许使用封存批次作为新投入（纠正后重算封存），不按新加工拒绝
    build_topology(client)
    investigate(client, "INV-1", "RM-Y")  # B1 封存
    r = client.post("/processings/P2/inputs/correction",
                    json={"inputs": ["A1", "B1"]})
    assert r.status_code == 200, r.text
    # A2 经投入链新增被 INV-1 封存
    assert client.get("/batches/A2/release").json()["held"] is True
    inv = client.get("/batches/A2/release").json()["active_investigations"][0]
    assert inv["investigation_id"] == "INV-1"


# ---------------------------------------------------------------- 事件流兼容

def test_snapshot_and_event_replay_include_release(client):
    build_topology(client)
    investigate(client, "INV-1", "RM-H")
    snap = client.get("/changes/snapshot").json()
    rel = {b["batch_id"]: b["release"] for b in snap["batches"]}
    assert rel["A3"]["held"] is True
    assert rel["B1"]["held"] is False
    # 既有结论字段保持兼容
    for b in snap["batches"]:
        assert set(b["conclusion"]) >= {
            "pending", "allergens", "allergen_paths", "pending_paths"}
    # 事件回放终态与快照一致（含 release）
    state, c = {}, 0
    while True:
        page = events(client, c, limit=3)
        for ev in page["events"]:
            state[ev["batch_id"]] = ev["after"]
        c = page["next_cursor"]
        if not page["has_more"]:
            break
    for b in snap["batches"]:
        assert state[b["batch_id"]]["release"] == b["release"]


# ---------------------------------------------------------------- 乱序补登

def test_hold_propagates_with_out_of_order_processing_registration(client):
    # 先登记晚加工 P2/P3，再补登早加工 P1（乱序补登），封存仍须沿残留链正确传播
    material(client, "RM-H")
    material(client, "RM-X")
    assert processing(client, "P2", "EQ-A", "2026-01-02T10:00:00Z",
                       "2026-01-02T11:00:00Z", ["RM-X"], ["A2"]).status_code == 201
    assert processing(client, "P3", "EQ-A", "2026-01-02T12:00:00Z",
                       "2026-01-02T13:00:00Z", ["RM-X"], ["A3"]).status_code == 201
    # P1 插入到 P2 之前；P2 的“上一次加工”变为 P1
    assert processing(client, "P1", "EQ-A", "2026-01-02T08:00:00Z",
                       "2026-01-02T09:00:00Z", ["RM-H"], ["A1"]).status_code == 201
    investigate(client, "INV-1", "RM-H")
    held = {b["batch_id"] for b in
            client.get("/investigations/INV-1").json()["affected_batches"]}
    assert held == {"RM-H", "A1", "A2", "A3"}


def test_failed_operations_leave_no_hold_events(client):
    build_topology(client)
    cur = cursor(client)
    assert investigate(client, "INV-1", "GHOST", "x").status_code == 400
    investigate(client, "INV-1", "RM-H")
    assert lift(client, "INV-1").status_code == 200
    assert lift(client, "INV-1").status_code == 409   # 重复解除
    # 解除后标签可签发；重新封存后被拒绝
    assert client.post("/batches/A2/labels", json={}).status_code == 201
    investigate(client, "INV-2", "RM-H")
    assert client.post("/batches/A2/labels", json={}).status_code == 409
    assert processing(client, "P9", "EQ-A", "2026-01-02T16:00:00Z",
                      "2026-01-02T17:00:00Z", ["A3"], ["Z1"]).status_code == 409
    page = events(client, cur)
    # 只有成功的登记/解除各一组成事件；失败操作零事件
    ops = [e["op"] for e in page["events"]]
    assert set(ops) == {"investigation_open", "investigation_lift"}
    # 每种操作各一次（open INV-1 / lift / open INV-2 共三次成功写入）
    assert [e["op"] for e in page["events"]
            if e["batch_id"] == "RM-H"] == [
        "investigation_open", "investigation_lift", "investigation_open"]
