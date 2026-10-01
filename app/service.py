"""核心业务逻辑：登记、校验（时序/可用性/重叠/来源环）、溯源计算、标签签发、变更预演。

溯源模型
--------
- 原料批次：当前有效声明（最新版本）给出过敏原集合与待确认状态。
- 加工产出：继承全部投入批次的过敏原/待确认，并继承同一设备上一次加工的残留；
  若两次加工之间存在“已验证且未撤销”的清洗，则残留被清除。
- 来源路径：从声明起点到目标批次的链路，逐步标注经由哪个加工、
  是“投入(input)”还是“设备残留(equipment_residue)”。

预演模型
--------
声明更正/清洗撤销正式落库前可先预演：在内存视图上叠加“假设变更”
（decl_overrides / extra_revoked_cleanings）重算，不落库任何业务数据；
预演返回带凭据的差异报告，提交时核对影响本次差异的记录是否仍与预演时一致，
一致才原子应用，否则 409 拒绝并要求重新预演。
"""
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone

from .db import read_tx, write_tx


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BadRequest(Exception):
    pass


# ---------------------------------------------------------------- 时间工具

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_store(dt: datetime) -> str:
    """归一化为 UTC ISO 字符串入库（格式一致，可按字符串比较）。"""
    return dt.astimezone(timezone.utc).isoformat()


def from_store(s: str) -> datetime:
    return datetime.fromisoformat(s)


def iso_z(s: str) -> str:
    """展示用：+00:00 -> Z"""
    return s.replace("+00:00", "Z")


# ---------------------------------------------------------------- 批次查询

def _material(conn, batch_id: str):
    return conn.execute(
        "SELECT * FROM material_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def _output_of(conn, batch_id: str):
    return conn.execute(
        "SELECT * FROM processing_outputs WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def batch_kind(conn, batch_id: str):
    """返回 ('material', row) / ('product', processing_row) / (None, None)。"""
    m = _material(conn, batch_id)
    if m:
        return "material", m
    out = _output_of(conn, batch_id)
    if out:
        proc = conn.execute(
            "SELECT * FROM processings WHERE processing_id = ?",
            (out["processing_id"],),
        ).fetchone()
        return "product", proc
    return None, None


def _available_at(conn, batch_id: str) -> datetime:
    """批次可用时间：原料=首次登记时间；加工产出=产出加工结束时间。"""
    kind, row = batch_kind(conn, batch_id)
    if kind == "material":
        return from_store(row["created_at"])
    if kind == "product":
        return from_store(row["end_utc"])
    raise NotFound(f"未知批次: {batch_id}")


def _current_declaration(conn, batch_id: str):
    return conn.execute(
        "SELECT * FROM material_declarations WHERE batch_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (batch_id,),
    ).fetchone()


# ---------------------------------------------------------------- 原料登记

def create_material(conn: sqlite3.Connection, data) -> dict:
    declared_at = to_store(data.declared_at) if data.declared_at else to_store(now_utc())
    with write_tx(conn):
        kind, _ = batch_kind(conn, data.batch_id)
        if kind:
            raise Conflict(f"批次 {data.batch_id} 已存在（{kind}），不能重复登记")
        releases = _all_releases(conn)
        before_views = _snapshot_views(conn, releases)
        conn.execute(
            "INSERT INTO material_batches (batch_id, created_at) VALUES (?, ?)",
            (data.batch_id, declared_at),
        )
        conn.execute(
            "INSERT INTO material_declarations (batch_id, version, allergens, pending, declared_at)"
            " VALUES (?, 1, ?, ?, ?)",
            (data.batch_id, json.dumps(data.allergens), int(data.pending), declared_at),
        )
        _record_events(conn, "material_register",
                       {"batch_id": data.batch_id, "version": 1}, before_views,
                       before_releases=releases)
    return get_material(conn, data.batch_id)


def add_declaration(conn: sqlite3.Connection, batch_id: str, data) -> dict:
    declared_at = to_store(data.declared_at) if data.declared_at else to_store(now_utc())
    with write_tx(conn):
        if not _material(conn, batch_id):
            raise NotFound(f"未知原料批次: {batch_id}")
        cur = _current_declaration(conn, batch_id)
        releases = _all_releases(conn)
        before_views = _snapshot_views(conn, releases)
        conn.execute(
            "INSERT INTO material_declarations (batch_id, version, allergens, pending, declared_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (batch_id, cur["version"] + 1, json.dumps(data.allergens),
             int(data.pending), declared_at),
        )
        _record_events(conn, "declaration_correction",
                       {"batch_id": batch_id, "new_version": cur["version"] + 1,
                        "via": "direct"}, before_views, before_releases=releases)
    return get_material(conn, batch_id)


def get_material(conn: sqlite3.Connection, batch_id: str) -> dict:
    m = _material(conn, batch_id)
    if not m:
        raise NotFound(f"未知原料批次: {batch_id}")
    decl = _current_declaration(conn, batch_id)
    history = conn.execute(
        "SELECT COUNT(*) AS n FROM material_declarations WHERE batch_id = ?", (batch_id,)
    ).fetchone()["n"]
    return {
        "batch_id": batch_id,
        "kind": "material",
        "allergens": json.loads(decl["allergens"]),
        "pending": bool(decl["pending"]),
        "version": decl["version"],
        "declared_at": iso_z(decl["declared_at"]),
        "declaration_versions": history,
    }


def list_materials(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT batch_id FROM material_batches ORDER BY batch_id").fetchall()
    return [get_material(conn, r["batch_id"]) for r in rows]


# ---------------------------------------------------------------- 加工登记

def create_processing(conn: sqlite3.Connection, data) -> dict:
    start = to_store(data.start_utc)
    end = to_store(data.end_utc)
    with write_tx(conn):
        # 1) 时序有效
        if not data.start_utc < data.end_utc:
            raise BadRequest("无效时序：end_utc 必须晚于 start_utc")
        if conn.execute(
            "SELECT 1 FROM processings WHERE processing_id = ?", (data.processing_id,)
        ).fetchone():
            raise Conflict(f"加工批次 {data.processing_id} 已存在")

        # 2) 来源环：产出不得同时是投入（自环）；并做通用可达性检查
        loop = set(data.inputs) & set(data.outputs)
        if loop:
            raise Conflict(f"批次来源成环：{sorted(loop)} 既是投入又是产出")
        _check_no_cycle(conn, data.inputs, data.outputs)

        # 3) 产出批次必须是全新批次（一个批次只能有一个来源）
        for out_id in data.outputs:
            kind, _ = batch_kind(conn, out_id)
            if kind:
                raise Conflict(f"产出批次 {out_id} 已存在（{kind}），来源冲突")

        # 4) 投入必须存在且在加工开始前可用
        for in_id in data.inputs:
            try:
                avail = _available_at(conn, in_id)
            except NotFound:
                raise BadRequest(f"投入批次 {in_id} 不存在")
            if avail > data.start_utc:
                raise BadRequest(
                    f"投入批次 {in_id} 在加工开始前不可用"
                    f"（可用时间 {iso_z(to_store(avail))} 晚于开始时间 {iso_z(start)}）"
                )

        # 5) 同设备加工不得重叠
        for row in conn.execute(
            "SELECT processing_id, start_utc, end_utc FROM processings WHERE equipment_id = ?",
            (data.equipment_id,),
        ):
            if start < row["end_utc"] and row["start_utc"] < end:
                raise Conflict(
                    f"设备 {data.equipment_id} 在 [{iso_z(row['start_utc'])}, {iso_z(row['end_utc'])}) "
                    f"已有加工 {row['processing_id']}，时间重叠"
                )

        # 6) 封存约束（校验与登记同一事务，失败回滚不留加工）：
        #    封存批次不得作为新加工投入；若设备残留会使本次产出受封存影响
        #    （上一次加工产出被封存且两次加工间无有效清洗截断），同样拒绝。
        releases = _all_releases(conn)
        held_inputs = [i for i in data.inputs
                       if releases.get(i, _empty_release())["held"]]
        if held_inputs:
            raise Conflict(
                f"封存批次不得作为新加工投入: {sorted(held_inputs)}")
        held_residue = _residue_held_output(
            conn, data.equipment_id, start, releases)
        if held_residue:
            raise Conflict(
                f"设备 {data.equipment_id} 的残留来自封存批次 {held_residue}"
                f"（与本次加工之间无有效清洗截断），登记将使新加工产出受封存影响")

        before_views = _snapshot_views(conn, releases)
        conn.execute(
            "INSERT INTO processings (processing_id, equipment_id, start_utc, end_utc)"
            " VALUES (?, ?, ?, ?)",
            (data.processing_id, data.equipment_id, start, end),
        )
        for in_id in data.inputs:
            conn.execute(
                "INSERT INTO processing_inputs (processing_id, batch_id) VALUES (?, ?)",
                (data.processing_id, in_id),
            )
        for out_id in data.outputs:
            conn.execute(
                "INSERT INTO processing_outputs (processing_id, batch_id) VALUES (?, ?)",
                (data.processing_id, out_id),
            )
        _record_events(conn, "processing_register",
                       {"processing_id": data.processing_id}, before_views,
                       before_releases=releases)
    return get_processing(conn, data.processing_id)


def _check_no_cycle(conn, inputs: list[str], outputs: list[str],
                    ignore_processing: str | None = None) -> None:
    """通用成环检查：在现有“投入->产出”批次图上，若任一新产出可到达任一投入，
    则新增/替换后的边会成环，拒绝。（产出为全新批次时该检查恒通过，作为防御保留。）

    ignore_processing：加工投入纠正时传入被纠正加工号——构边时排除该加工的
    旧投入边（这些边即将被整表替换，不应参与成环判定）。
    """
    adj: dict[str, list[str]] = {}
    for p in conn.execute("SELECT processing_id FROM processings"):
        pid = p["processing_id"]
        if pid == ignore_processing:
            continue
        ins = [r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_inputs WHERE processing_id = ?", (pid,))]
        outs = [r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_outputs WHERE processing_id = ?", (pid,))]
        for i in ins:
            adj.setdefault(i, []).extend(outs)
    targets = set(inputs)
    for o in outputs:
        stack, seen = [o], set()
        while stack:
            node = stack.pop()
            if node in targets:
                raise Conflict(f"批次来源成环：产出 {o} 是投入 {node} 的上游")
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adj.get(node, []))


def get_processing(conn: sqlite3.Connection, processing_id: str) -> dict:
    proc = conn.execute(
        "SELECT * FROM processings WHERE processing_id = ?", (processing_id,)
    ).fetchone()
    if not proc:
        raise NotFound(f"未知加工批次: {processing_id}")
    ins = [r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM processing_inputs WHERE processing_id = ? ORDER BY batch_id",
        (processing_id,))]
    outs = [r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM processing_outputs WHERE processing_id = ? ORDER BY batch_id",
        (processing_id,))]
    return {
        "processing_id": proc["processing_id"],
        "equipment_id": proc["equipment_id"],
        "start_utc": iso_z(proc["start_utc"]),
        "end_utc": iso_z(proc["end_utc"]),
        "inputs": ins,
        "outputs": outs,
    }


def list_processings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT processing_id FROM processings ORDER BY start_utc").fetchall()
    return [get_processing(conn, r["processing_id"]) for r in rows]


# ---------------------------------------------------------------- 加工投入纠正

def correct_processing_inputs(conn: sqlite3.Connection, processing_id: str,
                              data) -> dict:
    """纠正已登记加工的投入列表（整表替换）。

    原加工号、设备、起止时段与产出批次保持原值；新投入须非空、无重复、
    均已登记且在加工开始前可用，替换后的来源关系不得成环。校验与替换在同一
    写事务内完成，随后按现有规则重算全部相关批次的实时结论（过敏原/待确认/
    来源路径/依据，含后续设备残留传播）并仅为实际变化的批次按批次号顺序
    记录事件；投入集合未变时不替换、不产事件。已签发标签快照不受影响。
    """
    new_inputs = list(data.inputs)
    with write_tx(conn):
        proc = conn.execute(
            "SELECT * FROM processings WHERE processing_id = ?", (processing_id,)
        ).fetchone()
        if not proc:
            raise NotFound(f"未知加工批次: {processing_id}")

        start = from_store(proc["start_utc"])
        outputs = [r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_outputs WHERE processing_id = ?",
            (processing_id,))]
        old_inputs = [r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_inputs WHERE processing_id = ?",
            (processing_id,))]

        # 投入集合（多重集合）未变 -> 幂等返回，不替换、不产事件
        if sorted(old_inputs) == sorted(new_inputs):
            return get_processing(conn, processing_id)

        # 1) 自环：本加工产出不得同时是新投入
        loop = set(new_inputs) & set(outputs)
        if loop:
            raise Conflict(f"批次来源成环：{sorted(loop)} 既是投入又是产出")

        # 2) 新投入必须已登记且在加工开始前可用（设备/时段不变，沿用原开始时间）
        for in_id in new_inputs:
            try:
                avail = _available_at(conn, in_id)
            except NotFound:
                raise BadRequest(f"投入批次 {in_id} 不存在")
            if avail > start:
                raise BadRequest(
                    f"投入批次 {in_id} 在加工开始前不可用"
                    f"（可用时间 {iso_z(to_store(avail))} 晚于开始时间 {proc['start_utc']}）"
                )

        # 3) 通用成环检查：排除本加工的旧投入边（即将被替换），
        #    校验“替换后”的来源图中本加工产出不可到达任一新投入
        _check_no_cycle(conn, new_inputs, outputs, ignore_processing=processing_id)

        # 校验通过：替换投入（原加工号/设备/时段/产出保持原值）。
        # 封存批次允许作为纠正后的投入（“投入纠正…后同步重算封存状态”）：
        # 替换后重算，本加工产出可能因此进入/解除封存。
        releases = _all_releases(conn)
        before_views = _snapshot_views(conn, releases)
        conn.execute(
            "DELETE FROM processing_inputs WHERE processing_id = ?", (processing_id,))
        for in_id in new_inputs:
            conn.execute(
                "INSERT INTO processing_inputs (processing_id, batch_id) VALUES (?, ?)",
                (processing_id, in_id),
            )
        # 全量重算对比：仅为结论/来源路径/依据/封存状态实际变化的批次按批次号顺序
        # 落事件，设备残留沿后续加工继续传播的批次同样自动计入。
        _record_events(conn, "processing_input_correction",
                       {"processing_id": processing_id}, before_views,
                       before_releases=releases)
    return get_processing(conn, processing_id)


# ---------------------------------------------------------------- 清洗

def create_cleaning(conn: sqlite3.Connection, data) -> dict:
    with write_tx(conn):
        releases = _all_releases(conn)
        before_views = _snapshot_views(conn, releases)
        cur = conn.execute(
            "INSERT INTO cleanings (equipment_id, cleaned_at, verified, revoked)"
            " VALUES (?, ?, ?, 0)",
            (data.equipment_id, to_store(data.cleaned_at), int(data.verified)),
        )
        cid = cur.lastrowid
        # 未验证清洗、不在任何残留窗口内的清洗不改变实时溯源 -> 对比后无事件；
        # 有效清洗截断残留传播，可能同时解除下游封存（release 变化计入）。
        _record_events(conn, "cleaning_register", {"cleaning_id": cid}, before_views,
                       before_releases=releases)
    return get_cleaning(conn, cid)


def get_cleaning(conn: sqlite3.Connection, cleaning_id: int) -> dict:
    row = conn.execute("SELECT * FROM cleanings WHERE id = ?", (cleaning_id,)).fetchone()
    if not row:
        raise NotFound(f"未知清洗记录: {cleaning_id}")
    return {
        "id": row["id"],
        "equipment_id": row["equipment_id"],
        "cleaned_at": iso_z(row["cleaned_at"]),
        "verified": bool(row["verified"]),
        "revoked": bool(row["revoked"]),
        "effective": bool(row["verified"] and not row["revoked"]),
    }


def list_cleanings(conn: sqlite3.Connection, equipment_id: str | None = None) -> list[dict]:
    if equipment_id:
        rows = conn.execute(
            "SELECT id FROM cleanings WHERE equipment_id = ? ORDER BY cleaned_at",
            (equipment_id,)).fetchall()
    else:
        rows = conn.execute("SELECT id FROM cleanings ORDER BY cleaned_at").fetchall()
    return [get_cleaning(conn, r["id"]) for r in rows]


def revoke_cleaning(conn: sqlite3.Connection, cleaning_id: int) -> dict:
    with write_tx(conn):
        row = conn.execute("SELECT * FROM cleanings WHERE id = ?", (cleaning_id,)).fetchone()
        if not row:
            raise NotFound(f"未知清洗记录: {cleaning_id}")
        if row["revoked"]:
            raise Conflict(f"清洗记录 {cleaning_id} 已撤销")
        releases = _all_releases(conn)
        before_views = _snapshot_views(conn, releases)
        conn.execute("UPDATE cleanings SET revoked = 1 WHERE id = ?", (cleaning_id,))
        _record_events(conn, "cleaning_revocation",
                       {"cleaning_id": cleaning_id, "via": "direct"}, before_views,
                       before_releases=releases)
    return get_cleaning(conn, cleaning_id)


# ---------------------------------------------------------------- 调查封存传播
#
# 封存传播与过敏原内容无关：只要调查生效，根批次及其沿“加工投入链 + 同设备相邻
# 加工残留链（有效清洗截断）”可达的全部批次一律视为受封存影响。传播结果实时
# 重算（投入纠正、清洗登记/撤销、调查登记/解除后自动更新），多个调查叠加时
# 每批次保留各自调查号与传播路径，解除其中一个只移除该调查自身的影响。

def _empty_release() -> dict:
    return {"held": False, "investigations": []}


def _effective_cleaning_between(conn, equipment_id: str,
                                 prev_end: str, start: str,
                                 extra_revoked_cleanings: set | None = None):
    """两次相邻加工之间是否存在“已验证且未撤销”的清洗（含端点时刻）。
    extra_revoked_cleanings：预演用，额外视为已撤销的清洗。"""
    cleaning = conn.execute(
        "SELECT * FROM cleanings WHERE equipment_id = ?"
        " AND verified = 1 AND revoked = 0"
        " AND cleaned_at >= ? AND cleaned_at <= ?"
        " ORDER BY cleaned_at LIMIT 1",
        (equipment_id, prev_end, start),
    ).fetchone()
    if cleaning and extra_revoked_cleanings \
            and cleaning["id"] in extra_revoked_cleanings:
        return None
    return cleaning


def _build_hold_graph(conn: sqlite3.Connection,
                      extra_revoked_cleanings: set | None = None):
    """构造封存传播用的批次 DAG：
    - 节点：全部批次（原料/加工产出）；
    - 投入边：加工投入批次 -> 本加工各产出（step via=input）；
    - 残留边：若同设备“上一次加工”（end_utc <= 本次 start）与本次之间无有效
      清洗，则上一次加工各产出 -> 本加工各产出（via=equipment_residue）。
    同一对批次可能同时存在投入边与残留边（语义不同，分别保留）。
    返回 (topo_order, incoming, all_batches)：
    incoming[bid] = [(pred_batch_id, step_dict), ...]（投入边在前、残留边在后）。
    """
    procs = {r["processing_id"]: r for r in conn.execute(
        "SELECT * FROM processings")}
    inputs_of: dict[str, list[str]] = {}
    outputs_of: dict[str, list[str]] = {}
    for pid in procs:
        inputs_of[pid] = sorted(r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_inputs WHERE processing_id = ?",
            (pid,)))
        outputs_of[pid] = sorted(r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_outputs WHERE processing_id = ?",
            (pid,)))

    batches = set(_all_batch_ids(conn))
    incoming: dict[str, list[tuple[str, dict]]] = {b: [] for b in batches}
    adj: dict[str, set[str]] = {b: set() for b in batches}
    indeg: dict[str, int] = {b: 0 for b in batches}

    for pid in sorted(procs):
        proc = procs[pid]
        outs = outputs_of[pid]
        preds: list[tuple[str, dict]] = []
        for in_id in inputs_of[pid]:
            preds.append((in_id, {"batch_id": None, "processing_id": pid,
                                 "via": "input"}))
        prev = conn.execute(
            "SELECT * FROM processings WHERE equipment_id = ? AND end_utc <= ?"
            " ORDER BY end_utc DESC LIMIT 1",
            (proc["equipment_id"], proc["start_utc"]),
        ).fetchone()
        residue = True
        if prev:
            cleaning = _effective_cleaning_between(
                conn, proc["equipment_id"], prev["end_utc"], proc["start_utc"],
                extra_revoked_cleanings)
            if cleaning:
                residue = False
        else:
            residue = False
        if residue:
            for rout in outputs_of[prev["processing_id"]]:
                preds.append((rout, {"batch_id": None, "processing_id": pid,
                                    "via": "equipment_residue",
                                    "equipment_id": proc["equipment_id"]}))
        for out_id in outs:
            batches.add(out_id)
            incoming.setdefault(out_id, [])
            pred_nodes = set()
            for pred, step_tmpl in preds:
                step = dict(step_tmpl)
                step["batch_id"] = out_id
                incoming[out_id].append((pred, step))
                pred_nodes.add(pred)
            for pred in pred_nodes:
                adj.setdefault(pred, set()).add(out_id)
                adj.setdefault(out_id, set())
                # 新产出节点可能在遍历到其加工之后才首次出现，需显式补零
                indeg.setdefault(pred, 0)
                indeg[out_id] = indeg.get(out_id, 0) + 1

    # 拓扑排序（Kahn）：投入/残留边均由时序约束保证无环；防御性兜底。
    ready = sorted(b for b in batches if indeg.get(b, 0) == 0)
    topo: list[str] = []
    while ready:
        node = ready.pop(0)
        topo.append(node)
        for nxt in sorted(adj.get(node, ())):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if len(topo) != len(batches):
        raise Conflict("封存传播图存在环（不应发生：时序约束已排除）")
    return topo, incoming, batches


def _open_investigation_rows(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM investigations WHERE status = 'open'"
        " ORDER BY opened_at, investigation_id").fetchall()


def _all_releases(conn: sqlite3.Connection,
                 extra_revoked_cleanings: set | None = None) -> dict:
    """实时计算全部批次的封存状态：
    {batch_id: {"held": bool, "investigations": [
        {"investigation_id", "root_batch_id", "reason", "opened_at",
         "paths": [[step, ...]]}, ...]}}
    每个生效调查沿传播 DAG 枚举根->批次的全部路径（枚举在拓扑序上进行）。
    extra_revoked_cleanings：预演用（清洗撤销假设下残留边恢复，封存随之重算）。
    """
    topo, incoming, batches = _build_hold_graph(conn, extra_revoked_cleanings)
    rows = _open_investigation_rows(conn)
    releases = {b: _empty_release() for b in batches}
    for row in rows:
        iid, root = row["investigation_id"], row["batch_id"]
        if root not in releases:  # 防御：根批次被删等异常情况
            continue
        root_step = {"batch_id": root, "via": "investigation_root",
                     "investigation_id": iid}
        paths_of: dict[str, list] = {}
        per_batch: dict[str, list] = {}
        for node in topo:
            if node == root:
                paths = [[root_step]]
            elif node in incoming and incoming[node]:
                paths = []
                for pred, step in incoming[node]:
                    paths.extend(p + [dict(step)] for p in paths_of.get(pred, []))
            else:
                paths = []
            paths_of[node] = paths
            if paths:
                canon = sorted(json.dumps(p, ensure_ascii=False, sort_keys=True)
                               for p in paths)
                per_batch[node] = [json.loads(s) for s in canon]
        for bid, paths in per_batch.items():
            releases.setdefault(bid, _empty_release())
            releases[bid]["held"] = True
            releases[bid]["investigations"].append({
                "investigation_id": iid,
                "root_batch_id": root,
                "reason": row["reason"],
                "opened_at": iso_z(row["opened_at"]),
                "paths": paths,
            })
    for rel in releases.values():
        rel["investigations"].sort(key=lambda e: e["investigation_id"])
    return releases


def _residue_held_output(conn: sqlite3.Connection, equipment_id: str,
                        start_utc: str, releases: dict) -> str | None:
    """新加工在 start_utc 时刻是否会经设备残留继承到封存批次：
    返回被封存的上一次加工产出批次号；被有效清洗截断或无前置加工时返回 None。"""
    prev = conn.execute(
        "SELECT * FROM processings WHERE equipment_id = ? AND end_utc <= ?"
        " ORDER BY end_utc DESC LIMIT 1",
        (equipment_id, start_utc),
    ).fetchone()
    if not prev:
        return None
    if _effective_cleaning_between(
            conn, equipment_id, prev["end_utc"], start_utc):
        return None
    for r in conn.execute(
        "SELECT batch_id FROM processing_outputs WHERE processing_id = ?",
        (prev["processing_id"],),
    ):
        if releases.get(r["batch_id"], _empty_release())["held"]:
            return r["batch_id"]
    return None


# ---------------------------------------------------------------- 溯源计算

def compute_trace(conn: sqlite3.Connection, batch_id: str,
                  decl_overrides: dict | None = None,
                  extra_revoked_cleanings: set | None = None):
    """按当前有效记录实时计算批次结论。

    decl_overrides: {batch_id: {"allergens": [...], "pending": bool,
                                "version": int, "declared_at": str}}
        预演用：用“拟更正后的新版本声明”覆盖该原料的当前有效声明（不落库）。
    extra_revoked_cleanings: set[int]
        预演用：把这些清洗记录视为已撤销（不落库）。

    返回 (result, evidence)：
    - result: {"allergens": {过敏原: [路径...]}, "pending_paths": [路径...]}
    - evidence: 计算依据（命中的原料声明版本、赖以清除残留的清洗记录）
    路径 = 步骤列表，步骤形如：
      {"batch_id": "RM-1", "via": "declaration"}
      {"batch_id": "OUT-1", "processing_id": "P1", "via": "input"}
      {"batch_id": "OUT-2", "processing_id": "P2", "via": "equipment_residue", "equipment_id": "EQ-1"}
    """
    decl_overrides = decl_overrides or {}
    extra_revoked_cleanings = extra_revoked_cleanings or set()
    evidence = {"materials": {}, "cleanings": {}}
    memo: dict[str, dict] = {}

    def trace(bid: str) -> dict:
        if bid in memo:
            return memo[bid]
        if _material(conn, bid):
            if bid in decl_overrides:
                ov = decl_overrides[bid]
                allergens = list(ov["allergens"])
                pending = int(ov["pending"])
                version = ov["version"]
                declared_at = ov["declared_at"]
            else:
                decl = _current_declaration(conn, bid)
                allergens = json.loads(decl["allergens"])
                pending = decl["pending"]
                version = decl["version"]
                declared_at = decl["declared_at"]
            evidence["materials"][bid] = {
                "batch_id": bid,
                "version": version,
                "allergens": allergens,
                "pending": bool(pending),
                "declared_at": iso_z(declared_at),
            }
            step = {"batch_id": bid, "via": "declaration"}
            res = {
                "allergens": {a: [[step]] for a in allergens},
                "pending_paths": [[step]] if pending else [],
            }
        else:
            out = _output_of(conn, bid)
            if not out:
                raise NotFound(f"未知批次: {bid}")
            pid = out["processing_id"]
            proc = conn.execute(
                "SELECT * FROM processings WHERE processing_id = ?", (pid,)
            ).fetchone()
            res = {"allergens": {}, "pending_paths": []}

            def absorb(sub: dict, step: dict):
                for a, paths in sub["allergens"].items():
                    res["allergens"].setdefault(a, []).extend(
                        [p + [step] for p in paths])
                res["pending_paths"].extend([p + [step] for p in sub["pending_paths"]])

            # 1) 投入继承
            for r in conn.execute(
                "SELECT batch_id FROM processing_inputs WHERE processing_id = ?", (pid,)
            ):
                absorb(trace(r["batch_id"]),
                       {"batch_id": bid, "processing_id": pid, "via": "input"})

            # 2) 设备残留：上一次同设备加工，除非两次加工间存在有效清洗
            prev = conn.execute(
                "SELECT * FROM processings WHERE equipment_id = ? AND end_utc <= ?"
                " ORDER BY end_utc DESC LIMIT 1",
                (proc["equipment_id"], proc["start_utc"]),
            ).fetchone()
            if prev:
                cleaning = conn.execute(
                    "SELECT * FROM cleanings WHERE equipment_id = ?"
                    " AND verified = 1 AND revoked = 0"
                    " AND cleaned_at >= ? AND cleaned_at <= ?"
                    " ORDER BY cleaned_at LIMIT 1",
                    (proc["equipment_id"], prev["end_utc"], proc["start_utc"]),
                ).fetchone()
                if cleaning and cleaning["id"] not in extra_revoked_cleanings:
                    evidence["cleanings"][cleaning["id"]] = {
                        "id": cleaning["id"],
                        "equipment_id": cleaning["equipment_id"],
                        "cleaned_at": iso_z(cleaning["cleaned_at"]),
                        "verified": True,
                    }
                else:
                    for r in conn.execute(
                        "SELECT batch_id FROM processing_outputs WHERE processing_id = ?",
                        (prev["processing_id"],),
                    ):
                        absorb(trace(r["batch_id"]), {
                            "batch_id": bid,
                            "processing_id": pid,
                            "via": "equipment_residue",
                            "equipment_id": proc["equipment_id"],
                        })
        memo[bid] = res
        return res

    result = trace(batch_id)
    return result, evidence


def conclusion_view(result: dict) -> dict:
    """把 compute_trace 的 result 规整为对外一致的“结论”结构
    （trace 响应与标签快照共用，保证比对口径一致）。"""
    return {
        "pending": bool(result["pending_paths"]),
        "allergens": sorted(result["allergens"].keys()),
        "allergen_paths": {a: result["allergens"][a]
                           for a in sorted(result["allergens"])},
        "pending_paths": result["pending_paths"],
    }


def basis_view(evidence: dict) -> dict:
    return {
        "material_declarations": sorted(
            evidence["materials"].values(), key=lambda d: d["batch_id"]),
        "cleanings": sorted(
            evidence["cleanings"].values(), key=lambda d: d["id"]),
    }


def trace_view(conn: sqlite3.Connection, batch_id: str,
               decl_overrides: dict | None = None,
               extra_revoked_cleanings: set | None = None,
               releases: dict | None = None) -> dict:
    """完整实时视图：结论（含来源路径）+ 依据 + 封存状态。可叠加预演覆盖。"""
    result, evidence = compute_trace(
        conn, batch_id, decl_overrides, extra_revoked_cleanings)
    release = (releases or {}).get(batch_id, _empty_release())
    return {
        "conclusion": conclusion_view(result),
        "basis": basis_view(evidence),
        "release": release_view(release),
    }


def release_view(release: dict) -> dict:
    """对外封存状态视图（与 trace/事件/快照同口径）：
    held 为该批次是否被任一生效调查封存；investigations 给出仍生效的
    调查号及各自传播路径（封存与过敏原声明是否为空无关）。"""
    return {
        "held": bool(release.get("held")),
        "investigations": sorted(release.get("investigations", []),
                                key=lambda e: e["investigation_id"]),
    }


def trace_response(conn: sqlite3.Connection, batch_id: str) -> dict:
    kind, _ = batch_kind(conn, batch_id)
    if not kind:
        raise NotFound(f"未知批次: {batch_id}")
    releases = _all_releases(conn)
    view = trace_view(conn, batch_id, releases=releases)
    return {
        "batch_id": batch_id,
        "kind": kind,
        **view["conclusion"],
        "basis": view["basis"],
        "release": view["release"],
    }


# ---------------------------------------------------------------- 变更事件流
#
# 下游系统按游标增量消费溯源变化：每次业务写入在**同一事务**内先取全部批次
# 实时视图快照，写入后重算并对比，仅为“新批次（前态为空）”与“结论/来源路径/
# 依据确有变化”的批次落事件；同次操作的事件按批次号排序插入（seq 即有序）。
# 校验失败回滚、预演（dry-run）不落任何事件；未验证清洗、标签签发等不改变
# 实时溯源的操作经对比自然不产生事件。

def _snapshot_views(conn: sqlite3.Connection,
                    releases: dict | None = None) -> dict:
    """当前全部批次的实时视图（结论 + 依据 + 封存状态），{batch_id: view}。
    封存传播在一次操作内只重算一次，所有视图共用同一结果。"""
    if releases is None:
        releases = _all_releases(conn)
    return {bid: trace_view(conn, bid, releases=releases)
            for bid in _all_batch_ids(conn)}


def _record_events(conn: sqlite3.Connection, op: str, op_ref: dict,
                   before_views: dict,
                   before_releases: dict | None = None,
                   after_releases: dict | None = None) -> list[str]:
    """写事务内调用：重算全部批次视图并与 before_views 对比，为新批次与
    确有变化的批次按批次号顺序落事件。返回产生事件的批次号列表。
    封存状态（release）变化同样计入（investigation_open/lift 操作）。
    after_releases 显式给出时用于清洗撤销预演提交（撤销假设改变残留边）；
    其余情况在写入后按当前库状态实时重算，保证封存状态前后对比正确。"""
    if after_releases is None:
        after_releases = _all_releases(conn)
    after_views = _snapshot_views(conn, after_releases)
    changed = []
    for bid, after in after_views.items():
        before = before_views.get(bid)
        if before is None or _view_key(before) != _view_key(after):
            changed.append((bid, before, after))
    changed.sort(key=lambda item: item[0])  # 同次操作按批次号排序
    now = to_store(now_utc())
    ref = json.dumps(op_ref, ensure_ascii=False, sort_keys=True)
    for bid, before, after in changed:
        kind, _ = batch_kind(conn, bid)
        conn.execute(
            "INSERT INTO trace_events"
            " (op, op_ref, batch_id, kind, before_view, after_view, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (op, ref, bid, kind,
             json.dumps(before, ensure_ascii=False, sort_keys=True)
             if before is not None else None,
             json.dumps(after, ensure_ascii=False, sort_keys=True),
             now),
        )
    return [bid for bid, _, _ in changed]


def _max_event_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(seq) AS s FROM trace_events").fetchone()
    return row["s"] or 0


def _event_view(row) -> dict:
    return {
        "seq": row["seq"],
        "op": row["op"],
        "op_ref": json.loads(row["op_ref"]),
        "batch_id": row["batch_id"],
        "kind": row["kind"],
        "before": (json.loads(row["before_view"])
                   if row["before_view"] is not None else None),
        "after": json.loads(row["after_view"]),
        "recorded_at": iso_z(row["created_at"]),
    }


def changes_snapshot(conn: sqlite3.Connection) -> dict:
    """初始化读取：同一状态（读事务一致快照）下的现存批次实时结论与游标。
    之后以该游标调用事件接口即可不重不漏地接续增量变化。"""
    with read_tx(conn):
        cursor = _max_event_seq(conn)
        releases = _all_releases(conn)
        batches = []
        for bid in _all_batch_ids(conn):
            kind, _ = batch_kind(conn, bid)
            view = trace_view(conn, bid, releases=releases)
            batches.append({
                "batch_id": bid,
                "kind": kind,
                "conclusion": view["conclusion"],
                "basis": view["basis"],
                "release": view["release"],
            })
    return {"cursor": cursor, "batches": batches}


def list_change_events(conn: sqlite3.Connection, cursor: int, limit: int,
                       upper: int | None) -> dict:
    """按游标分页读取事件：返回 (cursor, upper] 内按 seq 升序的事件。

    upper 为本次分页会话的固定上界：首页缺省取当前最大序号，翻页时原样回传，
    分页期间的新写入（seq > upper）不会进入已开始的读取。无效游标拒绝。
    """
    with read_tx(conn):
        max_seq = _max_event_seq(conn)
        if cursor < 0 or cursor > max_seq:
            raise BadRequest(f"无效游标: {cursor}（当前有效范围 0..{max_seq}）")
        if upper is None:
            upper = max_seq
        elif upper < cursor or upper > max_seq:
            raise BadRequest(
                f"无效上界游标: {upper}（需满足 cursor <= upper <= {max_seq}）")
        rows = conn.execute(
            "SELECT * FROM trace_events WHERE seq > ? AND seq <= ?"
            " ORDER BY seq LIMIT ?",
            (cursor, upper, limit),
        ).fetchall()
        events = [_event_view(r) for r in rows]
        next_cursor = events[-1]["seq"] if events else cursor
    return {
        "events": events,
        "next_cursor": next_cursor,
        "upper": upper,
        "has_more": next_cursor < upper,
        "latest": max_seq,
    }


# ---------------------------------------------------------------- 标签签发

def issue_label(conn: sqlite3.Connection, batch_id: str, supersedes_id: int | None) -> dict:
    with write_tx(conn):
        kind, _ = batch_kind(conn, batch_id)
        if not kind:
            raise NotFound(f"未知批次: {batch_id}")
        # 封存批次不得签发新标签（旧标签快照不受影响、永不变更）
        release = _all_releases(conn).get(batch_id, _empty_release())
        if release["held"]:
            held_by = sorted(e["investigation_id"]
                             for e in release["investigations"])
            raise Conflict(
                f"批次 {batch_id} 已被调查封存（{held_by}），不得签发新标签")
        if supersedes_id is not None:
            old = conn.execute(
                "SELECT * FROM labels WHERE id = ?", (supersedes_id,)).fetchone()
            if not old:
                raise NotFound(f"被关联的旧标签不存在: {supersedes_id}")
            if old["batch_id"] != batch_id:
                raise BadRequest(
                    f"旧标签 {supersedes_id} 属于批次 {old['batch_id']}，"
                    f"不能关联到批次 {batch_id} 的新标签")
        # 签发时刻冻结结论与依据
        view = trace_view(conn, batch_id)
        snapshot = {
            "batch_id": batch_id,
            "conclusion": view["conclusion"],
            "basis": view["basis"],
        }
        cur = conn.execute(
            "INSERT INTO labels (batch_id, issued_at, supersedes_id, snapshot)"
            " VALUES (?, ?, ?, ?)",
            (batch_id, to_store(now_utc()), supersedes_id, json.dumps(snapshot)),
        )
        label_id = cur.lastrowid
    return get_label(conn, label_id)


def get_label(conn: sqlite3.Connection, label_id: int) -> dict:
    row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    if not row:
        raise NotFound(f"未知标签: {label_id}")
    snap = json.loads(row["snapshot"])
    return {
        "label_id": row["id"],
        "batch_id": row["batch_id"],
        "issued_at": iso_z(row["issued_at"]),
        "supersedes_id": row["supersedes_id"],
        "conclusion": snap["conclusion"],
        "basis": snap["basis"],
    }


def list_labels(conn: sqlite3.Connection, batch_id: str) -> list[dict]:
    kind, _ = batch_kind(conn, batch_id)
    if not kind:
        raise NotFound(f"未知批次: {batch_id}")
    rows = conn.execute(
        "SELECT id FROM labels WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    return [get_label(conn, r["id"]) for r in rows]


# ---------------------------------------------------------------- 变更预演
#
# 预演在“当前库状态”与“叠加假设变更后的状态”两个视图上重算全部批次，
# 不落库任何业务数据；差异报告含受影响批次的结论/路径/依据变化以及其已签发
# 标签相对两个实时结论的差异。预演同时生成凭据：指纹只纳入“影响本次差异”
# 的记录（声明版本、残留链路上的加工、残留判定窗口内的清洗、受影响批次的
# 已签发标签），无关记录的更新不会使凭据失效。

# 进程内存放凭据（单 worker 部署；容器/进程重启后凭据失效，需重新预演）。
_PREVIEWS: dict[str, dict] = {}
_PREVIEW_LIMIT = 1000


def _all_batch_ids(conn: sqlite3.Connection) -> list[str]:
    ids = {r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM material_batches")}
    ids.update(r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM processing_outputs"))
    return sorted(ids)


# ---------------- 规范化比对（路径按多重集合比较，不依赖运行期顺序）

def _canon_paths(paths: list) -> list[str]:
    return sorted(json.dumps(p, sort_keys=True, ensure_ascii=False) for p in paths)


def _canon_conclusion(c: dict) -> dict:
    return {
        "pending": bool(c["pending"]),
        "allergens": sorted(c["allergens"]),
        "allergen_paths": {a: _canon_paths(c["allergen_paths"].get(a, []))
                           for a in sorted(c["allergens"])},
        "pending_paths": _canon_paths(c.get("pending_paths", [])),
    }


def _canon_basis(b: dict) -> dict:
    return {
        "material_declarations": {
            d["batch_id"]: {"version": d["version"], "allergens": sorted(d["allergens"]),
                            "pending": bool(d["pending"]), "declared_at": d["declared_at"]}
            for d in b.get("material_declarations", [])},
        "cleanings": {d["id"]: {"equipment_id": d["equipment_id"],
                                "cleaned_at": d["cleaned_at"]}
                      for d in b.get("cleanings", [])},
    }


def _canon_release(rel: dict) -> dict:
    return {
        "held": bool(rel.get("held")),
        "investigations": {
            e["investigation_id"]: {
                "root_batch_id": e["root_batch_id"],
                "reason": e["reason"],
                "opened_at": e["opened_at"],
                "paths": sorted(
                    json.dumps(p, sort_keys=True, ensure_ascii=False)
                    for p in e.get("paths", [])),
            }
            for e in rel.get("investigations", [])},
    }


def _view_key(view: dict) -> str:
    payload = {
        "conclusion": _canon_conclusion(view["conclusion"]),
        "basis": _canon_basis(view["basis"]),
        "release": _canon_release(view.get("release", _empty_release())),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# ---------------- 差异构造

def _diff_conclusions(before: dict, after: dict) -> dict | None:
    cb, ca = before["conclusion"], after["conclusion"]
    pb = _canon_conclusion(cb)
    pa = _canon_conclusion(ca)
    if pb == pa:
        return None
    diff = {}
    if pb["allergens"] != pa["allergens"]:
        diff["allergens"] = {
            "before": pb["allergens"], "after": pa["allergens"],
            "added": sorted(set(pa["allergens"]) - set(pb["allergens"])),
            "removed": sorted(set(pb["allergens"]) - set(pa["allergens"])),
        }
    if pb["pending"] != pa["pending"]:
        diff["pending"] = {"before": pb["pending"], "after": pa["pending"]}
    if pb["allergen_paths"] != pa["allergen_paths"]:
        diff["allergen_paths"] = {
            "before": cb["allergen_paths"], "after": ca["allergen_paths"]}
    if pb["pending_paths"] != pa["pending_paths"]:
        diff["pending_paths"] = {
            "before": cb.get("pending_paths", []),
            "after": ca.get("pending_paths", [])}
    return diff or None


def _diff_basis(before: dict, after: dict) -> dict:
    bb, ba = _canon_basis(before["basis"]), _canon_basis(after["basis"])
    out: dict = {"material_declarations": {}, "cleanings": {}}
    mb, ma = bb["material_declarations"], ba["material_declarations"]
    for bid in sorted(set(mb) | set(ma)):
        if mb.get(bid) != ma.get(bid):
            out["material_declarations"][bid] = {
                "before": mb.get(bid), "after": ma.get(bid)}
    cb_, ca_ = bb["cleanings"], ba["cleanings"]
    for cid in sorted(set(cb_) | set(ca_)):
        if cb_.get(cid) != ca_.get(cid):
            out["cleanings"][str(cid)] = {
                "before": cb_.get(cid), "after": ca_.get(cid)}
    return out


def _diff_views(before: dict, after: dict) -> dict | None:
    """两个完整视图的差异（结论路径 + 依据）；完全一致返回 None。"""
    if _view_key(before) == _view_key(after):
        return None
    return {
        "conclusion": _diff_conclusions(before, after),
        "basis": _diff_basis(before, after),
    }


def _diff_label_against(label_snapshot_conclusion: dict,
                        live_view: dict) -> dict | None:
    """已签发标签的冻结结论相对某个实时结论的差异（无差异返回 None）。"""
    return _diff_conclusions(
        {"conclusion": label_snapshot_conclusion},
        {"conclusion": live_view["conclusion"]})


# ---------------- 凭据指纹（仅纳入影响本次差异的记录）

def _related_processings(conn, root_batches: set, equipment_ids: set) -> set[str]:
    """与本次变更“相关”的加工集合：
    - 相关设备上的全部加工（残留只在同设备相邻加工间传播）；
    - 消费根批次（及其下游产出）的加工，沿 投入->加工->产出 闭包扩展。
    无关设备、无关原料链上的新增加工不会进入集合，故不使凭据失效。
    """
    adj: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT processing_id, batch_id FROM processing_inputs"
    ):
        adj.setdefault(r["batch_id"], []).append(r["processing_id"])

    pids: set[str] = set()
    frontier = set(root_batches)
    for eq in equipment_ids:
        for r in conn.execute(
            "SELECT processing_id FROM processings WHERE equipment_id = ?", (eq,)
        ):
            pids.add(r["processing_id"])
    for pid in pids:  # 相关设备上加工的产出也作为下游传播起点
        for r in conn.execute(
            "SELECT batch_id FROM processing_outputs WHERE processing_id = ?", (pid,)
        ):
            frontier.add(r["batch_id"])

    while frontier:
        b = frontier.pop()
        for pid in adj.get(b, []):
            if pid not in pids:
                pids.add(pid)
                for r in conn.execute(
                    "SELECT batch_id FROM processing_outputs WHERE processing_id = ?",
                    (pid,),
                ):
                    frontier.add(r["batch_id"])
    return pids


def _collect_witnesses(conn: sqlite3.Connection, affected: dict,
                       root_batches: set, root_equipment: set,
                       forced_cleanings: set) -> dict:
    """抽取“影响本次差异”的记录指纹：
    - 受影响批次溯源依据中的原料声明（+ 根原料）；
    - 相关加工（含设备/时间窗/投入边，新增相关加工会使指纹变化）；
    - 相关加工残留窗口内“已验证且未撤销”的清洗（+ 拟撤销目标清洗）；
    - 受影响批次的已签发标签快照（受影响批次上新签发标签会使指纹变化）。
    """
    materials = set(root_batches)
    equipment = set(root_equipment)
    labels: dict[int, str] = {}

    for bid, views in affected.items():
        for phase in ("before", "after"):
            view = views[phase]
            materials.update(
                d["batch_id"] for d in view["basis"]["material_declarations"])
            for path_group in (list(view["conclusion"]["allergen_paths"].values())
                              + [view["conclusion"]["pending_paths"]]):
                for path in path_group:
                    for step in path:
                        if step.get("equipment_id"):
                            equipment.add(step["equipment_id"])
        for lab in conn.execute(
            "SELECT id, snapshot FROM labels WHERE batch_id = ? ORDER BY id",
            (bid,),
        ):
            labels[lab["id"]] = lab["snapshot"]

    pids = _related_processings(conn, root_batches, equipment)

    # 相关加工产出的实时溯源依据（声明版本）也纳入指纹：当预演差异为空时
    # （如残留链路上当前无过敏原），上游声明若在预演后更正、使撤销变得有影响，
    # 凭据仍能失效。
    for pid in pids:
        for r in conn.execute(
            "SELECT batch_id FROM processing_outputs WHERE processing_id = ?", (pid,)
        ):
            _, ev = compute_trace(conn, r["batch_id"])
            materials.update(ev["materials"].keys())

    witness = {
        "materials": {},
        "processings": {},
        "cleanings": {},
        "labels": {str(i): labels[i] for i in sorted(labels)},
        "investigations": {},
    }

    for bid in sorted(materials):
        m = _material(conn, bid)
        if not m:
            witness["materials"][bid] = None
            continue
        decl = _current_declaration(conn, bid)
        witness["materials"][bid] = {
            "created_at": m["created_at"],
            "version": decl["version"],
            "allergens": sorted(json.loads(decl["allergens"])),
            "pending": int(decl["pending"]),
            "declared_at": decl["declared_at"],
        }

    cleaning_ids = set(forced_cleanings)
    for pid in sorted(pids):
        p = conn.execute(
            "SELECT processing_id, equipment_id, start_utc, end_utc"
            " FROM processings WHERE processing_id = ?", (pid,)).fetchone()
        if not p:
            witness["processings"][pid] = None
            continue
        ins = sorted(r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_inputs WHERE processing_id = ?", (pid,)))
        outs = sorted(r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_outputs WHERE processing_id = ?", (pid,)))
        witness["processings"][pid] = {
            "equipment_id": p["equipment_id"],
            "start_utc": p["start_utc"], "end_utc": p["end_utc"],
            "inputs": ins, "outputs": outs}
        # 该加工残留窗口内真正参与判定（已验证未撤销）的清洗
        prev = conn.execute(
            "SELECT end_utc FROM processings WHERE equipment_id = ? AND end_utc <= ?"
            " ORDER BY end_utc DESC LIMIT 1",
            (p["equipment_id"], p["start_utc"])).fetchone()
        if prev:
            for c in conn.execute(
                "SELECT id FROM cleanings WHERE equipment_id = ?"
                " AND verified = 1 AND revoked = 0"
                " AND cleaned_at >= ? AND cleaned_at <= ?",
                (p["equipment_id"], prev["end_utc"], p["start_utc"]),
            ):
                cleaning_ids.add(c["id"])

    for cid in sorted(cleaning_ids):
        c = conn.execute(
            "SELECT id, equipment_id, cleaned_at, verified, revoked"
            " FROM cleanings WHERE id = ?", (cid,)).fetchone()
        witness["cleanings"][str(cid)] = (
            None if not c else {
                "equipment_id": c["equipment_id"], "cleaned_at": c["cleaned_at"],
                "verified": int(c["verified"]), "revoked": int(c["revoked"])})

    # 封存传播与过敏原内容无关：仅把“当前封存状态覆盖到本次受影响批次”的生效
    # 调查纳入指纹（根在受影响集合内，或其传播闭包与受影响集合相交）。
    # 无关链路上的调查登记/解除不使凭据失效；传播图本身的变化已由
    # processings 指纹覆盖（新调查若因此波及受影响批次，闭包检查会命中）。
    releases = _all_releases(conn)
    affected_set = set(affected)
    for inv in conn.execute(
            "SELECT investigation_id, batch_id, reason, opened_at, lifted_at,"
            " status FROM investigations WHERE status = 'open'"
            " ORDER BY investigation_id"):
        iid = inv["investigation_id"]
        closure = {bid for bid, rel in releases.items()
                   if any(e["investigation_id"] == iid
                          for e in rel["investigations"])}
        if inv["batch_id"] in affected_set or closure & affected_set:
            witness["investigations"][iid] = {
                "batch_id": inv["batch_id"], "reason": inv["reason"],
                "opened_at": inv["opened_at"], "lifted_at": inv["lifted_at"],
                "status": inv["status"]}
    return witness


def _fingerprint(witness: dict) -> str:
    blob = json.dumps(witness, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------- 预演评估与报告

def _evaluate(conn: sqlite3.Connection, decl_overrides: dict,
              extra_revoked: set, root_batches: set,
              root_equipment: set, forced_cleanings: set) -> dict:
    """在前后两个视图上重算全部批次，构造受影响批次报告与凭据指纹。
    过敏原视图与封存视图分开传播：声明更正不改变封存传播（before/after
    共用同一封存状态）；清洗撤销预演会恢复残留边，封存状态按撤销后重算。"""
    affected: dict[str, dict] = {}

    releases_before = _all_releases(conn)
    releases_after = (
        _all_releases(conn, extra_revoked) if extra_revoked else releases_before)

    for bid in _all_batch_ids(conn):
        kind, _ = batch_kind(conn, bid)
        before = trace_view(conn, bid, releases=releases_before)
        after = trace_view(conn, bid, decl_overrides, extra_revoked,
                           releases=releases_after)
        if _view_key(before) != _view_key(after):
            affected[bid] = {"kind": kind, "before": before, "after": after}

    witness = _collect_witnesses(
        conn, affected, root_batches, root_equipment, forced_cleanings)

    affected_out: list[dict] = []
    for bid in sorted(affected):
        entry = affected[bid]
        before, after = entry["before"], entry["after"]
        label_reports = []
        for lab in conn.execute(
            "SELECT id, issued_at, supersedes_id, snapshot FROM labels"
            " WHERE batch_id = ? ORDER BY id", (bid,),
        ):
            snap = json.loads(lab["snapshot"])
            frozen = snap["conclusion"]
            lb = _diff_label_against(frozen, before)
            la = _diff_label_against(frozen, after)
            if lb is not None or la is not None:
                label_reports.append({
                    "label_id": lab["id"],
                    "issued_at": iso_z(lab["issued_at"]),
                    "supersedes_id": lab["supersedes_id"],
                    "frozen_conclusion": frozen,
                    "diff_vs_before": lb,
                    "diff_vs_after": la,
                })
        affected_out.append({
            "batch_id": bid,
            "kind": entry["kind"],
            "before": before,
            "after": after,
            "diff": _diff_views(before, after),
            "issued_labels": label_reports,
        })

    return {
        "affected_batches": affected_out,
        "witness": witness,
        "fingerprint": _fingerprint(witness),
    }


def _store_preview(record: dict) -> str:
    if len(_PREVIEWS) >= _PREVIEW_LIMIT:  # 简单淘汰最旧凭据
        oldest = next(iter(_PREVIEWS))
        _PREVIEWS.pop(oldest, None)
    token = secrets.token_urlsafe(24)
    record["preview_id"] = token
    record["previewed_at"] = iso_z(to_store(now_utc()))
    _PREVIEWS[token] = record
    return token


def _preview_report(change: dict, evaluation: dict, token: str,
                    previewed_at: str) -> dict:
    return {
        "preview_id": token,
        "previewed_at": previewed_at,
        "dry_run": True,
        "applied": False,
        "change": change,
        "affected_batches": evaluation["affected_batches"],
        "affected_count": len(evaluation["affected_batches"]),
        "commit": {
            "method": "POST",
            "path": f"/previews/{token}/commit",
            "note": "预演不落库；凭据仅在影响本次差异的记录变化时失效。",
        },
    }


# ---------------- 声明更正预演 / 提交

def preview_declaration(conn: sqlite3.Connection, batch_id: str, data) -> dict:
    declared_at = to_store(data.declared_at) if data.declared_at else to_store(now_utc())
    m = _material(conn, batch_id)
    if not m:
        # 无效目标沿用现有语义：加工产出批次号 / 不存在批次均为 404
        raise NotFound(f"未知原料批次: {batch_id}")
    cur = _current_declaration(conn, batch_id)
    new_version = cur["version"] + 1
    override = {
        "allergens": list(data.allergens),
        "pending": bool(data.pending),
        "version": new_version,
        "declared_at": declared_at,
    }
    change = {
        "type": "declaration_correction",
        "batch_id": batch_id,
        "new_version": new_version,
        "allergens": list(data.allergens),
        "pending": bool(data.pending),
        "declared_at": iso_z(declared_at),
    }
    evaluation = _evaluate(
        conn, {batch_id: override}, set(),
        root_batches={batch_id}, root_equipment=set(),
        forced_cleanings=set())
    record = {
        "kind": "declaration",
        "batch_id": batch_id,
        "payload": {"allergens": list(data.allergens),
                    "pending": bool(data.pending),
                    "declared_at": declared_at},
        "fingerprint": evaluation["fingerprint"],
    }
    token = _store_preview(record)
    return _preview_report(change, evaluation, token, record["previewed_at"])


def commit_declaration(conn: sqlite3.Connection, preview_id: str) -> dict:
    record = _PREVIEWS.get(preview_id)
    if not record or record["kind"] != "declaration":
        raise Conflict("预演凭据不存在、已使用或已失效，请重新预演")
    batch_id = record["batch_id"]
    payload = record["payload"]
    with write_tx(conn):
        m = _material(conn, batch_id)
        if not m:
            _PREVIEWS.pop(preview_id, None)
            raise NotFound(f"未知原料批次: {batch_id}")
        cur = _current_declaration(conn, batch_id)
        override = {
            "allergens": payload["allergens"],
            "pending": payload["pending"],
            "version": cur["version"] + 1,
            "declared_at": payload["declared_at"],
        }
        evaluation = _evaluate(
            conn, {batch_id: override}, set(),
            root_batches={batch_id}, root_equipment=set(),
            forced_cleanings=set())
        if evaluation["fingerprint"] != record["fingerprint"]:
            raise Conflict(
                "影响本次差异的记录自预演后已变化，请重新预演后再提交"
                "（无关记录更新不影响凭据）")
        releases = _all_releases(conn)
        before_views = _snapshot_views(conn, releases)
        conn.execute(
            "INSERT INTO material_declarations (batch_id, version, allergens, pending, declared_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (batch_id, cur["version"] + 1, json.dumps(payload["allergens"]),
             int(payload["pending"]), payload["declared_at"]))
        _record_events(conn, "declaration_correction",
                       {"batch_id": batch_id, "new_version": cur["version"] + 1,
                        "via": "preview_commit", "preview_id": preview_id},
                       before_views, before_releases=releases)
        applied_at = iso_z(to_store(now_utc()))
        change = {
            "type": "declaration_correction",
            "batch_id": batch_id,
            "new_version": cur["version"] + 1,
            "allergens": list(payload["allergens"]),
            "pending": bool(payload["pending"]),
            "declared_at": iso_z(payload["declared_at"]),
        }
        report = _preview_report(change, evaluation, preview_id,
                                 record["previewed_at"])
        _PREVIEWS.pop(preview_id, None)  # 一次性凭据
    report["dry_run"] = False
    report["applied"] = True
    report["applied_at"] = applied_at
    return report


# ---------------- 清洗撤销预演 / 提交

def preview_revoke_cleaning(conn: sqlite3.Connection, cleaning_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM cleanings WHERE id = ?", (cleaning_id,)).fetchone()
    if not row:
        raise NotFound(f"未知清洗记录: {cleaning_id}")
    if row["revoked"]:
        raise Conflict(f"清洗记录 {cleaning_id} 已撤销")
    change = {
        "type": "cleaning_revocation",
        "cleaning_id": cleaning_id,
        "equipment_id": row["equipment_id"],
        "cleaned_at": iso_z(row["cleaned_at"]),
    }
    evaluation = _evaluate(
        conn, {}, {cleaning_id},
        root_batches=set(), root_equipment={row["equipment_id"]},
        forced_cleanings={cleaning_id})
    record = {
        "kind": "revoke_cleaning",
        "cleaning_id": cleaning_id,
        "fingerprint": evaluation["fingerprint"],
    }
    token = _store_preview(record)
    return _preview_report(change, evaluation, token, record["previewed_at"])


def commit_revoke_cleaning(conn: sqlite3.Connection, preview_id: str) -> dict:
    record = _PREVIEWS.get(preview_id)
    if not record or record["kind"] != "revoke_cleaning":
        raise Conflict("预演凭据不存在、已使用或已失效，请重新预演")
    cleaning_id = record["cleaning_id"]
    with write_tx(conn):
        row = conn.execute(
            "SELECT * FROM cleanings WHERE id = ?", (cleaning_id,)).fetchone()
        if not row:
            _PREVIEWS.pop(preview_id, None)
            raise NotFound(f"未知清洗记录: {cleaning_id}")
        if row["revoked"]:
            _PREVIEWS.pop(preview_id, None)
            raise Conflict(f"清洗记录 {cleaning_id} 已撤销")
        evaluation = _evaluate(
            conn, {}, {cleaning_id},
            root_batches=set(), root_equipment={row["equipment_id"]},
            forced_cleanings={cleaning_id})
        if evaluation["fingerprint"] != record["fingerprint"]:
            raise Conflict(
                "影响本次差异的记录自预演后已变化，请重新预演后再提交"
                "（无关记录更新不影响凭据）")
        releases_before = _all_releases(conn)
        before_views = _snapshot_views(conn, releases_before)
        releases_after = _all_releases(conn, {cleaning_id})
        conn.execute("UPDATE cleanings SET revoked = 1 WHERE id = ?", (cleaning_id,))
        _record_events(conn, "cleaning_revocation",
                       {"cleaning_id": cleaning_id, "via": "preview_commit",
                        "preview_id": preview_id}, before_views,
                       before_releases=releases_before,
                       after_releases=releases_after)
        applied_at = iso_z(to_store(now_utc()))
        change = {
            "type": "cleaning_revocation",
            "cleaning_id": cleaning_id,
            "equipment_id": row["equipment_id"],
            "cleaned_at": iso_z(row["cleaned_at"]),
        }
        report = _preview_report(change, evaluation, preview_id,
                                 record["previewed_at"])
        _PREVIEWS.pop(preview_id, None)
    report["dry_run"] = False
    report["applied"] = True
    report["applied_at"] = applied_at
    return report


def commit_preview(conn: sqlite3.Connection, preview_id: str) -> dict:
    """预演凭据统一提交入口：按预演类型分派，原子应用并返回实际影响。"""
    record = _PREVIEWS.get(preview_id)
    if not record:
        raise Conflict("预演凭据不存在、已使用或已失效，请重新预演")
    if record["kind"] == "declaration":
        return commit_declaration(conn, preview_id)
    if record["kind"] == "revoke_cleaning":
        return commit_revoke_cleaning(conn, preview_id)
    _PREVIEWS.pop(preview_id, None)
    raise Conflict("未知的预演类型，请重新预演")


# ---------------------------------------------------------------- 调查封存

def _investigation_row(conn: sqlite3.Connection, investigation_id: str):
    return conn.execute(
        "SELECT * FROM investigations WHERE investigation_id = ?",
        (investigation_id,)).fetchone()


def open_investigation(conn: sqlite3.Connection, data) -> dict:
    """登记疑似交叉接触调查封存：唯一调查号 + 疑似批次 + 原因。

    受影响批次沿加工投入链与同设备相邻加工残留链实时计算（有效清洗截断
    残留传播）；封存与过敏原声明是否为空无关。封存/解除与受影响状态变更在
    同一事务原子提交，并沿用游标事件流发布实际变化（按批次号顺序）。
    """
    with write_tx(conn):
        if _investigation_row(conn, data.investigation_id):
            raise Conflict(
                f"调查号 {data.investigation_id} 已存在，不能重复登记")
        kind, _ = batch_kind(conn, data.batch_id)
        if not kind:
            raise BadRequest(f"未知批次: {data.batch_id}")
        releases = _all_releases(conn)
        before_views = _snapshot_views(conn, releases)
        opened_at = to_store(now_utc())
        conn.execute(
            "INSERT INTO investigations"
            " (investigation_id, batch_id, reason, opened_at, lifted_at, status)"
            " VALUES (?, ?, ?, ?, NULL, 'open')",
            (data.investigation_id, data.batch_id, data.reason, opened_at))
        _record_events(conn, "investigation_open",
                       {"investigation_id": data.investigation_id},
                       before_views, before_releases=releases)
    return get_investigation(conn, data.investigation_id)


def lift_investigation(conn: sqlite3.Connection, investigation_id: str) -> dict:
    """解除调查封存：只移除该调查自身的影响；其他调查仍封存的批次保持封存。
    重复解除（不存在/已解除）明确报错；封存解除与受影响状态变更原子提交。"""
    with write_tx(conn):
        row = _investigation_row(conn, investigation_id)
        if not row:
            raise NotFound(f"未知调查号: {investigation_id}")
        if row["status"] != "open":
            raise Conflict(f"调查 {investigation_id} 已解除，不能重复解除")
        releases = _all_releases(conn)
        before_views = _snapshot_views(conn, releases)
        lifted_at = to_store(now_utc())
        conn.execute(
            "UPDATE investigations SET status = 'lifted', lifted_at = ?"
            " WHERE investigation_id = ?",
            (lifted_at, investigation_id))
        _record_events(conn, "investigation_lift",
                       {"investigation_id": investigation_id},
                       before_views, before_releases=releases)
    return get_investigation(conn, investigation_id)


def _investigation_view(row, releases: dict) -> dict:
    rel = releases.get(row["batch_id"], _empty_release())
    entry = next((e for e in rel["investigations"]
                  if e["investigation_id"] == row["investigation_id"]), None)
    if entry:
        affected = _affected_by_investigation(
            releases, row["investigation_id"])
        affected_batches = [
            {"batch_id": b, "paths": affected[b]}
            for b in sorted(affected, key=lambda b: (b != row["batch_id"], b))]
    else:
        affected_batches = []  # 已解除：该调查不再封存任何批次
    return {
        "investigation_id": row["investigation_id"],
        "batch_id": row["batch_id"],
        "reason": row["reason"],
        "opened_at": iso_z(row["opened_at"]),
        "status": row["status"],
        "lifted_at": iso_z(row["lifted_at"]) if row["lifted_at"] else None,
        "affected_batches": affected_batches,
    }


def _affected_by_investigation(releases: dict, investigation_id: str) -> dict:
    """{batch_id: paths}：当前仍被该调查封存的批次及各自传播路径。"""
    out: dict[str, list] = {}
    for bid, rel in releases.items():
        entry = next((e for e in rel["investigations"]
                      if e["investigation_id"] == investigation_id), None)
        if entry:
            out[bid] = entry["paths"]
    return out


def get_investigation(conn: sqlite3.Connection, investigation_id: str) -> dict:
    row = _investigation_row(conn, investigation_id)
    if not row:
        raise NotFound(f"未知调查号: {investigation_id}")
    releases = _all_releases(conn)
    return _investigation_view(row, releases)


def list_investigations(conn: sqlite3.Connection,
                        status: str | None = None) -> list[dict]:
    if status not in (None, "open", "lifted"):
        raise BadRequest("status 仅支持 open / lifted")
    sql = "SELECT * FROM investigations"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY opened_at, investigation_id"
    rows = conn.execute(sql, params).fetchall()
    releases = _all_releases(conn)
    return [_investigation_view(r, releases) for r in rows]


def batch_release(conn: sqlite3.Connection, batch_id: str) -> dict:
    """按批次查询放行状态：是否封存、仍生效的调查号及各自传播路径。"""
    kind, _ = batch_kind(conn, batch_id)
    if not kind:
        raise NotFound(f"未知批次: {batch_id}")
    releases = _all_releases(conn)
    rel = releases.get(batch_id, _empty_release())
    return {
        "batch_id": batch_id,
        "kind": kind,
        "release_status": "held" if rel["held"] else "releasable",
        "held": rel["held"],
        "active_investigations": [
            {"investigation_id": e["investigation_id"],
             "root_batch_id": e["root_batch_id"],
             "reason": e["reason"],
             "opened_at": e["opened_at"],
             "paths": e["paths"]}
            for e in rel["investigations"]],
    }
