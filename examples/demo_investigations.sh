#!/usr/bin/env bash
# 调查封存（疑似交叉接触）端到端演示：
#   登记 -> 封存传播（投入链 + 同设备残留链，有效清洗截断）-> 查询 ->
#   标签/新加工约束 -> 清洗截断 / 撤销恢复 -> 多调查叠加与解除 -> 事件流
# 用法：BASE=http://localhost:8000 ./examples/demo_investigations.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

step() { echo -e "\n\033[1;36m== $* ==\033[0m"; }
req() { # METHOD PATH JSON
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo
}
q() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(json.dumps(d,ensure_ascii=False,indent=2))'; }

step "0. 基础数据：原料（过敏原声明留空，封存与声明无关）与共线加工"
req POST /materials '{"batch_id":"RM-MILK","allergens":[],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-OAT","allergens":[],"declared_at":"2026-01-02T06:00:00Z"}'
# EQ-1: P1 奶 -> OUT-1；P2 燕麦 -> OUT-2（与 P1 之间无清洗，继承残留）
req POST /processings '{"processing_id":"P1","equipment_id":"EQ-1","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-MILK"],"outputs":["OUT-1"]}'
req POST /processings '{"processing_id":"P2","equipment_id":"EQ-1","start_utc":"2026-01-02T10:00:00Z","end_utc":"2026-01-02T11:00:00Z","inputs":["RM-OAT"],"outputs":["OUT-2"]}'

step "1. 登记疑似交叉接触调查封存（批次号 + 唯一调查号 + 原因）"
req POST /investigations '{"investigation_id":"INV-001","batch_id":"RM-MILK","reason":"共线疑似奶残留交叉接触"}'

step "2. 按批次查询放行状态：OUT-2 held，含仍生效调查号与残留传播路径"
req GET /batches/OUT-2/release | q

step "3. 实时溯源同步附 release（过敏原仍为空，但封存独立成立）"
req GET /batches/OUT-2/trace | q

step "4. 封存批次不得签发标签 / 不得作为新加工投入（均 409，失败不留痕）"
req POST /batches/OUT-2/labels '{}'
req POST /processings '{"processing_id":"P9","equipment_id":"EQ-2","start_utc":"2026-01-02T12:00:00Z","end_utc":"2026-01-02T13:00:00Z","inputs":["OUT-2"],"outputs":["OUT-9"]}'

step "4b. 设备残留会封存新产出时同样拒绝登记（EQ-1 上 P2 之后、无清洗）"
req POST /processings '{"processing_id":"P8","equipment_id":"EQ-1","start_utc":"2026-01-02T12:00:00Z","end_utc":"2026-01-02T13:00:00Z","inputs":["RM-OAT"],"outputs":["OUT-8"]}'

step "5. 错误约定：重复调查号 409 / 未知批次 400 / 未知调查号解除 404"
req POST /investigations '{"investigation_id":"INV-001","batch_id":"RM-OAT","reason":"重复调查号"}'
req POST /investigations '{"investigation_id":"INV-ERR","batch_id":"GHOST","reason":"未知批次"}'
req POST /investigations/GHOST/lift

step "6. 两次加工之间登记有效清洗 -> 残留传播被截断（先解除再重开演示实时重算）"
req POST /investigations/INV-001/lift
req POST /cleanings '{"equipment_id":"EQ-1","cleaned_at":"2026-01-02T09:30:00Z","verified":true}'
req POST /investigations '{"investigation_id":"INV-002","batch_id":"RM-MILK","reason":"清洗后重开调查"}'
step "OUT-2 仍经投入链？不——P2 投入为 RM-OAT；RM-MILK 的残留被清洗截断，OUT-2 放行"
req GET /batches/OUT-2/release | q

step "7. 撤销清洗 -> 残留恢复，OUT-2 重新被 INV-002 封存（同步重算 + 事件）"
CID="$(curl -sS "$BASE/cleanings" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')"
req POST "/cleanings/$CID/revoke"
req GET /batches/OUT-2/release | q

step "8. 多调查叠加：另对 OUT-2 本体开 INV-003；解除 INV-002 只移除它自身的影响"
req POST /investigations '{"investigation_id":"INV-003","batch_id":"OUT-2","reason":"标签复核异常"}'
req GET /batches/OUT-2/release | q
req POST /investigations/INV-002/lift
step "INV-002 解除后 OUT-2 仍被 INV-003 封存；重复解除 INV-002 报 409"
req GET /batches/OUT-2/release | q
req POST /investigations/INV-002/lift
req POST /investigations/INV-003/lift
step "全部解除后可签发标签（旧约束消失）"
req POST /batches/OUT-2/labels '{}'

step "9. 变更事件流：封存/解除按批次号顺序发布实际变化（release 字段附加，分页约定不变）"
req GET "/changes/events?cursor=0&limit=50" | python3 -c 'import sys,json
d=json.load(sys.stdin)
for e in d["events"]:
    if e["op"].startswith("investigation") or e["op"] in ("cleaning_register","cleaning_revocation"):
        print(e["seq"], e["op"], e["batch_id"], "held", e["after"]["release"]["held"])'
