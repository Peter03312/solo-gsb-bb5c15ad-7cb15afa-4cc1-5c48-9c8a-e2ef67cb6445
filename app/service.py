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


# ---------------------------------------------------------------- 调查封存传播
#
# 封存沿两类边从调查起点批次向**下游**传播：
# - input：某加工的产出被后续加工作为投入（加工投入链）；
# - equipment_residue：同设备“相邻”加工间的残留（上一次加工 end <= 本次 start），
#   且两次加工之间不存在“已验证且未撤销”的清洗（有效清洗截断残留传播，
#   不截断投入链传播）。
# 残留边一旦从某次加工发出（其任一产出在封存闭包内），该加工的全部产出都作为
# 残留污染带入相邻的下一次加工，再由该加工的全部产出继续沿两类边传播。

def _consuming_processings(conn, batch_id: str):
    """以该批次为投入的加工。"""
    return conn.execute(
        "SELECT processing_id FROM processing_inputs WHERE batch_id = ?",
        (batch_id,)).fetchall()


def _processing_outputs(conn, processing_id: str) -> list[str]:
    return [r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM processing_outputs WHERE processing_id = ?"
        " ORDER BY batch_id", (processing_id,))]


def _adjacent_next_processing(conn, proc_row) -> object | None:
    """同设备上的相邻下一次加工：start_utc >= 本次 end_utc 中 start 最早者
    （前序结束即开始视为相邻，与既有“相邻不重叠”口径一致）。"""
    return conn.execute(
        "SELECT * FROM processings WHERE equipment_id = ? AND start_utc >= ?"
        " ORDER BY start_utc ASC, processing_id ASC LIMIT 1",
        (proc_row["equipment_id"], proc_row["end_utc"]),
    ).fetchone()


def _effective_cleaning_between(conn, equipment_id: str,
                                prev_end: str, next_start: str,
                                extra_revoked: set | None = None):
    """窗口 (prev_end, next_start] 内是否存在“已验证且未撤销”的清洗。
    与溯源残留判定同一口径；extra_revoked 供预演叠加（视为已撤销）。"""
    extra_revoked = extra_revoked or set()
    c = conn.execute(
        "SELECT * FROM cleanings WHERE equipment_id = ?"
        " AND verified = 1 AND revoked = 0"
        " AND cleaned_at >= ? AND cleaned_at <= ?"
        " ORDER BY cleaned_at LIMIT 1",
        (equipment_id, prev_end, next_start),
    ).fetchone()
    if c and c["id"] not in extra_revoked:
        return c
    return None


def _open_investigations(conn) -> list:
    return conn.execute(
        "SELECT * FROM investigations WHERE status = 'open' ORDER BY investigation_id"
    ).fetchall()


def investigation_closure(conn, root_batch_id: str,
                          extra_revoked_cleanings: set | None = None) -> dict:
    """单个调查起点的封存闭包：{batch_id: [传播路径...]}（含起点本身）。

    路径为步骤列表，首步标记调查起点，后续每步标注经由加工及边类型：
      {"batch_id": "OUT-A", "via": "investigation_root", "investigation_id": ...（调用方补）}
      {"batch_id": "OUT-B", "processing_id": "P2", "via": "input"}
      {"batch_id": "OUT-C", "processing_id": "P3", "via": "equipment_residue",
       "equipment_id": "EQ-1"}
    边按时间向前传播，图无环；以“批次+完整路径”去重保留所有实际传播路径。
    残留边从受封存批次的“源加工”发出：该加工即相邻下一次加工的上次加工，
    等价于“源加工有产出受封存则其残留污染下一加工”；未受封存的兄弟产出
    （多产出加工）不参与该残留路径。
    """
    extra_revoked = extra_revoked_cleanings or set()
    root_step = {"batch_id": root_batch_id, "via": "investigation_root"}
    affected: dict[str, list[list]] = {root_batch_id: [[root_step]]}
    seen_paths: set[str] = {json.dumps([root_step], sort_keys=True, ensure_ascii=False)}

    # 以“到达某批次的具体路径”为工作项：同批次经新路径到达时，仍需沿该新路径
    # 继续传播（包括重发其源加工的残留边）。seen_paths 保证终止（路径数量有穷）。
    work: list[tuple[str, list[dict]]] = [(root_batch_id, [root_step])]

    def extend(tgt: str, path: list[dict]) -> bool:
        key = json.dumps(path, sort_keys=True, ensure_ascii=False)
        if key in seen_paths:
            return False
        seen_paths.add(key)
        affected.setdefault(tgt, []).append(path)
        work.append((tgt, path))
        return True

    while work:
        bid, base = work.pop(0)
        # 投入边：该批次 -> 以它为投入的加工 -> 该加工全部产出
        for r in _consuming_processings(conn, bid):
            pid = r["processing_id"]
            for tgt in _processing_outputs(conn, pid):
                extend(tgt, base + [{
                    "batch_id": tgt, "processing_id": pid, "via": "input"}])
        # 残留边：若该批次为加工产出，其源加工的任一产出已在闭包内，
        # 则同设备相邻下一次加工经残留被封存（有效清洗截断）。
        out = _output_of(conn, bid)
        if not out:
            continue
        pid = out["processing_id"]
        proc = conn.execute(
            "SELECT * FROM processings WHERE processing_id = ?", (pid,)).fetchone()
        nxt = _adjacent_next_processing(conn, proc)
        if not nxt:
            continue
        cleaning = _effective_cleaning_between(
            conn, proc["equipment_id"], proc["end_utc"], nxt["start_utc"],
            extra_revoked)
        if cleaning:
            continue  # 有效清洗截断残留传播
        for tgt in _processing_outputs(conn, nxt["processing_id"]):
            extend(tgt, base + [{
                "batch_id": tgt,
                "processing_id": nxt["processing_id"],
                "via": "equipment_residue",
                "equipment_id": proc["equipment_id"],
            }])

    for paths in affected.values():
        paths.sort(key=lambda p: json.dumps(p, sort_keys=True, ensure_ascii=False))
    return affected


def quarantine_views(conn, extra_revoked_cleanings: set | None = None) -> dict:
    """按当前仍生效（open）调查计算全部批次封存视图：{batch_id: quarantine_view}。

    多个调查叠加：同批次可被多个调查封存，各自保留独立传播路径；
    解除其中一个只移除该调查的条目，其余不受影响。封存与过敏原声明是否为空无关。
    """
    states: dict[str, dict[str, dict]] = {}
    for inv in _open_investigations(conn):
        closure = investigation_closure(
            conn, inv["root_batch_id"], extra_revoked_cleanings)
        for bid, paths in closure.items():
            states.setdefault(bid, {})[inv["investigation_id"]] = {
                "investigation_id": inv["investigation_id"],
                "root_batch_id": inv["root_batch_id"],
                "reason": inv["reason"],
                "registered_at": iso_z(inv["registered_at"]),
                "paths": paths,
            }
    views: dict[str, dict] = {}
    for bid in _all_batch_ids(conn):
        entries = states.get(bid, {})
        views[bid] = {
            "status": "held" if entries else "released",
            "investigations": [entries[k] for k in sorted(entries)],
        }
    return views


def batch_quarantine_view(conn, batch_id: str,
                          extra_revoked_cleanings: set | None = None) -> dict:
    return quarantine_views(conn, extra_revoked_cleanings).get(
        batch_id, {"status": "released", "investigations": []})


def _quarantine_key(view: dict) -> str:
    """封存视图规范化键：状态 + 各调查（id/原因/登记时间/路径多重集合）。"""
    payload = {
        "status": view["status"],
        "investigations": sorted(
            ({
                "investigation_id": i["investigation_id"],
                "root_batch_id": i["root_batch_id"],
                "reason": i["reason"],
                "registered_at": i["registered_at"],
                "paths": _canon_paths(i["paths"]),
            } for i in view["investigations"]),
            key=lambda d: d["investigation_id"]),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------- 原料登记

def create_material(conn: sqlite3.Connection, data) -> dict:
    declared_at = to_store(data.declared_at) if data.declared_at else to_store(now_utc())
    with write_tx(conn):
        kind, _ = batch_kind(conn, data.batch_id)
        if kind:
            raise Conflict(f"批次 {data.batch_id} 已存在（{kind}），不能重复登记")
        before_views = _snapshot_views(conn)
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
                       {"batch_id": data.batch_id, "version": 1}, before_views)
    return get_material(conn, data.batch_id)


def add_declaration(conn: sqlite3.Connection, batch_id: str, data) -> dict:
    declared_at = to_store(data.declared_at) if data.declared_at else to_store(now_utc())
    with write_tx(conn):
        if not _material(conn, batch_id):
            raise NotFound(f"未知原料批次: {batch_id}")
        cur = _current_declaration(conn, batch_id)
        before_views = _snapshot_views(conn)
        conn.execute(
            "INSERT INTO material_declarations (batch_id, version, allergens, pending, declared_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (batch_id, cur["version"] + 1, json.dumps(data.allergens),
             int(data.pending), declared_at),
        )
        _record_events(conn, "declaration_correction",
                       {"batch_id": batch_id, "new_version": cur["version"] + 1,
                        "via": "direct"}, before_views)
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

        before_views = _snapshot_views(conn)
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

        # 6) 封存拦截：封存批次不得作为新加工投入；设备残留会使新产出受封存也拒绝。
        #    此时尚未落事件，事务回滚后不留下加工记录。
        held_inputs = [
            in_id for in_id in data.inputs
            if before_views[in_id]["quarantine"]["status"] == "held"]
        if held_inputs:
            raise Conflict(
                f"加工 {data.processing_id} 登记被拒绝：投入批次已封存 "
                f"{sorted(held_inputs)}，封存批次不得作为新加工投入")
        q_after = quarantine_views(conn)
        for out_id in data.outputs:
            if q_after[out_id]["status"] == "held":
                inv_ids = sorted(
                    i["investigation_id"] for i in q_after[out_id]["investigations"])
                raise Conflict(
                    f"加工 {data.processing_id} 登记被拒绝：产出批次 {out_id} 会因设备"
                    f"残留受到封存（生效调查 {inv_ids}）")

        _record_events(conn, "processing_register",
                       {"processing_id": data.processing_id}, before_views)
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

        # 校验通过：替换投入（原加工号/设备/时段/产出保持原值）
        before_views = _snapshot_views(conn)
        conn.execute(
            "DELETE FROM processing_inputs WHERE processing_id = ?", (processing_id,))
        for in_id in new_inputs:
            conn.execute(
                "INSERT INTO processing_inputs (processing_id, batch_id) VALUES (?, ?)",
                (processing_id, in_id),
            )
        # 封存拦截（与加工登记同口径）：封存批次不得经纠正成为投入；
        # 纠正后若产出（含设备残留路径）会受封存也拒绝，回滚保持原状。
        held_inputs = [
            in_id for in_id in new_inputs
            if before_views[in_id]["quarantine"]["status"] == "held"]
        if held_inputs:
            raise Conflict(
                f"加工 {processing_id} 投入纠正被拒绝：投入批次已封存 "
                f"{sorted(held_inputs)}，封存批次不得作为加工投入")
        q_after = quarantine_views(conn)
        for out_id in outputs:
            if q_after[out_id]["status"] == "held":
                inv_ids = sorted(
                    i["investigation_id"] for i in q_after[out_id]["investigations"])
                raise Conflict(
                    f"加工 {processing_id} 投入纠正被拒绝：产出批次 {out_id} "
                    f"纠正后会受到封存（生效调查 {inv_ids}）")
        # 全量重算对比：仅为结论/来源路径/依据/封存状态实际变化的批次按批次号
        # 顺序落事件，设备残留沿后续加工继续传播的批次同样自动计入。
        _record_events(conn, "processing_input_correction",
                       {"processing_id": processing_id}, before_views)
    return get_processing(conn, processing_id)


# ---------------------------------------------------------------- 清洗

def create_cleaning(conn: sqlite3.Connection, data) -> dict:
    with write_tx(conn):
        before_views = _snapshot_views(conn)
        cur = conn.execute(
            "INSERT INTO cleanings (equipment_id, cleaned_at, verified, revoked)"
            " VALUES (?, ?, ?, 0)",
            (data.equipment_id, to_store(data.cleaned_at), int(data.verified)),
        )
        cid = cur.lastrowid
        # 未验证清洗、不在任何残留窗口内的清洗不改变实时溯源 -> 对比后无事件
        _record_events(conn, "cleaning_register", {"cleaning_id": cid}, before_views)
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
        before_views = _snapshot_views(conn)
        conn.execute("UPDATE cleanings SET revoked = 1 WHERE id = ?", (cleaning_id,))
        _record_events(conn, "cleaning_revocation",
                       {"cleaning_id": cleaning_id, "via": "direct"}, before_views)
    return get_cleaning(conn, cleaning_id)


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
               extra_revoked_cleanings: set | None = None) -> dict:
    """完整实时视图：结论（含来源路径）+ 依据。可叠加预演覆盖。"""
    result, evidence = compute_trace(
        conn, batch_id, decl_overrides, extra_revoked_cleanings)
    return {
        "conclusion": conclusion_view(result),
        "basis": basis_view(evidence),
    }


def trace_response(conn: sqlite3.Connection, batch_id: str) -> dict:
    kind, _ = batch_kind(conn, batch_id)
    if not kind:
        raise NotFound(f"未知批次: {batch_id}")
    view = trace_view(conn, batch_id)
    return {
        "batch_id": batch_id,
        "kind": kind,
        **view["conclusion"],
        "basis": view["basis"],
    }


# ---------------------------------------------------------------- 变更事件流
#
# 下游系统按游标增量消费溯源变化：每次业务写入在**同一事务**内先取全部批次
# 实时视图快照，写入后重算并对比，仅为“新批次（前态为空）”与“结论/来源路径/
# 依据确有变化”的批次落事件；同次操作的事件按批次号排序插入（seq 即有序）。
# 校验失败回滚、预演（dry-run）不落任何事件；未验证清洗、标签签发等不改变
# 实时溯源的操作经对比自然不产生事件。

def _snapshot_views(conn: sqlite3.Connection) -> dict:
    """当前全部批次的实时视图（结论 + 依据 + 封存状态），{batch_id: view}。"""
    qviews = quarantine_views(conn)
    return {bid: {**trace_view(conn, bid), "quarantine": qviews[bid]}
            for bid in _all_batch_ids(conn)}


def _record_events(conn: sqlite3.Connection, op: str, op_ref: dict,
                   before_views: dict) -> list[str]:
    """写事务内调用：重算全部批次视图并与 before_views 对比，为新批次与
    确有变化（溯源结论/路径/依据 **或** 封存状态）的批次按批次号顺序落事件。
    返回产生事件的批次号列表。事件同时携带溯源视图与封存视图（任一有差异）。"""
    after_views = _snapshot_views(conn)
    changed = []
    for bid, after in after_views.items():
        before = before_views.get(bid)
        diff_trace = before is None or _view_key({
            "conclusion": before["conclusion"], "basis": before["basis"]}
        ) != _view_key({"conclusion": after["conclusion"], "basis": after["basis"]})
        diff_q = before is None or _quarantine_key(
            before.get("quarantine") or {"status": "released", "investigations": []}
        ) != _quarantine_key(after["quarantine"])
        if diff_trace or diff_q:
            changed.append((bid, before, after))
    changed.sort(key=lambda item: item[0])  # 同次操作按批次号排序
    now = to_store(now_utc())
    ref = json.dumps(op_ref, ensure_ascii=False, sort_keys=True)
    for bid, before, after in changed:
        kind, _ = batch_kind(conn, bid)
        conn.execute(
            "INSERT INTO trace_events"
            " (op, op_ref, batch_id, kind, before_view, after_view, created_at,"
            "  before_quarantine_view, quarantine_view)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (op, ref, bid, kind,
             json.dumps({k: before[k] for k in ("conclusion", "basis")},
                        ensure_ascii=False, sort_keys=True)
             if before is not None else None,
             json.dumps({k: after[k] for k in ("conclusion", "basis")},
                        ensure_ascii=False, sort_keys=True),
             now,
             json.dumps(
                 before["quarantine"] if before is not None else None,
                 ensure_ascii=False, sort_keys=True),
             json.dumps(after["quarantine"], ensure_ascii=False, sort_keys=True)),
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
        # 封存前后态（历史事件/纯溯源事件为 null）
        "quarantine_before": (json.loads(row["before_quarantine_view"])
                              if row["before_quarantine_view"] is not None else None),
        "quarantine_after": (json.loads(row["quarantine_view"])
                             if row["quarantine_view"] is not None else None),
        "recorded_at": iso_z(row["created_at"]),
    }


def changes_snapshot(conn: sqlite3.Connection) -> dict:
    """初始化读取：同一状态（读事务一致快照）下的现存批次实时结论、封存状态与游标。
    之后以该游标调用事件接口即可不重不漏地接续增量变化。"""
    with read_tx(conn):
        cursor = _max_event_seq(conn)
        qviews = quarantine_views(conn)
        batches = []
        for bid in _all_batch_ids(conn):
            kind, _ = batch_kind(conn, bid)
            view = trace_view(conn, bid)
            batches.append({
                "batch_id": bid,
                "kind": kind,
                "conclusion": view["conclusion"],
                "basis": view["basis"],
                "quarantine": qviews[bid],
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
        if supersedes_id is not None:
            old = conn.execute(
                "SELECT * FROM labels WHERE id = ?", (supersedes_id,)).fetchone()
            if not old:
                raise NotFound(f"被关联的旧标签不存在: {supersedes_id}")
            if old["batch_id"] != batch_id:
                raise BadRequest(
                    f"旧标签 {supersedes_id} 属于批次 {old['batch_id']}，"
                    f"不能关联到批次 {batch_id} 的新标签")
        # 封存批次不得签发新标签（旧标签快照永不改写，不影响既有标签）
        qview = batch_quarantine_view(conn, batch_id)
        if qview["status"] == "held":
            inv_ids = sorted(i["investigation_id"] for i in qview["investigations"])
            raise Conflict(
                f"批次 {batch_id} 已封存（生效调查 {inv_ids}），不得签发新标签")
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


def _view_key(view: dict) -> str:
    payload = {
        "conclusion": _canon_conclusion(view["conclusion"]),
        "basis": _canon_basis(view["basis"]),
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
    return witness


def _fingerprint(witness: dict) -> str:
    blob = json.dumps(witness, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------- 预演评估与报告

def _evaluate(conn: sqlite3.Connection, decl_overrides: dict,
              extra_revoked: set, root_batches: set,
              root_equipment: set, forced_cleanings: set) -> dict:
    """在前后两个视图上重算全部批次，构造受影响批次报告与凭据指纹。"""
    affected: dict[str, dict] = {}

    for bid in _all_batch_ids(conn):
        kind, _ = batch_kind(conn, bid)
        before = trace_view(conn, bid)
        after = trace_view(conn, bid, decl_overrides, extra_revoked)
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
        before_views = _snapshot_views(conn)
        conn.execute(
            "INSERT INTO material_declarations (batch_id, version, allergens, pending, declared_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (batch_id, cur["version"] + 1, json.dumps(payload["allergens"]),
             int(payload["pending"]), payload["declared_at"]))
        _record_events(conn, "declaration_correction",
                       {"batch_id": batch_id, "new_version": cur["version"] + 1,
                        "via": "preview_commit", "preview_id": preview_id},
                       before_views)
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
        before_views = _snapshot_views(conn)
        conn.execute("UPDATE cleanings SET revoked = 1 WHERE id = ?", (cleaning_id,))
        _record_events(conn, "cleaning_revocation",
                       {"cleaning_id": cleaning_id, "via": "preview_commit",
                        "preview_id": preview_id}, before_views)
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
#
# 质控人员以“批次号 + 唯一调查号 + 原因”登记疑似交叉接触调查：登记后系统沿
# 加工投入链与同设备相邻加工的残留链（有效清洗截断）计算受影响批次并封存。
# 封存与过敏原声明是否为空无关；封存状态为全部生效（open）调查闭包的叠加，
# 解除其中一个只移除该调查的影响。调查登记/解除与受影响状态变更同事务原子
# 提交，并沿用既有游标事件流（仅为封存状态实际变化的批次按批次号序发事件）。

def _investigation(conn, investigation_id: str):
    return conn.execute(
        "SELECT * FROM investigations WHERE investigation_id = ?",
        (investigation_id,)).fetchone()


def register_investigation(conn, data) -> dict:
    """登记调查封存。未知批次 404，重复调查号 409；与封存状态变更原子提交。"""
    with write_tx(conn):
        kind, _ = batch_kind(conn, data.batch_id)
        if not kind:
            raise NotFound(f"未知批次: {data.batch_id}")
        if _investigation(conn, data.investigation_id):
            raise Conflict(f"调查号 {data.investigation_id} 已存在，不能重复登记")
        registered_at = to_store(now_utc())
        before_views = _snapshot_views(conn)
        conn.execute(
            "INSERT INTO investigations"
            " (investigation_id, root_batch_id, reason, status,"
            "  registered_at, released_at)"
            " VALUES (?, ?, ?, 'open', ?, NULL)",
            (data.investigation_id, data.batch_id, data.reason, registered_at))
        _record_events(conn, "investigation_register",
                       {"investigation_id": data.investigation_id,
                        "root_batch_id": data.batch_id},
                       before_views)
        row = _investigation(conn, data.investigation_id)
    return _investigation_response(conn, row)


def release_investigation(conn, investigation_id: str) -> dict:
    """解除调查封存：只移除该调查的影响，其余调查叠加不变。
    未知调查号 404，重复解除（非 open）409；与状态变更原子提交。"""
    with write_tx(conn):
        row = _investigation(conn, investigation_id)
        if not row:
            raise NotFound(f"未知调查号: {investigation_id}")
        if row["status"] != "open":
            raise Conflict(f"调查 {investigation_id} 已解除，不能重复解除")
        before_views = _snapshot_views(conn)
        released_at = to_store(now_utc())
        conn.execute(
            "UPDATE investigations SET status = 'released', released_at = ?"
            " WHERE investigation_id = ?",
            (released_at, investigation_id))
        _record_events(conn, "investigation_release",
                       {"investigation_id": investigation_id},
                       before_views)
        row = _investigation(conn, investigation_id)
    return _investigation_response(conn, row)


def list_investigations(conn, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM investigations WHERE status = ? ORDER BY investigation_id",
            (status,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM investigations ORDER BY investigation_id").fetchall()
    return [_investigation_response(conn, r) for r in rows]


def get_investigation(conn, investigation_id: str) -> dict:
    row = _investigation(conn, investigation_id)
    if not row:
        raise NotFound(f"未知调查号: {investigation_id}")
    return _investigation_response(conn, row)


def _investigation_response(conn, row) -> dict:
    """对外响应：解除后仍返回登记时的传播闭包（该调查自身的路径，不随当前生效集变化），
    但以 status 标明已解除。"""
    closure = investigation_closure(conn, row["root_batch_id"])
    return {
        "investigation_id": row["investigation_id"],
        "root_batch_id": row["root_batch_id"],
        "reason": row["reason"],
        "status": row["status"],
        "registered_at": iso_z(row["registered_at"]),
        "released_at": iso_z(row["released_at"]) if row["released_at"] else None,
        "affected_batches": sorted(closure),
        "propagation": {
            bid: [[{**step} for step in path] for path in paths]
            for bid, paths in sorted(closure.items())
        },
    }


def batch_quarantine_response(conn, batch_id: str) -> dict:
    """按批次查询放行状态、仍生效的调查号及各自传播路径。"""
    kind, _ = batch_kind(conn, batch_id)
    if not kind:
        raise NotFound(f"未知批次: {batch_id}")
    qview = batch_quarantine_view(conn, batch_id)
    investigations = []
    for item in qview["investigations"]:
        investigations.append({
            "investigation_id": item["investigation_id"],
            "root_batch_id": item["root_batch_id"],
            "reason": item["reason"],
            "registered_at": item["registered_at"],
            "propagation_paths": item["paths"],
        })
    return {
        "batch_id": batch_id,
        "kind": kind,
        "status": qview["status"],
        "held": qview["status"] == "held",
        "active_investigation_ids": [i["investigation_id"] for i in investigations],
        "active_investigations": investigations,
    }
