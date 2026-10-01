"""HTTP API 层：路由、依赖注入、错误映射。

错误约定：400 无效时序/参数（含无效游标），404 资源不存在，409 冲突（重叠/来源环/重复批次/重复撤销）。
所有写操作在单事务内完成，拒绝时回滚、保持原状态。
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import db, service
from .schemas import (
    CleaningCreate,
    DeclarationCreate,
    DeclarationPreviewCreate,
    InvestigationCreate,
    LabelCreate,
    MaterialCreate,
    ProcessingCreate,
    ProcessingInputCorrectionCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="过敏原溯源服务",
    version="1.4.0",
    description="共线生产食品批次的过敏原与待确认状态溯源：原料声明、加工登记、"
                "加工投入纠正、设备残留与清洗、来源路径查询、标签签发，"
                "声明更正/清洗撤销的预演与凭据提交、疑似交叉接触的调查封存，"
                "以及面向下游的游标增量变更事件订阅。",
    lifespan=lifespan,
)


@app.exception_handler(service.NotFound)
async def not_found_handler(request: Request, exc: service.NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(service.Conflict)
async def conflict_handler(request: Request, exc: service.Conflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(service.BadRequest)
async def bad_request_handler(request: Request, exc: service.BadRequest):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- 基础

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 原料批次

@app.post("/materials", status_code=201, tags=["原料"])
def create_material(data: MaterialCreate, conn=Depends(get_conn)):
    """登记原料批次及其过敏原声明（或待确认状态）。"""
    return service.create_material(conn, data)


@app.get("/materials", tags=["原料"])
def list_materials(conn=Depends(get_conn)):
    return service.list_materials(conn)


@app.get("/materials/{batch_id}", tags=["原料"])
def get_material(batch_id: str, conn=Depends(get_conn)):
    return service.get_material(conn, batch_id)


@app.post("/materials/{batch_id}/declarations", status_code=201, tags=["原料"])
def add_declaration(batch_id: str, data: DeclarationCreate, conn=Depends(get_conn)):
    """更正原料声明：追加新版本。未签发结论按最新版本实时重算；已签发标签不变。"""
    return service.add_declaration(conn, batch_id, data)


@app.post("/materials/{batch_id}/declarations/preview", status_code=200, tags=["预演"])
def preview_declaration(batch_id: str, data: DeclarationPreviewCreate,
                        conn=Depends(get_conn)):
    """声明更正预演（不落库）：返回确有差异的下游批次（过敏原/待确认/来源路径/依据）
    及其已签发标签相对变更前后实时结论的差异，并给出提交凭据。"""
    return service.preview_declaration(conn, batch_id, data)


# ---------------------------------------------------------------- 加工批次

@app.post("/processings", status_code=201, tags=["加工"])
def create_processing(data: ProcessingCreate, conn=Depends(get_conn)):
    """登记加工批次：设备、UTC 起止、投入/产出批次。

    校验：时序有效、投入已存在且加工开始前可用、来源不成环、同设备不重叠、
    产出批次号未被占用。任一不满足即拒绝并保持原状态。
    """
    return service.create_processing(conn, data)


@app.get("/processings", tags=["加工"])
def list_processings(conn=Depends(get_conn)):
    return service.list_processings(conn)


@app.get("/processings/{processing_id}", tags=["加工"])
def get_processing(processing_id: str, conn=Depends(get_conn)):
    return service.get_processing(conn, processing_id)


@app.post("/processings/{processing_id}/inputs/correction", tags=["加工"])
def correct_processing_inputs(processing_id: str,
                              data: ProcessingInputCorrectionCreate,
                              conn=Depends(get_conn)):
    """纠正加工的投入批次登记（整表替换为完整正确投入列表）。

    原加工号、设备、起止时段、产出批次保持原值；投入须非空、无重复、已登记且
    在开工前可用，纠正后的来源关系不得成环。校验与替换在同一事务内完成，
    随后按现有规则重算相关批次实时结论（含后续设备残留传播）；投入集合未变
    不产事件，已签发标签保留原快照。加工不存在返回 404，校验失败返回
    400/409，记录与事件均保持原状。
    """
    return service.correct_processing_inputs(conn, processing_id, data)


# ---------------------------------------------------------------- 清洗

@app.post("/cleanings", status_code=201, tags=["清洗"])
def create_cleaning(data: CleaningCreate, conn=Depends(get_conn)):
    """登记设备清洗。只有 verified=true 且未被撤销、且发生在两次加工之间的清洗才清除残留。"""
    return service.create_cleaning(conn, data)


@app.get("/cleanings", tags=["清洗"])
def list_cleanings(equipment_id: str | None = None, conn=Depends(get_conn)):
    return service.list_cleanings(conn, equipment_id)


@app.get("/cleanings/{cleaning_id}", tags=["清洗"])
def get_cleaning(cleaning_id: int, conn=Depends(get_conn)):
    return service.get_cleaning(conn, cleaning_id)


@app.post("/cleanings/{cleaning_id}/revoke", tags=["清洗"])
def revoke_cleaning(cleaning_id: int, conn=Depends(get_conn)):
    """撤销清洗验证。撤销后未签发结论实时重算（残留恢复）；已签发标签不变。"""
    return service.revoke_cleaning(conn, cleaning_id)


@app.post("/cleanings/{cleaning_id}/revoke/preview", status_code=200, tags=["预演"])
def preview_revoke_cleaning(cleaning_id: int, conn=Depends(get_conn)):
    """清洗撤销预演（不落库）：返回确有差异的下游批次（含设备残留传播）
    及其已签发标签相对变更前后实时结论的差异，并给出提交凭据。"""
    return service.preview_revoke_cleaning(conn, cleaning_id)


# ---------------------------------------------------------------- 预演提交

@app.post("/previews/{preview_id}/commit", tags=["预演"])
def commit_preview(preview_id: str, conn=Depends(get_conn)):
    """凭预演凭据原子应用变更：影响本次差异的记录已变化则 409（需重新预演），
    无关记录更新不使凭据失效。返回实际影响（与预演同口径）。"""
    return service.commit_preview(conn, preview_id)


# ---------------------------------------------------------------- 溯源查询

@app.get("/batches/{batch_id}/trace", tags=["溯源"])
def trace_batch(batch_id: str, conn=Depends(get_conn)):
    """实时溯源：返回每项过敏原与待确认状态的来源路径及当前有效依据，
    并附封存放行状态（是否被生效调查封存、调查号与传播路径）。"""
    return service.trace_response(conn, batch_id)


@app.get("/batches/{batch_id}/release", tags=["调查封存"])
def get_batch_release(batch_id: str, conn=Depends(get_conn)):
    """按批次查询放行状态：封存状态、仍生效的调查号及各自传播路径。"""
    return service.batch_release(conn, batch_id)


# ---------------------------------------------------------------- 调查封存

@app.post("/investigations", status_code=201, tags=["调查封存"])
def open_investigation(data: InvestigationCreate, conn=Depends(get_conn)):
    """疑似交叉接触：以批次号、唯一调查号和原因登记调查封存。

    系统沿加工投入链及同设备相邻加工的残留链（有效清洗截断残留传播）
    实时计算受影响批次；封存与过敏原声明是否为空无关。封存与受影响状态
    变更在同一事务原子提交，并沿用游标事件流发布实际变化。
    """
    return service.open_investigation(conn, data)


@app.get("/investigations", tags=["调查封存"])
def list_investigations(status: str | None = None, conn=Depends(get_conn)):
    """查询调查（可按 ?status=open/lifted 过滤），含各自封存传播路径。"""
    return service.list_investigations(conn, status)


@app.get("/investigations/{investigation_id}", tags=["调查封存"])
def get_investigation(investigation_id: str, conn=Depends(get_conn)):
    return service.get_investigation(conn, investigation_id)


@app.post("/investigations/{investigation_id}/lift", tags=["调查封存"])
def lift_investigation(investigation_id: str, conn=Depends(get_conn)):
    """解除调查封存：只移除该调查自身的影响（多调查叠加时其他封存保留）；
    重复解除/未知调查号明确报错；解除与受影响状态变更原子提交并发事件。"""
    return service.lift_investigation(conn, investigation_id)


# ---------------------------------------------------------------- 变更事件订阅

@app.get("/changes/snapshot", tags=["变更订阅"])
def get_changes_snapshot(conn=Depends(get_conn)):
    """初始化读取：同一状态下的现存批次实时结论（含依据）与游标。
    之后以该游标调用 /changes/events 即可不重不漏地接续增量变化。"""
    return service.changes_snapshot(conn)


@app.get("/changes/events", tags=["变更订阅"])
def get_change_events(cursor: int = 0,
                      limit: int = Query(default=100, ge=1, le=1000),
                      upper: int | None = None,
                      conn=Depends(get_conn)):
    """按游标分页读取变更事件（seq 升序）。

    - `cursor`：起始游标（快照游标或上一页的 next_cursor），返回 seq > cursor 的事件；
    - `upper`：本次分页会话的固定上界，首页缺省取当前最大序号，翻页时原样回传，
      分页期间的新写入不会进入已开始的读取；
    - 无效游标（负数、超过当前最大序号、upper < cursor 等）返回 400。
    """
    return service.list_change_events(conn, cursor, limit, upper)


# ---------------------------------------------------------------- 标签

@app.post("/batches/{batch_id}/labels", status_code=201, tags=["标签"])
def issue_label(batch_id: str, data: LabelCreate, conn=Depends(get_conn)):
    """签发标签：冻结签发时刻的结论与依据，后续变更不影响本标签。
    可通过 supersedes_id 关联被替代的旧版标签。"""
    return service.issue_label(conn, batch_id, data.supersedes_id)


@app.get("/batches/{batch_id}/labels", tags=["标签"])
def list_labels(batch_id: str, conn=Depends(get_conn)):
    return service.list_labels(conn, batch_id)


@app.get("/labels/{label_id}", tags=["标签"])
def get_label(label_id: int, conn=Depends(get_conn)):
    return service.get_label(conn, label_id)
