#!/usr/bin/env bash
# 协议修订（amendment）端到端示例：预览 -> 确认 -> 已出单期间追补。
# 前置：docker compose up -d 已启动（或本地 uvicorn）。
# 用法: BASE=http://localhost:8000 bash examples/demo_amendments.sh
set -u
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

say "1) 登记作品与权利人"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d '{"code":"W-AMD-001","title":"协议修订演示录音"}' >/dev/null
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RA","name":"甲"}' >/dev/null
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RB","name":"乙"}' >/dev/null
$C -X POST "$BASE/rightsholders" -H 'Content-Type: application/json' -d '{"code":"RC","name":"丙"}' >/dev/null
echo done

say "2) 登记 v1 协议：2026 全年，RA/RB/RC = 62.5/30/7.5"
AID=$($C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d '{
  "work_code":"W-AMD-001",
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00",
  "shares":[
    {"holder_code":"RA","percent":"62.5"},
    {"holder_code":"RB","percent":"30"},
    {"holder_code":"RC","percent":"7.5"}
  ]}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "agreement id = $AID"

say "3) 导入两条播放：2 月 100 分（区间内）、2025 年 40 分（无协议->待处理）"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"AMD-P1","work_code":"W-AMD-001","occurred_at":"2026-02-10T20:00:00+08:00","amount":100}' >/dev/null
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d '{"import_id":"AMD-P0","work_code":"W-AMD-001","occurred_at":"2025-12-31T20:00:00+08:00","amount":40}' >/dev/null
echo done

say "4) 出具 2026 年 2 月初次单（100 => 63/30/7）"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool

say "5) 预览修订：区间收窄到 [2026-05-01, 2027-01-01)、RA/RB = 50/50（不改账）"
PREVIEW=$($C -X POST "$BASE/agreements/$AID/amendments/preview" \
  -H 'Content-Type: application/json' -d '{
    "valid_from":"2026-05-01T00:00:00+08:00",
    "valid_to":"2027-01-01T00:00:00+08:00",
    "note":"2026 年 5 月起改为两方各半",
    "shares":[
      {"holder_code":"RA","percent":"50"},
      {"holder_code":"RB","percent":"50"}
    ]}')
echo "$PREVIEW" | python3 -m json.tool
TOKEN=$(echo "$PREVIEW" | python3 -c 'import sys,json; print(json.load(sys.stdin)["preview_token"])')

say "6) 预览后协议仍是 v1（预览不改任何数据）"
$C "$BASE/agreements/$AID" | python3 -m json.tool

say "7) 用篡改过的比例确认 => 409 amendment_preview_invalid"
$C -i -X POST "$BASE/agreements/$AID/amendments/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"valid_from\":\"2026-05-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"shares\":[{\"holder_code\":\"RA\",\"percent\":\"90\"},{\"holder_code\":\"RB\",\"percent\":\"10\"}],
    \"preview_token\":\"$TOKEN\"}" | head -1

say "8) 用预览原内容确认 => 201 追加 v2（协议编号 $AID 不变）"
$C -X POST "$BASE/agreements/$AID/amendments/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"valid_from\":\"2026-05-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"note\":\"2026 年 5 月起改为两方各半\",
    \"shares\":[
      {\"holder_code\":\"RA\",\"percent\":\"50\"},
      {\"holder_code\":\"RB\",\"percent\":\"50\"}
    ],
    \"preview_token\":\"$TOKEN\"}" | python3 -m json.tool

say "9) 旧版可追溯：版本列表 + v1 详情"
$C "$BASE/agreements/$AID/versions" | python3 -m json.tool
$C "$BASE/agreements/$AID/versions/1" | python3 -m json.tool

say "10) 令牌重放（版本已过期）=> 409 agreement_version_conflict"
$C -i -X POST "$BASE/agreements/$AID/amendments/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"valid_from\":\"2026-05-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"shares\":[
      {\"holder_code\":\"RA\",\"percent\":\"50\"},
      {\"holder_code\":\"RB\",\"percent\":\"50\"}
    ],
    \"preview_token\":\"$TOKEN\"}" | head -1

say "11) 重新出具 2 月单据：失去匹配的播放转待处理，负额追补 -63/-30/-7"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool

say "12) 试算：2 月待处理清单含 AMD-P1，当前应付投影为 0"
$C "$BASE/statements/preview?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" \
  | python3 -m json.tool

echo
echo "完成：协议 v2 已生效；历史初次单与归因未改写，差额经追补单体现。"
