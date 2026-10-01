#!/usr/bin/env bash
# 端到端演示：登记 -> 加工 -> 残留继承 -> 清洗 -> 撤销 -> 声明更正 -> 标签签发
# 用法：BASE=http://localhost:8000 ./examples/demo.sh
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

step "1. 登记原料批次（含过敏原 / 待确认）"
req POST /materials '{"batch_id":"RM-MILK","allergens":["milk"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-OAT","allergens":["gluten"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-MYST","pending":true,"declared_at":"2026-01-02T06:00:00Z"}'
# 另登记一个“误登”原料，第 8b 步用投入纠正把 P2 改回正确列表
req POST /materials '{"batch_id":"RM-WRONG","allergens":["peanut"],"declared_at":"2026-01-02T06:00:00Z"}'

step "2. 登记加工批次（EQ-1 共线：先奶制品，后燕麦品；P2 投入先误登）"
req POST /processings '{"processing_id":"P1","equipment_id":"EQ-1","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-MILK"],"outputs":["OUT-MILK-1"]}'
req POST /processings '{"processing_id":"P2","equipment_id":"EQ-1","start_utc":"2026-01-02T10:00:00Z","end_utc":"2026-01-02T11:00:00Z","inputs":["RM-WRONG","RM-MYST"],"outputs":["OUT-OAT-1"]}'

step "3. 溯源 OUT-OAT-1：peanut 来自误登投入，milk 来自设备残留，pending 来自 RM-MYST（第 8b 步纠正投入）"
req GET /batches/OUT-OAT-1/trace

step "4. 登记两次加工之间的已验证清洗 -> 残留被清除"
req POST /cleanings '{"equipment_id":"EQ-1","cleaned_at":"2026-01-02T09:30:00Z","verified":true}'
req GET /batches/OUT-OAT-1/trace

step "5. 撤销清洗 -> 未签发结论实时重算，milk 残留恢复"
req POST /cleanings/1/revoke
req GET /batches/OUT-OAT-1/trace

step "6. 原料声明更正：RM-OAT 实际含 sesame"
req POST /materials/RM-OAT/declarations '{"allergens":["gluten","sesame"]}'
req GET /batches/OUT-OAT-1/trace

step "7. 签发标签（冻结当前结论）；再更正声明后签发关联旧版的新标签"
req POST /batches/OUT-OAT-1/labels '{}'
req POST /materials/RM-OAT/declarations '{"allergens":["gluten","sesame","soy"]}'
req POST /batches/OUT-OAT-1/labels '{"supersedes_id":1}'
step "旧标签保持不变"
req GET /labels/1
step "新标签反映最新结论"
req GET /labels/2

step "8. 非法登记示例（均被拒绝且不影响已有数据）"
req POST /processings '{"processing_id":"P-BAD","equipment_id":"EQ-1","start_utc":"2026-01-02T08:30:00Z","end_utc":"2026-01-02T09:30:00Z","inputs":["RM-OAT"],"outputs":["OUT-X"]}'
req POST /processings '{"processing_id":"P-LOOP","equipment_id":"EQ-9","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-OAT"],"outputs":["RM-OAT"]}'
req POST /processings '{"processing_id":"P-TIME","equipment_id":"EQ-9","start_utc":"2026-01-02T09:00:00Z","end_utc":"2026-01-02T08:00:00Z","inputs":["RM-OAT"],"outputs":["OUT-Y"]}'

step "8b. 加工投入纠正：P2 投入误登含 RM-WRONG，整表替换为正确的 RM-OAT + RM-MYST（加工号/设备/时段/产出不变）"
req POST /processings/P2/inputs/correction '{"inputs":["RM-OAT","RM-MYST"]}'
step "纠正后 OUT-OAT-1 实时重算：gluten 来自投入、peanut 消失，milk 仍经设备残留，pending 保留"
req GET /batches/OUT-OAT-1/trace
step "投入集合未变（顺序不同）：幂等，不产生事件"
req POST /processings/P2/inputs/correction '{"inputs":["RM-MYST","RM-OAT"]}'
step "非法纠正（未登记投入 400 / 自环 409 / 空列表 422 / 加工不存在 404）"
req POST /processings/P2/inputs/correction '{"inputs":["GHOST"]}'
req POST /processings/P2/inputs/correction '{"inputs":["OUT-OAT-1"]}'
req POST /processings/P2/inputs/correction '{"inputs":[]}'
req POST /processings/NOPE/inputs/correction '{"inputs":["RM-OAT"]}'

step "9. 声明更正预演（不落库）：查看差异与已签发标签偏差，取得提交凭据"
req POST /materials/RM-OAT/declarations/preview '{"allergens":["gluten","sesame","tree-nut"]}'

step "10. 清洗撤销预演（不落库）：设备残留恢复，含下游批次与标签差异"
# 先补一条 EQ-1 的已验证清洗（步骤 5 的清洗已被直接撤销）
CID="$(curl -sS -X POST "$BASE/cleanings" -H 'Content-Type: application/json' \
  -d '{"equipment_id":"EQ-1","cleaned_at":"2026-01-02T09:20:00Z","verified":true}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
req POST "/cleanings/$CID/revoke/preview"

step "11. 凭预演凭据原子提交（取上一步返回的 preview_id）"
PID="$(curl -sS -X POST "$BASE/cleanings/$CID/revoke/preview" | python3 -c 'import sys,json;print(json.load(sys.stdin)["preview_id"])')"
req POST "/previews/$PID/commit"
step "已签发标签仍不改写"
req GET /labels/1
step "凭据一次性：再次提交将被拒绝"
req POST "/previews/$PID/commit"

step "12. 变更订阅：初始化读取（现存批次实时结论 + 游标，同一状态）"
SNAP="$(curl -sS "$BASE/changes/snapshot")"
echo "$SNAP" | python3 -c 'import sys,json; d=json.load(sys.stdin)
print("cursor =", d["cursor"])
for b in d["batches"]:
    c = b["conclusion"]
    print(" ", b["batch_id"], "(" + b["kind"] + "):",
          "allergens =", c["allergens"], "pending =", c["pending"])'
CURSOR="$(echo "$SNAP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')"

step "13. 触发一次变更（声明更正），按游标增量读取事件"
req POST /materials/RM-OAT/declarations '{"allergens":["gluten","sesame","soy","mustard"]}'
req GET "/changes/events?cursor=$CURSOR&limit=10"

step "14. 分页读取：固定上界 upper，翻页期间的新写入不进入本次读取"
PAGE1="$(curl -sS "$BASE/changes/events?cursor=0&limit=2")"
echo "$PAGE1" | python3 -c 'import sys,json; d=json.load(sys.stdin)
print("page1:", [(e["seq"], e["batch_id"]) for e in d["events"]], "next_cursor =", d["next_cursor"], "upper =", d["upper"])'
NEXT="$(echo "$PAGE1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["next_cursor"])')"
UPPER="$(echo "$PAGE1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["upper"])')"
req POST /materials '{"batch_id":"RM-LATE","allergens":["peanut"],"declared_at":"2026-01-03T06:00:00Z"}'
step "带固定上界翻页（本次读取看不到刚写入的 RM-LATE）"
PAGE2="$(curl -sS "$BASE/changes/events?cursor=$NEXT&limit=1000&upper=$UPPER")"
echo "$PAGE2" | python3 -c 'import sys,json; d=json.load(sys.stdin)
print("page2:", [(e["seq"], e["batch_id"]) for e in d["events"]])
print("has_more =", d["has_more"], " next_cursor =", d["next_cursor"], " latest =", d["latest"], "（latest > upper 表示期间有新写入）")'
step "本轮读完（has_more=false）后开启新一轮：不传 upper，从 next_cursor 续读（含 RM-LATE）"
req GET "/changes/events?cursor=$UPPER&limit=2"
step "无效游标被拒绝（400）"
req GET "/changes/events?cursor=999999"
