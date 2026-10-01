# 过敏原溯源服务（共线生产食品批次）

基于 **Python + FastAPI + SQLite** 的过敏原溯源服务，面向共线（共享设备）生产场景：

- 登记**原料批次**的过敏原集合或**待确认**状态（声明可更正，保留版本历史）；
- 登记**加工批次**：设备、UTC 起止时间、投入批次、产出批次；
  已登记加工的**投入登记错误可纠正**（整表替换投入列表，加工号/设备/时段/产出不变，
  同事务校验、随后按现有规则实时重算并仅为实际变化批次记录事件）；
- 产出继承投入的过敏原/待确认状态，并继承**设备上一次加工的残留**；
  只有两次加工之间**已验证且未撤销**的清洗才清除残留；
- 查询任一批次，返回**每项过敏原与待确认状态的来源路径**及当前有效依据；
- 原料声明更正、清洗撤销正式落库前可先**预演（不落库）**：返回确有差异的下游
  批次（过敏原、待确认状态、来源路径及依据的前后变化，设备残留传播同样计入）、
  这些批次**已签发标签**相对变更前后实时结论的差异，以及一次性提交凭据；
  提交时若影响差异的记录已变化则拒绝（需重新预演），无关记录更新不使凭据失效；
- 原料声明更正、清洗撤销后，**未签发结论按当前有效记录实时重算**；
- **签发标签**冻结签发时刻的结论与依据，后续变更不改旧标签，
  可签发关联旧版（`supersedes_id`）的新标签；
- **变更事件订阅**：下游按游标增量同步——初始化读取返回同一状态下的现存批次
  实时结论与游标，之后分页读取事件（全局递增序号、触发操作、批次号、变更前后
  完整结论与依据）；业务写入与事件同事务，失败/预演/无实际变化不产事件；
- 无效时序、来源环、设备时间重叠、批次来源冲突一律**拒绝并保持原状态**
  （校验与写入在同一事务内，失败即回滚）。

## 快速启动

### Docker Compose（推荐，一键启动）

```bash
docker compose up --build
```

依赖安装与数据库初始化（建表）均在容器启动时自动完成，无需额外步骤。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## 地址与端口

| 项目 | 地址 |
|---|---|
| 服务地址 | http://localhost:8000 |
| 交互式 API 文档 (Swagger) | http://localhost:8000/docs |
| OpenAPI 规格 | http://localhost:8000/openapi.json |
| 健康检查 | http://localhost:8000/health |

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_PORT` | `8000` | 服务监听端口（Compose 下同时作为宿主机映射端口） |
| `DB_PATH` | 容器内 `/data/allergen.db`，本地 `app.db` | SQLite 数据库文件路径 |

修改宿主机端口：`APP_PORT=9000 docker compose up --build`，然后访问 http://localhost:9000 。

## 数据与规则模型

- **原料批次**：当前有效声明 = 最新版本声明（过敏原集合 + 是否待确认）。
- **批次可用时间**：原料 = 首次登记时间；加工产出 = 产出加工的结束时间。
  投入批次的可用时间必须 ≤ 加工开始时间。
- **设备残留**：加工开始时，若同一设备存在上一次加工（`end_utc ≤ 本次 start_utc`），
  且两次加工之间没有 `verified=true 且未撤销` 的清洗
  （`上次 end_utc ≤ cleaned_at ≤ 本次 start_utc`），
  则上一次加工产出的全部过敏原/待确认状态作为残留继承。
- **标签**：签发时将结论（每项过敏原/待确认的来源路径）与依据
  （命中的原料声明版本、赖以清除残留的清洗记录）整体冻结为快照，永不可变。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/materials` | 登记原料批次（过敏原集合或待确认状态） |
| GET | `/materials` / `/materials/{batch_id}` | 查询原料批次及当前有效声明 |
| POST | `/materials/{batch_id}/declarations` | 更正声明（追加新版本） |
| POST | `/materials/{batch_id}/declarations/preview` | 声明更正预演（不落库），返回差异报告 + 提交凭据 |
| POST | `/processings` | 登记加工批次（含全部校验） |
| POST | `/processings/{id}/inputs/correction` | 纠正加工投入（整表替换，加工号/设备/时段/产出不变） |
| GET | `/processings` / `/processings/{id}` | 查询加工批次 |
| POST | `/cleanings` | 登记设备清洗（`verified` 标记是否已验证） |
| GET | `/cleanings` / `/cleanings/{id}` | 查询清洗记录（可按 `?equipment_id=` 过滤） |
| POST | `/cleanings/{id}/revoke` | 撤销清洗验证 |
| POST | `/cleanings/{id}/revoke/preview` | 清洗撤销预演（不落库），返回差异报告 + 提交凭据 |
| POST | `/previews/{preview_id}/commit` | 凭预演凭据原子应用变更（一次性） |
| GET | `/batches/{batch_id}/trace` | 实时溯源：每项过敏原/待确认的来源路径 + 依据 |
| POST | `/batches/{batch_id}/labels` | 签发标签（可带 `supersedes_id` 关联旧版） |
| GET | `/batches/{batch_id}/labels` / `/labels/{id}` | 查询标签 |
| GET | `/changes/snapshot` | 初始化读取：同一状态下的现存批次实时结论 + 游标 |
| GET | `/changes/events` | 按游标分页读取变更事件（固定上界，无效游标拒绝） |

错误约定：`400` 无效时序/参数（如投入批次在加工开始前不可用、无效游标）；
`404` 资源不存在；`409` 冲突（设备时间重叠、来源环、批次号占用、重复撤销、
预演凭据不存在/已使用/已过期，或影响差异的记录已变化需重新预演）；
`422` 请求格式错误（如时间缺少时区）。所有时间必须为带时区的 ISO8601（建议 UTC `Z`）。

## 变更预演（dry-run）与凭据提交

在**原料声明更正**或**清洗撤销**正式落库前，可先对“拟变更”进行预演。预演只在
内存中叠加假设状态重算，**不写库**；已有更正、撤销与标签查询接口完全兼容，
已签发标签永不改写。

预演响应（`dry_run=true, applied=false`）包含：

- `preview_id`：提交凭据（进程内一次性令牌，服务重启后失效，需重新预演）；
- `change`：拟应用的变更（类型、目标、新版本号/清洗记录等）；
- `affected_batches[]`：仅列**变更前后确有差异**的批次（含原料目标本身；
  设备残留传播的下游批次同样计入），每项含：
  - `before` / `after`：完整实时结论（过敏原集合、待确认、逐项来源路径）与依据
    （命中的原料声明版本、赖以清除残留的清洗）；
  - `diff`：结论差异（过敏原 `added/removed`、`pending` 翻转、来源路径前后对照）
    与依据差异（声明版本、清洗依据的前后对照）；
  - `issued_labels[]`：该批次每张已签发标签的冻结结论，以及
    `diff_vs_before` / `diff_vs_after`（标签相对变更前、变更后实时结论的差异，
    无差异为 `null`）；
- `commit`：提交方法与路径。

提交 `POST /previews/{preview_id}/commit` 在单个 `BEGIN IMMEDIATE` 事务内重新核对
指纹并写入：若**影响本次差异**的记录（目标/路径上的原料声明版本、相关加工及其
残留窗口内有效的清洗、受影响批次的已签发标签）与预演时一致，则原子应用并返回
实际影响报告（`dry_run=false, applied=true`，结构与预演相同）；否则 `409` 拒绝、
不落库并要求重新预演。无关记录（其他原料/设备上的加工、窗口内未验证或已撤销的
清洗、不受影响批次的标签等）的新增更新**不会**使凭据失效。

```bash
BASE=http://localhost:8000

# 预演声明更正（过敏原 + 待确认），不落库
curl -s $BASE/materials/RM-OAT/declarations/preview -X POST \
  -H 'Content-Type: application/json' \
  -d '{"allergens": ["gluten", "sesame"], "pending": false}'

# 预演清洗撤销（设备残留恢复计入下游批次）
curl -s $BASE/cleanings/1/revoke/preview -X POST

# 用上一步返回的 preview_id 原子提交（返回实际影响）
curl -s $BASE/previews/<preview_id>/commit -X POST
```

## 加工投入纠正

加工已登记后若发现**投入批次登记错误**，可用加工号 + **完整的正确投入列表**
纠正记录：

```
POST /processings/{processing_id}/inputs/correction
{"inputs": ["RM-A", "RM-B"]}
```

规则：

- 请求体为**整表替换**（不是增量补丁），须提交完整的正确投入列表；
- **原加工号、设备、起止时段、产出批次保持原值**，只替换投入边；
- 投入须**非空、无重复、均已登记且在加工开始前可用**（可用时间口径与登记一致：
  原料=首次登记时间，加工产出=产出加工结束时间）；纠正后的来源关系**不得成环**
  （本加工产出不得是新投入，且其下游不得间接到达任一新投入）；
- **校验与替换在同一 `BEGIN IMMEDIATE` 事务内完成**，任一校验失败即回滚，
  记录与事件均保持原状；加工号不存在返回 `404`，未登记/开工前不可用 `400`，
  成环 `409`，空列表/重复 `422`；
- 替换后按现有规则**实时重算相关批次**的过敏原、待确认状态、来源路径与依据，
  包括沿同设备**后续加工的残留传播**；已签发标签**保留原快照**不受影响；
- 变更沿用既有变更订阅事件的字段与全局递增序号：`op` =
  `processing_input_correction`，`op_ref` = `{"processing_id": "..."}`，
  同次纠错**仅为实际变化的批次按批次号顺序**记录事件（固定上界分页等约定不变）；
  **投入集合未变时不替换、不产生事件**（按集合比较，顺序不同视为未变）。

```bash
BASE=http://localhost:8000

# 纠正 P2 的投入（原误登为 RM-MILK，实际为 RM-OAT）；设备/时段/产出不变
curl -s $BASE/processings/P2/inputs/correction -X POST \
  -H 'Content-Type: application/json' \
  -d '{"inputs": ["RM-OAT"]}'

# 多投入的完整正确列表
curl -s $BASE/processings/P2/inputs/correction -X POST \
  -H 'Content-Type: application/json' \
  -d '{"inputs": ["RM-OAT", "RM-SOY"]}'

# 实时溯源随之重算（含后续设备残留下游）；已签发标签仍是签发时快照
curl -s $BASE/batches/OUT-OAT-1/trace
```

## 变更事件订阅（游标增量同步）

下游系统按“**初始化快照 + 游标增量事件**”两阶段同步溯源变化：

1. **初始化读取** `GET /changes/snapshot`：在一致快照（读事务）内返回
   `cursor` 与现存全部批次的实时结论（`conclusion`：过敏原/待确认/来源路径）
   与依据（`basis`：命中的声明版本、清除残留的清洗），按批次号排序。
2. **增量读取** `GET /changes/events?cursor=<c>&limit=<n>[&upper=<u>]`：
   从快照游标（或上一页 `next_cursor`）起，按 `seq` 升序分页拉取事件，
   逐条应用到本地副本即可与线上结论保持一致。

每条事件：

```json
{
  "seq": 14,                                  // 全局递增序号
  "op": "declaration_correction",             // 触发操作
  "op_ref": {"batch_id": "RM-OAT", "new_version": 4, "via": "direct"},
  "batch_id": "OUT-OAT-1",                    // 批次号
  "kind": "product",
  "before": {"conclusion": {...}, "basis": {...}},   // 变更前完整结论与依据
  "after":  {"conclusion": {...}, "basis": {...}},   // 变更后完整结论与依据
  "recorded_at": "2026-09-30T08:00:00Z"
}
```

事件生成规则：

- **触发操作**（`op`）：`material_register`（原料登记）、`declaration_correction`
  （原料更正，含预演提交，`op_ref.via` 区分 `direct` / `preview_commit`）、
  `processing_register`（加工登记）、`processing_input_correction`（加工投入纠正，
  `op_ref` 为 `{"processing_id": ...}`）、`cleaning_register`（清洗登记）、
  `cleaning_revocation`（清洗撤销，含预演提交）。
- **新批次**（原料登记、加工产出）`before` 为 `null`（前态为空）。
- 仅为**结论、来源路径或依据确有变化**的批次生成事件；同次操作的多条事件
  按批次号排序（`seq` 升序即批次号序）。
- 业务写入与事件在**同一事务**落库：校验失败回滚、预演（dry-run）均不产事件；
  未验证清洗、窗口外清洗、标签签发等不改变实时溯源的操作也不产事件，
  已签发标签永远不改写。
- 乱序补登加工（插入到同设备两次加工之间）会改变后续批次的残留来源，
  这类既有批次的结论/路径变化同样产生事件。

分页与游标约定：

- 首页可不传 `upper`，服务端固定本次分页会话的上界为当前最大 `seq` 并在
  响应中返回；**翻页时原样回传 `upper`**，分页期间的新写入（`seq > upper`）
  不会进入已开始的读取。
- 响应字段：`events`、`next_cursor`（下一页游标）、`upper`（固定上界）、
  `has_more`（`next_cursor < upper`）、`latest`（当前最大序号；
  `latest > upper` 表示分页期间有新写入，本轮读完后可开启新一轮）。
- 无效游标（负数、超过当前最大序号、`upper < cursor` 或 `upper` 超界）
  返回 `400`；非整数参数返回 `422`。

```bash
BASE=http://localhost:8000

# 1) 初始化：拿到游标与现存批次实时结论
CURSOR=$(curl -s $BASE/changes/snapshot | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')

# 2) 增量：首页（服务端固定上界 upper）
curl -s "$BASE/changes/events?cursor=$CURSOR&limit=100"
# -> {"events":[...], "next_cursor":N, "upper":U, "has_more":true, "latest":L}

# 3) 翻页：回传固定上界，直到 has_more=false
curl -s "$BASE/changes/events?cursor=N&limit=100&upper=U"

# 4) 本轮读完后，用 next_cursor 开启新一轮（重新固定上界）
curl -s "$BASE/changes/events?cursor=U&limit=100"
```

## 调用示例（curl）

完整演示脚本（覆盖下述全部流程）：`./examples/demo.sh`

```bash
BASE=http://localhost:8000

# 1. 登记原料批次
curl -s $BASE/materials -X POST -H 'Content-Type: application/json' -d '{
  "batch_id": "RM-MILK", "allergens": ["milk"],
  "declared_at": "2026-01-02T06:00:00Z"}'
curl -s $BASE/materials -X POST -H 'Content-Type: application/json' -d '{
  "batch_id": "RM-MYST", "pending": true,
  "declared_at": "2026-01-02T06:00:00Z"}'   # 待确认状态

# 2. 登记加工批次（同一设备 EQ-1 先后两批）
curl -s $BASE/processings -X POST -H 'Content-Type: application/json' -d '{
  "processing_id": "P1", "equipment_id": "EQ-1",
  "start_utc": "2026-01-02T08:00:00Z", "end_utc": "2026-01-02T09:00:00Z",
  "inputs": ["RM-MILK"], "outputs": ["OUT-MILK-1"]}'
curl -s $BASE/processings -X POST -H 'Content-Type: application/json' -d '{
  "processing_id": "P2", "equipment_id": "EQ-1",
  "start_utc": "2026-01-02T10:00:00Z", "end_utc": "2026-01-02T11:00:00Z",
  "inputs": ["RM-OAT"], "outputs": ["OUT-OAT-1"]}'

# 3. 溯源：milk 经设备残留进入 OUT-OAT-1，路径逐步可见
curl -s $BASE/batches/OUT-OAT-1/trace
# -> allergen_paths.milk: RM-MILK --(P1, input)--> OUT-MILK-1
#    --(P2, equipment_residue, EQ-1)--> OUT-OAT-1

# 4. 两次加工之间的已验证清洗 -> 残留清除
curl -s $BASE/cleanings -X POST -H 'Content-Type: application/json' -d '{
  "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z", "verified": true}'

# 5. 撤销清洗 -> 未签发结论实时重算，milk 残留恢复
curl -s $BASE/cleanings/1/revoke -X POST

# 6. 原料声明更正（追加版本，实时结论随之更新）
curl -s $BASE/materials/RM-OAT/declarations -X POST \
  -H 'Content-Type: application/json' \
  -d '{"allergens": ["gluten", "sesame"]}'

# 6b. 也可以先预演再凭凭据提交（预演不落库；示例见上节“变更预演”）
curl -s $BASE/materials/RM-OAT/declarations/preview -X POST \
  -H 'Content-Type: application/json' \
  -d '{"allergens": ["gluten", "sesame", "soy"]}'
# -> 返回 preview_id 后：curl -s $BASE/previews/<preview_id>/commit -X POST

# 7. 签发标签（冻结当前结论）；变更后签发关联旧版的新标签
curl -s $BASE/batches/OUT-OAT-1/labels -X POST \
  -H 'Content-Type: application/json' -d '{}'
curl -s $BASE/batches/OUT-OAT-1/labels -X POST \
  -H 'Content-Type: application/json' -d '{"supersedes_id": 1}'
curl -s $BASE/labels/1   # 旧标签保持签发时的结论不变
```

## 项目结构

```
app/
  main.py      # FastAPI 路由与错误映射
  service.py   # 业务规则：校验、溯源计算、标签签发、变更预演、变更事件流
  db.py        # SQLite 连接、建表、写/读事务
  schemas.py   # 请求模型（强制时区感知时间）
tests/test_app.py   # 45 个端到端测试
examples/demo.sh    # 全流程 curl 演示（含变更订阅）
Dockerfile / docker-compose.yml / requirements*.txt
```
