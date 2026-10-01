#!/usr/bin/env bash
# 付款额度预留指令（两阶段付款）端到端调用示例。
# 前置：服务已启动（docker compose up -d）。
# 用法: BASE=http://localhost:8000 bash examples/demo_instructions.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

# ---- 准备：作品 / 权利人 / 协议 / 播放 / 单据（A 得 60 分）----
say "1) 登记作品、权利人 RS-A 与 100% 协议"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-RESERVE","title":"预留付款演示作品"}' >/dev/null
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' \
  -d '{"code":"RS-A","name":"预留演示权利人"}' >/dev/null
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-RESERVE",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[{"holder_code":"RS-A","percent":"100"}]
}' >/dev/null
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' -d '{
  "import_id":"PLAY-R-1","work_code":"W-RESERVE",
  "occurred_at":"2026-02-10T20:00:00+08:00","amount":60}' >/dev/null
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' -d '{
  "start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool

# ---- 预留 ----
say "2) 预留 40 分：可用额 60-40=20，返回指令状态 pending 与 available=20（201）"
$C -X POST "$BASE/payment-instructions" -H 'Content-Type: application/json' -d '{
  "holder_code":"RS-A",
  "instruction_ref":"PI-20260301-0001",
  "amount":40
}' | python3 -m json.tool

say "3) 同指令号同内容重试 => 200 status=duplicate，返回原指令，不重复预留"
$C -X POST "$BASE/payment-instructions" -H 'Content-Type: application/json' -d '{
  "holder_code":"RS-A",
  "instruction_ref":"PI-20260301-0001",
  "amount":40
}' | python3 -m json.tool

say "4) 同指令号异内容（金额 10）=> 409 instruction_ref_conflict"
$C -i -X POST "$BASE/payment-instructions" -H 'Content-Type: application/json' -d '{
  "holder_code":"RS-A",
  "instruction_ref":"PI-20260301-0001",
  "amount":10}' | head -1

say "5) 超可用额预留（21 > 可用 20）=> 409 instruction_exceeds_balance，不留指令"
$C -i -X POST "$BASE/payment-instructions" -H 'Content-Type: application/json' -d '{
  "holder_code":"RS-A",
  "instruction_ref":"PI-20260301-0099",
  "amount":21}' | head -1

say "6) 再预留 20 分恰好占满：available=0"
$C -X POST "$BASE/payment-instructions" -H 'Content-Type: application/json' -d '{
  "holder_code":"RS-A",
  "instruction_ref":"PI-20260301-0002",
  "amount":20}' | python3 -m json.tool

# ---- 原付款接口兼容 ----
say "7) 原 POST /payments 新付款须避开待确认预留：可用 0，付 1 分 => 409"
$C -i -X POST "$BASE/payments" -H 'Content-Type: application/json' -d '{
  "holder_code":"RS-A","external_ref":"BANK-DIRECT-0001","amount":1}' | head -1

# ---- 取消 ----
say "8) 取消第二笔待确认指令 => 释放 20 分，available 回到 20"
$C -X POST "$BASE/payment-instructions/PI-20260301-0002/cancel" \
  | python3 -m json.tool
say "9) 重复取消 => 200 status=duplicate（幂等）"
$C -X POST "$BASE/payment-instructions/PI-20260301-0002/cancel" \
  | python3 -m json.tool

# ---- 确认 ----
say "10) 确认第一笔：提交外部付款流水号，原子登记付款并结束预留（201）"
$C -X POST "$BASE/payment-instructions/PI-20260301-0001/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"external_ref":"BANK-20260301-0001"}' | python3 -m json.tool

say "11) 同流水号确认重试 => 200 status=duplicate，返回原付款，不重复入账"
$C -X POST "$BASE/payment-instructions/PI-20260301-0001/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"external_ref":"BANK-20260301-0001"}' | python3 -m json.tool

say "12) 换流水号确认已确认指令 => 409 instruction_already_confirmed"
$C -i -X POST "$BASE/payment-instructions/PI-20260301-0001/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"external_ref":"BANK-OTHER"}' | head -1

say "13) 取消已确认指令 => 409 instruction_status_conflict"
$C -i -X POST "$BASE/payment-instructions/PI-20260301-0001/cancel" | head -1

say "14) 流水号被其他付款占用时确认待确认指令 => 409 payment_ref_occupied，指令保持 pending"
$C -X POST "$BASE/payment-instructions" -H 'Content-Type: application/json' -d '{
  "holder_code":"RS-A","instruction_ref":"PI-20260301-0003","amount":5}' >/dev/null
$C -X POST "$BASE/payments" -H 'Content-Type: application/json' -d '{
  "holder_code":"RS-A","external_ref":"BANK-TAKEN","amount":5}' >/dev/null
$C -i -X POST "$BASE/payment-instructions/PI-20260301-0003/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"external_ref":"BANK-TAKEN"}' | head -1
# 换新流水号即可正常确认
$C -X POST "$BASE/payment-instructions/PI-20260301-0003/confirm" \
  -H 'Content-Type: application/json' \
  -d '{"external_ref":"BANK-20260301-0003"}' | python3 -m json.tool

# ---- 查询 ----
say "15) 查询单个指令（状态 + 当时余额/可用额）"
$C "$BASE/payment-instructions/PI-20260301-0001" | python3 -m json.tool

say "16) 权利人对账单：reserved_total / available / payment_instructions[] 可见，历史不可改写"
$C "$BASE/rightsholders/RS-A/ledger" | python3 -m json.tool

say "完成"
