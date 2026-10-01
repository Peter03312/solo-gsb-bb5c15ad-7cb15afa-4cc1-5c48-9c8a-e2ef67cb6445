"""端到端测试：登记校验、残留继承、清洗与撤销、声明更正、标签冻结。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 每个测试会话使用独立数据库文件
_tmp = tempfile.mkdtemp(prefix="allergen-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # 重置数据库
    db.init_db()
    conn = db.connect()
    for t in ["material_declarations", "material_batches", "processing_inputs",
              "processing_outputs", "processings", "cleanings", "labels",
              "trace_events"]:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'trace_events'")
    conn.close()
    with TestClient(app) as c:
        yield c


def material(c, batch_id, allergens=None, pending=False, declared_at="2026-01-01T00:00:00Z"):
    return c.post("/materials", json={
        "batch_id": batch_id, "allergens": allergens or [],
        "pending": pending, "declared_at": declared_at})


def processing(c, pid, eq, start, end, inputs, outputs):
    return c.post("/processings", json={
        "processing_id": pid, "equipment_id": eq,
        "start_utc": start, "end_utc": end,
        "inputs": inputs, "outputs": outputs})


def correct_inputs(c, pid, inputs):
    return c.post(f"/processings/{pid}/inputs/correction", json={"inputs": inputs})


# ---------------------------------------------------------------- 基础登记与校验

def test_material_and_trace(client):
    r = material(client, "RM-1", allergens=["milk", "peanut"])
    assert r.status_code == 201, r.text
    t = client.get("/batches/RM-1/trace").json()
    assert t["allergens"] == ["milk", "peanut"]
    assert t["pending"] is False
    assert t["allergen_paths"]["milk"][0][0]["via"] == "declaration"


def test_pending_material(client):
    material(client, "RM-P", pending=True)
    t = client.get("/batches/RM-P/trace").json()
    assert t["pending"] is True
    assert t["pending_paths"][0][0]["batch_id"] == "RM-P"


def test_duplicate_material_rejected(client):
    assert material(client, "RM-1").status_code == 201
    r = material(client, "RM-1")
    assert r.status_code == 409


def test_invalid_time_range_rejected(client):
    material(client, "RM-1")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T09:00:00Z",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 400
    assert "无效时序" in r.json()["detail"]
    # 原状态保持：无加工记录
    assert client.get("/processings").json() == []


def test_naive_datetime_rejected(client):
    material(client, "RM-1")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00", "2026-01-02T09:00:00",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 422  # 必须带时区


def test_unknown_input_rejected(client):
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                   ["GHOST"], ["OUT-1"])
    assert r.status_code == 400


def test_input_not_available_before_start(client):
    # 原料 1 月 2 日 10 点才登记，加工 9 点开始 -> 不可用
    material(client, "RM-LATE", declared_at="2026-01-02T10:00:00Z")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T09:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-LATE"], ["OUT-1"])
    assert r.status_code == 400
    assert "不可用" in r.json()["detail"]
    # 上游产出未结束就投入也不可用
    material(client, "RM-2", declared_at="2026-01-01T00:00:00Z")
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T11:00:00Z", "2026-01-02T12:00:00Z",
                      ["RM-2"], ["MID-1"]).status_code == 201
    r = processing(client, "P3", "EQ-2",
                   "2026-01-02T11:30:00Z", "2026-01-02T12:30:00Z",
                   ["MID-1"], ["OUT-3"])
    assert r.status_code == 400  # MID-1 12:00 才可用


def test_equipment_overlap_rejected(client):
    material(client, "RM-1")
    material(client, "RM-2")
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T10:00:00Z",
                      ["RM-1"], ["OUT-1"]).status_code == 201
    # 部分重叠
    r = processing(client, "P2", "EQ-1",
                   "2026-01-02T09:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-2"], ["OUT-2"])
    assert r.status_code == 409
    assert "重叠" in r.json()["detail"]
    # 相邻不重叠（前序结束即开始）允许
    assert processing(client, "P3", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-2"], ["OUT-3"]).status_code == 201
    # 不同设备同时间段允许
    assert processing(client, "P4", "EQ-2",
                      "2026-01-02T08:30:00Z", "2026-01-02T09:30:00Z",
                      ["RM-2"], ["OUT-4"]).status_code == 201


def test_cycle_and_output_conflict_rejected(client):
    material(client, "RM-1")
    # 自环：同一批次既是投入又是产出
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                   ["RM-1"], ["RM-1"])
    assert r.status_code == 409
    assert "成环" in r.json()["detail"]
    # 产出批次号已被占用
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-1"], ["OUT-1"]).status_code == 201
    r = processing(client, "P3", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 409
    assert "冲突" in r.json()["detail"]
    # 把已登记的原料当产出也是来源冲突
    r = processing(client, "P4", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                   ["OUT-1"], ["RM-1"])
    assert r.status_code == 409


# ---------------------------------------------------------------- 继承与残留

def _build_line(client):
    """EQ-1: P1(RM-MILK -> OUT-1) 08-09, P2(RM-OAT -> OUT-2) 10-11"""
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-OAT", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-MILK"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OAT"], ["OUT-2"]).status_code == 201


def test_residue_inherited_without_cleaning(client):
    _build_line(client)
    t = client.get("/batches/OUT-2/trace").json()
    # 投入继承 gluten + 设备残留 milk
    assert t["allergens"] == ["gluten", "milk"]
    residue_paths = [p for p in t["allergen_paths"]["milk"]
                     if p[-1]["via"] == "equipment_residue"]
    assert residue_paths, "milk 应来自设备残留"
    p = residue_paths[0]
    assert [s.get("batch_id") for s in p] == ["RM-MILK", "OUT-1", "OUT-2"]
    assert p[-1]["equipment_id"] == "EQ-1"


def test_verified_cleaning_clears_residue(client):
    _build_line(client)
    r = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True})
    assert r.status_code == 201
    t = client.get("/batches/OUT-2/trace").json()
    assert t["allergens"] == ["gluten"]
    assert t["basis"]["cleanings"][0]["id"] == r.json()["id"]


def test_unverified_cleaning_does_not_clear(client):
    _build_line(client)
    client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": False})
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]


def test_revoked_cleaning_restores_residue(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    assert client.get("/batches/OUT-2/trace").json()["allergens"] == ["gluten"]
    # 撤销清洗 -> 未签发结论按当前有效记录重算，残留恢复
    r = client.post(f"/cleanings/{cid}/revoke")
    assert r.status_code == 200
    assert r.json()["revoked"] is True
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]
    # 重复撤销拒绝
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 409


def test_cleaning_outside_window_does_not_clear(client):
    _build_line(client)
    # 清洗发生在 P2 开始之后，不在两次加工之间
    client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T10:30:00Z",
        "verified": True})
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]


def test_pending_propagates_through_processing_and_residue(client):
    material(client, "RM-PEND", pending=True)
    material(client, "RM-OK", allergens=["soy"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-PEND"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OK"], ["OUT-2"]).status_code == 201
    t = client.get("/batches/OUT-2/trace").json()
    assert t["pending"] is True  # 残留带入待确认
    assert t["pending_paths"][0][-1]["via"] == "equipment_residue"


# ---------------------------------------------------------------- 声明更正与标签

def test_declaration_correction_recomputes_live(client):
    _build_line(client)
    # 更正：RM-OAT 实际含 sesame
    r = client.post("/materials/RM-OAT/declarations",
                    json={"allergens": ["gluten", "sesame"], "pending": False})
    assert r.status_code == 201
    assert r.json()["version"] == 2
    t = client.get("/batches/OUT-2/trace").json()
    assert t["allergens"] == ["gluten", "milk", "sesame"]
    # 依据记录为第 2 版声明
    versions = {d["batch_id"]: d["version"]
                for d in t["basis"]["material_declarations"]}
    assert versions["RM-OAT"] == 2


def test_label_frozen_and_superseded(client):
    _build_line(client)
    # 签发标签
    r = client.post("/batches/OUT-2/labels", json={})
    assert r.status_code == 201
    label1 = r.json()
    assert label1["conclusion"]["allergens"] == ["gluten", "milk"]
    # 声明更正 + 清洗撤销等后续变更
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    # 旧标签不变
    old = client.get(f"/labels/{label1['label_id']}").json()
    assert old["conclusion"]["allergens"] == ["gluten", "milk"]
    # 实时结论已变（清洗生效，milk 消失；sesame 出现）
    live = client.get("/batches/OUT-2/trace").json()
    assert live["allergens"] == ["gluten", "sesame"]
    # 签发关联旧版的新标签
    r = client.post("/batches/OUT-2/labels",
                    json={"supersedes_id": label1["label_id"]})
    assert r.status_code == 201
    label2 = r.json()
    assert label2["supersedes_id"] == label1["label_id"]
    assert label2["conclusion"]["allergens"] == ["gluten", "sesame"]
    # 撤销清洗后，两张已签发标签仍不变
    client.post(f"/cleanings/{cid}/revoke")
    assert client.get(f"/labels/{label2['label_id']}").json()[
        "conclusion"]["allergens"] == ["gluten", "sesame"]
    # 新实时结论再次包含 milk
    assert "milk" in client.get("/batches/OUT-2/trace").json()["allergens"]
    # 标签历史
    labels = client.get("/batches/OUT-2/labels").json()
    assert [l["label_id"] for l in labels] == [label1["label_id"], label2["label_id"]]


def test_label_supersedes_validation(client):
    _build_line(client)
    material(client, "RM-X")
    l1 = client.post("/batches/OUT-2/labels", json={}).json()
    # 旧标签属于别的批次 -> 拒绝
    r = client.post("/batches/RM-X/labels",
                    json={"supersedes_id": l1["label_id"]})
    assert r.status_code == 400
    # 旧标签不存在
    assert client.post("/batches/RM-X/labels",
                       json={"supersedes_id": 999}).status_code == 404


def test_unknown_batch_404(client):
    assert client.get("/batches/NOPE/trace").status_code == 404
    assert client.post("/batches/NOPE/labels", json={}).status_code == 404


# ---------------------------------------------------------------- 加工投入纠正

def test_correct_processing_inputs_recomputes_with_residue(client):
    # EQ-1: P1(RM-MILK -> OUT-1), P2(RM-WRONG -> OUT-2), P3(RM-X -> OUT-3)
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-WRONG", allergens=["sesame"])
    material(client, "RM-RIGHT", allergens=["gluten"])
    material(client, "RM-X")
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-MILK"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-WRONG"], ["OUT-2"]).status_code == 201
    assert processing(client, "P3", "EQ-1",
                      "2026-01-02T12:00:00Z", "2026-01-02T13:00:00Z",
                      ["RM-X"], ["OUT-3"]).status_code == 201
    cursor = _cursor(client)

    # 纠正 P2 的投入：RM-WRONG -> RM-RIGHT；加工号/设备/时段/产出保持原值
    r = correct_inputs(client, "P2", ["RM-RIGHT"])
    assert r.status_code == 200, r.text
    p2 = r.json()
    assert p2["processing_id"] == "P2"
    assert p2["equipment_id"] == "EQ-1"
    assert p2["start_utc"] == "2026-01-02T10:00:00Z"
    assert p2["end_utc"] == "2026-01-02T11:00:00Z"
    assert p2["inputs"] == ["RM-RIGHT"]
    assert p2["outputs"] == ["OUT-2"]

    # OUT-2：sesame 消失、gluten 出现，milk 仍经设备残留来自 OUT-1
    t2 = client.get("/batches/OUT-2/trace").json()
    assert t2["allergens"] == ["gluten", "milk"]
    milk_path = [p for p in t2["allergen_paths"]["milk"]
                 if p[-1]["via"] == "equipment_residue"][0]
    assert [s.get("batch_id") for s in milk_path] == ["RM-MILK", "OUT-1", "OUT-2"]
    gluten_path = t2["allergen_paths"]["gluten"][0]
    assert [s.get("batch_id") for s in gluten_path] == ["RM-RIGHT", "OUT-2"]
    assert gluten_path[-1]["via"] == "input"
    # 依据随纠正更新为 RM-RIGHT 的声明
    decls = {d["batch_id"] for d in t2["basis"]["material_declarations"]}
    assert decls == {"RM-MILK", "RM-RIGHT"}

    # OUT-3：后续设备残留传播——sesame 消失，gluten 经 OUT-2 残留进入
    t3 = client.get("/batches/OUT-3/trace").json()
    assert t3["allergens"] == ["gluten", "milk"]
    gpath = [p for p in t3["allergen_paths"]["gluten"]
             if p[-1]["via"] == "equipment_residue"][0]
    assert [s.get("batch_id") for s in gpath] == ["RM-RIGHT", "OUT-2", "OUT-3"]
    assert "sesame" not in t3["allergens"]
    # OUT-1 与纠正无关，结论不变
    assert client.get("/batches/OUT-1/trace").json()["allergens"] == ["milk"]

    # 事件：仅实际变化的批次（OUT-2、OUT-3），按批次号排序；op/op_ref 沿用既有字段
    page = _events(client, cursor)
    assert [e["batch_id"] for e in page["events"]] == ["OUT-2", "OUT-3"]
    ev = page["events"][0]
    assert ev["op"] == "processing_input_correction"
    assert ev["op_ref"] == {"processing_id": "P2"}
    assert ev["before"]["conclusion"]["allergens"] == ["milk", "sesame"]
    assert ev["after"]["conclusion"]["allergens"] == ["gluten", "milk"]


def test_correct_inputs_multiple_with_pending_and_residue(client):
    material(client, "RM-A", allergens=["milk"])
    material(client, "RM-P", pending=True)
    material(client, "RM-BLANK")
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-A"], ["OUT-1"]).status_code == 201
    cursor = _cursor(client)
    # 多投入纠正（替换为空白原料 + 待确认原料）：待确认经投入传播
    r = correct_inputs(client, "P1", ["RM-BLANK", "RM-P"])
    assert r.status_code == 200, r.text
    assert r.json()["inputs"] == ["RM-BLANK", "RM-P"]
    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == []
    assert t["pending"] is True
    via = {s["batch_id"]: s["via"] for s in t["pending_paths"][0]}
    assert via == {"RM-P": "declaration", "OUT-1": "input"}
    page = _events(client, cursor)
    assert [e["batch_id"] for e in page["events"]] == ["OUT-1"]


def test_correct_inputs_unchanged_set_produces_no_events(client):
    material(client, "RM-1", allergens=["milk"])
    material(client, "RM-2", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-1", "RM-2"], ["OUT-1"]).status_code == 201
    cursor = _cursor(client)
    # 同集合（顺序不同、含去重）纠正：不替换、不产事件
    r = correct_inputs(client, "P1", ["RM-2", "RM-1"])
    assert r.status_code == 200
    assert r.json()["inputs"] == ["RM-1", "RM-2"]
    assert _events(client, cursor)["events"] == []
    assert _cursor(client) == cursor


def test_correct_inputs_validation_errors_leave_state_untouched(client):
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-OAT", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-MILK"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-2",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["OUT-1"], ["OUT-2"]).status_code == 201

    # 加工不存在 -> 404
    assert correct_inputs(client, "NOPE", ["RM-MILK"]).status_code == 404
    # 下游产出 OUT-2 在 P1 开工后才可用 -> 开工前不可用 400（来源时序上也会成环）
    r = correct_inputs(client, "P1", ["OUT-2"])
    assert r.status_code == 400 and "不可用" in r.json()["detail"]
    # 自环：本加工产出同时作为投入 -> 409（成环检查先于可用性）
    r = correct_inputs(client, "P1", ["RM-OAT", "OUT-1"])
    assert r.status_code == 409 and "成环" in r.json()["detail"]
    # 未登记投入 -> 400
    r = correct_inputs(client, "P1", ["GHOST"])
    assert r.status_code == 400
    # 开工前不可用：RM-LATE 晚于 P1 开始时间登记 -> 400
    material(client, "RM-LATE", declared_at="2026-01-02T10:30:00Z")
    r = correct_inputs(client, "P1", ["RM-LATE"])
    assert r.status_code == 400 and "不可用" in r.json()["detail"]
    # 非空/无重复（请求级校验）-> 422
    assert correct_inputs(client, "P1", []).status_code == 422
    assert correct_inputs(client, "P1", ["RM-MILK", "RM-MILK"]).status_code == 422

    # 全部失败：记录保持原状（投入仍为 RM-MILK），且纠正操作未产生任何事件
    assert client.get("/processings/P1").json()["inputs"] == ["RM-MILK"]
    assert client.get("/batches/OUT-1/trace").json()["allergens"] == ["milk"]
    # 游标推进到 RM-LATE 登记之后（登记事件与本功能无关），其后应为空
    assert _events(client, _cursor(client))["events"] == []


def test_correct_inputs_generic_cycle_detected(client):
    # 直接落库构造来源边（绕过服务时序校验）：
    # RM-A --P1--> OUT-1 --P2--> OUT-2；P1 原投入为 RM-A。
    # 把 P1 投入纠正为 OUT-2（在构造图中 OUT-2 可经 P2 到达，会成环）-> 409。
    from app import db
    material(client, "RM-A", allergens=["milk"])
    conn = db.connect()
    with db.write_tx(conn):
        conn.execute("INSERT INTO processings VALUES ('P1','EQ-1',"
                     "'2026-01-02T08:00:00+00:00','2026-01-02T09:00:00+00:00')")
        conn.execute("INSERT INTO processings VALUES ('P2','EQ-2',"
                     "'2026-01-02T06:00:00+00:00','2026-01-02T07:00:00+00:00')")
        conn.execute("INSERT INTO processing_inputs VALUES ('P1','RM-A')")
        conn.execute("INSERT INTO processing_outputs VALUES ('P1','OUT-1')")
        conn.execute("INSERT INTO processing_inputs VALUES ('P2','OUT-1')")
        conn.execute("INSERT INTO processing_outputs VALUES ('P2','OUT-2')")
    conn.close()
    # OUT-2 的“可用时间”被构造成 07:00（早于 P1 开始），通过可用性后由成环检查拦截
    r = correct_inputs(client, "P1", ["OUT-2"])
    assert r.status_code == 409 and "成环" in r.json()["detail"]
    # 记录保持原状
    assert client.get("/processings/P1").json()["inputs"] == ["RM-A"]


def test_correct_inputs_keeps_issued_label_snapshot(client):
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-WRONG", allergens=["sesame"])
    material(client, "RM-RIGHT", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-WRONG"], ["OUT-1"]).status_code == 201
    label = client.post("/batches/OUT-1/labels", json={}).json()
    # 纠正投入：sesame -> gluten（实时结论改变）
    assert correct_inputs(client, "P1", ["RM-RIGHT"]).status_code == 200
    assert client.get("/batches/OUT-1/trace").json()["allergens"] == ["gluten"]
    # 已签发标签保留原快照
    frozen = client.get(f"/labels/{label['label_id']}").json()
    assert frozen["conclusion"]["allergens"] == ["sesame"]
    # 纠正后签发的新标签反映新实时结论
    label2 = client.post("/batches/OUT-1/labels",
                         json={"supersedes_id": label["label_id"]}).json()
    assert label2["conclusion"]["allergens"] == ["gluten"]


# ---------------------------------------------------------------- 预演与凭据提交

def test_declaration_preview_is_dry_run_and_reports_diffs(client):
    _build_line(client)  # EQ-1: P1(RM-MILK->OUT-1), P2(RM-OAT->OUT-2)，无清洗
    # 预演前签发标签：冻结 gluten + milk
    label = client.post("/batches/OUT-2/labels", json={}).json()
    r = client.post("/materials/RM-OAT/declarations/preview",
                    json={"allergens": ["gluten", "sesame"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dry_run"] is True and d["applied"] is False
    assert d["change"]["type"] == "declaration_correction"
    assert d["change"]["new_version"] == 2
    token = d["preview_id"]
    assert token
    affected = {b["batch_id"]: b for b in d["affected_batches"]}
    assert "OUT-2" in affected          # 下游批次（设备残留链外的 OUT-1 不受影响）
    assert "OUT-1" not in affected
    out2 = affected["OUT-2"]
    assert out2["before"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert out2["after"]["conclusion"]["allergens"] == ["gluten", "milk", "sesame"]
    assert out2["diff"]["conclusion"]["allergens"]["added"] == ["sesame"]
    basis_diff = out2["diff"]["basis"]["material_declarations"]["RM-OAT"]
    assert basis_diff["before"]["version"] == 1
    assert basis_diff["after"]["version"] == 2
    # 已签发标签：与变更前实时结论一致（null），与变更后结论存在差异
    labs = out2["issued_labels"]
    assert len(labs) == 1 and labs[0]["label_id"] == label["label_id"]
    assert labs[0]["diff_vs_before"] is None
    assert labs[0]["diff_vs_after"]["allergens"]["added"] == ["sesame"]
    # 预演不落库：声明仍为 v1，实时结论无 sesame，标签接口兼容
    assert client.get("/materials/RM-OAT").json()["version"] == 1
    assert "sesame" not in client.get("/batches/OUT-2/trace").json()["allergens"]
    return token


def test_declaration_preview_commit_applies_atomically(client):
    token = test_declaration_preview_is_dry_run_and_reports_diffs(client)
    label_id = client.get("/batches/OUT-2/labels").json()[0]["label_id"]
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dry_run"] is False and d["applied"] is True
    assert d["affected_count"] >= 1
    assert client.get("/materials/RM-OAT").json()["version"] == 2
    live = client.get("/batches/OUT-2/trace").json()
    assert live["allergens"] == ["gluten", "milk", "sesame"]
    # 已签发标签不改写；凭据一次性
    assert client.get(f"/labels/{label_id}").json()[
        "conclusion"]["allergens"] == ["gluten", "milk"]
    assert client.post(f"/previews/{token}/commit").status_code == 409


def test_preview_credential_invalidated_by_related_change(client):
    _build_line(client)
    token = client.post("/materials/RM-OAT/declarations/preview",
                        json={"allergens": ["sesame"]}).json()["preview_id"]
    # 影响差异的记录变化：目标原料又追加了声明版本
    client.post("/materials/RM-OAT/declarations", json={"allergens": ["gluten"]})
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 409
    assert "重新预演" in r.json()["detail"]
    # 落库未发生：仍是直接更正写入的内容
    assert client.get("/materials/RM-OAT").json()["allergens"] == ["gluten"]


def test_preview_credential_survives_unrelated_records(client):
    _build_line(client)
    token = client.post("/materials/RM-OAT/declarations/preview",
                        json={"allergens": ["gluten", "sesame"]}).json()["preview_id"]
    # 无关原料、无关设备的加工登记不应使凭据失效
    material(client, "RM-X", allergens=["soy"])
    assert processing(client, "PX", "EQ-X",
                      "2026-02-02T08:00:00Z", "2026-02-02T09:00:00Z",
                      ["RM-X"], ["OUT-X"]).status_code == 201
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    assert client.get("/materials/RM-OAT").json()["version"] == 2


def test_preview_credential_invalidated_by_new_related_processing(client):
    _build_line(client)
    token = client.post("/materials/RM-MILK/declarations/preview",
                        json={"allergens": []}).json()["preview_id"]
    # 预演后新增消费 RM-MILK 的加工 -> 实际影响面扩大，凭据必须失效
    assert processing(client, "P3", "EQ-2",
                      "2026-01-03T08:00:00Z", "2026-01-03T09:00:00Z",
                      ["RM-MILK"], ["OUT-3"]).status_code == 201
    assert client.post(f"/previews/{token}/commit").status_code == 409


def test_preview_credential_invalidated_by_new_label(client):
    _build_line(client)
    token = client.post("/materials/RM-OAT/declarations/preview",
                        json={"allergens": ["sesame"]}).json()["preview_id"]
    # 受影响批次上新签发标签 -> 标签差异集合变化，凭据失效
    assert client.post("/batches/OUT-2/labels", json={}).status_code == 201
    assert client.post(f"/previews/{token}/commit").status_code == 409


def test_revoke_preview_reports_residue_propagation_and_commit(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    # 清洗生效时签发标签（仅 gluten）
    label_id = client.post("/batches/OUT-2/labels", json={}).json()["label_id"]
    # 再加第三道加工：撤销清洗后残留沿 OUT-1 -> OUT-2 -> OUT-3 继续传播
    assert processing(client, "P3", "EQ-1",
                      "2026-01-02T12:00:00Z", "2026-01-02T13:00:00Z",
                      ["RM-OAT"], ["OUT-3"]).status_code == 201
    r = client.post(f"/cleanings/{cid}/revoke/preview")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["change"]["type"] == "cleaning_revocation"
    affected = {b["batch_id"]: b for b in d["affected_batches"]}
    assert set(affected) == {"OUT-2", "OUT-3"}
    out3 = affected["OUT-3"]
    assert "milk" in out3["after"]["conclusion"]["allergens"]
    milk_paths = out3["after"]["conclusion"]["allergen_paths"]["milk"]
    vias = [s["via"] for s in milk_paths[0]]
    assert vias == ["declaration", "input", "equipment_residue", "equipment_residue"]
    # 依据变化：before 依赖清洗 cid，after 无清洗依据
    assert str(cid) in affected["OUT-2"]["diff"]["basis"]["cleanings"]
    # OUT-2 标签与变更前一致、与变更后相差 milk（设备残留）
    labs = affected["OUT-2"]["issued_labels"]
    assert labs[0]["diff_vs_before"] is None
    assert labs[0]["diff_vs_after"]["allergens"]["added"] == ["milk"]
    # 预演期间清洗未撤销
    assert client.get(f"/cleanings/{cid}").json()["revoked"] is False
    # 提交：原子应用，实际影响与预演一致
    commit = client.post(f"/previews/{d['preview_id']}/commit")
    assert commit.status_code == 200, commit.text
    assert commit.json()["applied"] is True
    assert client.get(f"/cleanings/{cid}").json()["revoked"] is True
    assert "milk" in client.get("/batches/OUT-3/trace").json()["allergens"]
    assert client.get(f"/labels/{label_id}").json()[
        "conclusion"]["allergens"] == ["gluten"]
    # 既有直接撤销接口语义不变：重复撤销 409
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 409


def test_revoke_preview_survives_unrelated_cleaning(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    token = client.post(f"/cleanings/{cid}/revoke/preview").json()["preview_id"]
    # 无关设备的未验证清洗不影响任何残留结论
    client.post("/cleanings", json={
        "equipment_id": "EQ-9", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": False})
    assert client.post(f"/previews/{token}/commit").status_code == 200


def test_revoke_preview_empty_diff_guards_upstream_declarations(client):
    # P1 的投入原料无过敏原：撤销清洗当下无差异；预演后若上游补报过敏原，
    # 撤销将变得有影响 —— 凭据必须失效。
    material(client, "RM-BLANK")
    material(client, "RM-OAT", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-BLANK"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OAT"], ["OUT-2"]).status_code == 201
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    prev = client.post(f"/cleanings/{cid}/revoke/preview").json()
    # 结论（过敏原/待确认/路径）无差异，但依据（清洗）有差异 -> 仍列出
    assert prev["affected_count"] == 1
    entry = prev["affected_batches"][0]
    assert entry["batch_id"] == "OUT-2"
    assert entry["diff"]["conclusion"] is None
    assert str(cid) in entry["diff"]["basis"]["cleanings"]
    token = prev["preview_id"]
    # 上游补报 milk -> 撤销将把 milk 残留带进 OUT-2（结论开始受影响）
    client.post("/materials/RM-BLANK/declarations", json={"allergens": ["milk"]})
    assert client.post(f"/previews/{token}/commit").status_code == 409
    # 重新预演后可见结论差异并可提交
    prev2 = client.post(f"/cleanings/{cid}/revoke/preview").json()
    entry2 = [b for b in prev2["affected_batches"] if b["batch_id"] == "OUT-2"][0]
    assert entry2["diff"]["conclusion"]["allergens"]["added"] == ["milk"]
    assert client.post(f"/previews/{prev2['preview_id']}/commit").status_code == 200
    assert "milk" in client.get("/batches/OUT-2/trace").json()["allergens"]


def test_preview_invalid_targets_and_errors(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    # 无效目标沿用现有错误语义
    assert client.post("/materials/NOPE/declarations/preview",
                       json={"allergens": ["x"]}).status_code == 404
    assert client.post("/materials/OUT-1/declarations/preview",
                       json={"allergens": ["x"]}).status_code == 404  # 产出批次非原料
    assert client.post("/cleanings/999/revoke/preview").status_code == 404
    client.post(f"/cleanings/{cid}/revoke")
    assert client.post(f"/cleanings/{cid}/revoke/preview").status_code == 409  # 重复撤销
    assert client.post("/previews/bogus-token/commit").status_code == 409
    # 请求格式错误仍为 422（时间/字段校验保持一致）
    r = client.post("/materials/RM-OAT/declarations/preview", json={"allergens": "x"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 变更事件流

def _events(c, cursor=0, **params):
    r = c.get("/changes/events", params={"cursor": cursor, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _cursor(c):
    return c.get("/changes/snapshot").json()["cursor"]


def test_snapshot_empty_then_material_register_event(client):
    snap = client.get("/changes/snapshot").json()
    assert snap["cursor"] == 0 and snap["batches"] == []
    material(client, "RM-1", allergens=["milk"])
    # 初始化读取：同一状态下的现存批次实时结论与游标
    snap2 = client.get("/changes/snapshot").json()
    assert [b["batch_id"] for b in snap2["batches"]] == ["RM-1"]
    assert snap2["batches"][0]["kind"] == "material"
    assert snap2["batches"][0]["conclusion"]["allergens"] == ["milk"]
    # 新批次事件：前态为空
    page = _events(client, snap["cursor"])
    assert len(page["events"]) == 1
    ev = page["events"][0]
    assert ev["seq"] == 1 and ev["op"] == "material_register"
    assert ev["batch_id"] == "RM-1" and ev["kind"] == "material"
    assert ev["before"] is None
    assert ev["after"]["conclusion"]["allergens"] == ["milk"]
    assert ev["after"]["basis"]["material_declarations"][0]["version"] == 1
    assert page["next_cursor"] == ev["seq"] == snap2["cursor"]
    assert page["has_more"] is False


def test_processing_events_and_residue_side_effects(client):
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-OAT", allergens=["gluten"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-MILK"], ["OUT-1"])
    cursor = _cursor(client)
    # P2：新批次 OUT-2 事件（含设备残留 milk）
    processing(client, "P2", "EQ-1",
               "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
               ["RM-OAT"], ["OUT-2"])
    page = _events(client, cursor)
    assert [e["batch_id"] for e in page["events"]] == ["OUT-2"]
    ev = page["events"][0]
    assert ev["op"] == "processing_register"
    assert ev["op_ref"] == {"processing_id": "P2"}
    assert ev["before"] is None
    assert ev["after"]["conclusion"]["allergens"] == ["gluten", "milk"]
    # 乱序补登 P1.5（落在 P1/P2 之间）：OUT-2 残留来源改变 -> 既有批次也产事件
    material(client, "RM-X", allergens=["sesame"])
    cursor = _cursor(client)
    processing(client, "P15", "EQ-1",
               "2026-01-02T09:10:00Z", "2026-01-02T09:50:00Z",
               ["RM-X"], ["OUT-15"])
    page = _events(client, cursor)
    by_batch = {e["batch_id"]: e for e in page["events"]}
    assert set(by_batch) == {"OUT-15", "OUT-2"}
    assert by_batch["OUT-15"]["before"] is None
    # P15 自身继承 P1 的 milk 残留（P1/P15 间无清洗），P2 改从 OUT-15 继承残留：
    # OUT-2 结论增加 sesame，且 milk 的来源路径改经 OUT-15
    out2 = by_batch["OUT-2"]
    assert out2["before"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert out2["after"]["conclusion"]["allergens"] == ["gluten", "milk", "sesame"]
    milk_path = out2["after"]["conclusion"]["allergen_paths"]["milk"][0]
    assert [s.get("batch_id") for s in milk_path] == ["RM-MILK", "OUT-1", "OUT-15", "OUT-2"]
    # 同次操作的事件按批次号排序
    seqs = [e["seq"] for e in page["events"]]
    assert seqs == sorted(seqs)
    assert [e["batch_id"] for e in page["events"]] == ["OUT-15", "OUT-2"]


def test_declaration_correction_events_sorted_with_basis(client):
    _build_line(client)
    cursor = _cursor(client)
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    page = _events(client, cursor)
    # 仅结论/依据确有变化的批次：RM-OAT 自身 + 下游 OUT-2（OUT-1 不依赖 RM-OAT）
    assert [e["batch_id"] for e in page["events"]] == ["OUT-2", "RM-OAT"]
    assert all(e["op"] == "declaration_correction" for e in page["events"])
    by_batch = {e["batch_id"]: e for e in page["events"]}
    rm = by_batch["RM-OAT"]
    assert rm["before"]["basis"]["material_declarations"][0]["version"] == 1
    assert rm["after"]["basis"]["material_declarations"][0]["version"] == 2
    out2 = by_batch["OUT-2"]
    assert out2["before"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert out2["after"]["conclusion"]["allergens"] == ["gluten", "milk", "sesame"]


def test_cleaning_events_only_when_trace_changes(client):
    _build_line(client)
    cursor = _cursor(client)
    # 未验证清洗不改变实时溯源 -> 无事件
    client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": False})
    assert _events(client, cursor)["events"] == []
    # 窗口外的已验证清洗不改变实时溯源 -> 无事件
    client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T12:00:00Z",
        "verified": True})
    assert _events(client, cursor)["events"] == []
    # 窗口内的已验证清洗 -> 残留清除，OUT-2 产生事件
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    page = _events(client, cursor)
    assert [e["batch_id"] for e in page["events"]] == ["OUT-2"]
    ev = page["events"][0]
    assert ev["op"] == "cleaning_register" and ev["op_ref"] == {"cleaning_id": cid}
    assert ev["before"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert ev["after"]["conclusion"]["allergens"] == ["gluten"]
    assert ev["after"]["basis"]["cleanings"][0]["id"] == cid
    # 撤销 -> 残留恢复，再次产生事件
    cursor = _cursor(client)
    client.post(f"/cleanings/{cid}/revoke")
    page = _events(client, cursor)
    assert [e["batch_id"] for e in page["events"]] == ["OUT-2"]
    ev = page["events"][0]
    assert ev["op"] == "cleaning_revocation"
    assert ev["after"]["conclusion"]["allergens"] == ["gluten", "milk"]
    assert ev["after"]["basis"]["cleanings"] == []


def test_label_issuance_produces_no_events(client):
    _build_line(client)
    cursor = _cursor(client)
    assert client.post("/batches/OUT-2/labels", json={}).status_code == 201
    assert _events(client, cursor)["events"] == []


def test_preview_dry_run_no_events_commit_produces(client):
    _build_line(client)
    cursor = _cursor(client)
    prev = client.post("/materials/RM-OAT/declarations/preview",
                       json={"allergens": ["gluten", "sesame"]}).json()
    # 仅预演不产事件
    assert _events(client, cursor)["events"] == []
    # 凭据提交成功 -> 与直接更正同类型事件，via 标记来源
    assert client.post(f"/previews/{prev['preview_id']}/commit").status_code == 200
    page = _events(client, cursor)
    assert [e["batch_id"] for e in page["events"]] == ["OUT-2", "RM-OAT"]
    assert all(e["op"] == "declaration_correction" for e in page["events"])
    assert all(e["op_ref"]["via"] == "preview_commit" for e in page["events"])
    # 清洗撤销的预演提交同样产事件
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    cursor = _cursor(client)
    prev = client.post(f"/cleanings/{cid}/revoke/preview").json()
    assert _events(client, cursor)["events"] == []
    client.post(f"/previews/{prev['preview_id']}/commit")
    page = _events(client, cursor)
    assert [e["batch_id"] for e in page["events"]] == ["OUT-2"]
    assert page["events"][0]["op"] == "cleaning_revocation"
    assert page["events"][0]["op_ref"]["via"] == "preview_commit"


def test_failed_writes_produce_no_events(client):
    _build_line(client)
    cursor = _cursor(client)
    # 各类被拒绝的写入：时序/重叠/重复批次/未知资源/重复撤销
    assert processing(client, "P-BAD", "EQ-1",
                      "2026-01-02T08:30:00Z", "2026-01-02T09:30:00Z",
                      ["RM-OAT"], ["OUT-X"]).status_code == 409
    assert material(client, "RM-MILK").status_code == 409
    assert client.post("/cleanings/999/revoke").status_code == 404
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    client.post(f"/cleanings/{cid}/revoke")
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 409
    cursor2 = _cursor(client)
    assert client.post(f"/previews/bogus/commit").status_code == 409
    # 只有成功的清洗登记与撤销产生了事件，失败写入均无事件
    page = _events(client, cursor)
    assert [(e["op"], e["batch_id"]) for e in page["events"]] == [
        ("cleaning_register", "OUT-2"), ("cleaning_revocation", "OUT-2")]
    assert _events(client, cursor2)["events"] == []


def test_pagination_fixed_upper_bound(client):
    for i in range(5):
        material(client, f"RM-{i}", allergens=[f"a{i}"])
    first = _events(client, 0, limit=2)
    assert [e["batch_id"] for e in first["events"]] == ["RM-0", "RM-1"]
    assert first["has_more"] is True
    upper = first["upper"]
    # 分页进行中又有新写入：固定上界使其不进入本次读取
    material(client, "RM-NEW", allergens=["x"])
    second = _events(client, first["next_cursor"], limit=2, upper=upper)
    assert [e["batch_id"] for e in second["events"]] == ["RM-2", "RM-3"]
    assert second["upper"] == upper
    third = _events(client, second["next_cursor"], limit=2, upper=upper)
    assert [e["batch_id"] for e in third["events"]] == ["RM-4"]
    assert third["has_more"] is False
    assert third["latest"] > third["upper"]  # 提示期间有新写入
    # 新一轮（不传 upper）能读到 RM-NEW
    fourth = _events(client, third["next_cursor"])
    assert [e["batch_id"] for e in fourth["events"]] == ["RM-NEW"]
    assert fourth["has_more"] is False


def test_invalid_cursor_rejected(client):
    material(client, "RM-1")
    assert client.get("/changes/events", params={"cursor": -1}).status_code == 400
    assert client.get("/changes/events", params={"cursor": 999}).status_code == 400
    assert client.get("/changes/events",
                      params={"cursor": 0, "upper": 999}).status_code == 400
    assert client.get("/changes/events",
                      params={"cursor": 1, "upper": 0}).status_code == 400
    assert client.get("/changes/events", params={"cursor": "abc"}).status_code == 422
    assert client.get("/changes/events", params={"limit": 0}).status_code == 422


def test_events_replay_matches_snapshot(client):
    _build_line(client)
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    client.post(f"/cleanings/{cid}/revoke")
    # 从游标 0 重放全部事件，最终状态应与初始化快照一致
    state = {}
    cursor = 0
    while True:
        page = _events(client, cursor, limit=2)
        for ev in page["events"]:
            state[ev["batch_id"]] = ev["after"]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    snap = client.get("/changes/snapshot").json()
    assert set(state) == {b["batch_id"] for b in snap["batches"]}
    for b in snap["batches"]:
        assert state[b["batch_id"]]["conclusion"] == b["conclusion"]
        assert state[b["batch_id"]]["basis"] == b["basis"]
    # 快照游标之后无事件
    assert _events(client, snap["cursor"])["events"] == []
