#!/usr/bin/env bash
# 协议改版（新适用区间 + 完整权利人比例）端到端示例：先预览、后确认。
# 前置：docker compose up -d 已启动（或本地 uvicorn）。
# 用法: BASE=http://localhost:8000 bash examples/demo_revisions.sh
set -u
RUN="${RUN:-$(date +%s)}"
BASE="${BASE:-http://localhost:8000}"
C="curl -sS"
J="python3 -m json.tool"

say() { printf '\n=== %s ===\n' "$1"; }

say "健康检查"
$C "$BASE/health"; echo

say "1) 登记作品与第 1 版协议（A62.5 / B30 / C7.5，2026 全年）"
$C -X POST "$BASE/works" -H 'Content-Type: application/json' \
  -d "{\"code\":\"W-REV-$RUN\",\"title\":\"协议改版示例录音\"}" >/dev/null
$C -X POST "$BASE/agreements" -H 'Content-Type: application/json' -d "{
  \"work_code\":\"W-REV-$RUN\",
  \"valid_from\":\"2026-01-01T00:00:00+08:00\",
  \"valid_to\":\"2027-01-01T00:00:00+08:00\",
  \"shares\":[
    {\"holder_code\":\"RS-A\",\"percent\":\"62.5\"},
    {\"holder_code\":\"RS-B\",\"percent\":\"30\"},
    {\"holder_code\":\"RS-C\",\"percent\":\"7.5\"}
  ]
}" | $J
# 协议编号 id 记为 AID（响应里的 id；协议编号在历次改版中保持稳定）
AID="$($C "$BASE/agreements?work_code=W-REV-$RUN" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')"
echo "agreement id = $AID（稳定编号），当前版本 = 1"

say "2) 导入两笔播放：2 月 100 分（已出单期间）、3 月 200 分（未出单期间）"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d "{\"import_id\":\"REV-$RUN-P1\",\"work_code\":\"W-REV-$RUN\",
       \"occurred_at\":\"2026-02-10T20:00:00+08:00\",\"amount\":100}" >/dev/null
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d "{\"import_id\":\"REV-$RUN-P2\",\"work_code\":\"W-REV-$RUN\",
       \"occurred_at\":\"2026-03-10T20:00:00+08:00\",\"amount\":200}" >/dev/null

say "3) 先对 2026 年 2 月期间出具初次单（100 => 63/30/7）"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | $J

say "4) 预览改版：新适用区间自 2026-03-01 起（半开），新比例 A70/B20/C10"
# 2 月那笔落在新区间外 => 失去协议匹配、将转待处理；
# 3 月那笔仍在区间内 => 按新比例 140/40/20 重算；
# 已出单的 2 月期间预览应付差额 -63/-30/-7（确认后须再 issue 由追补单体现）。
PREVIEW="$($C -X POST "$BASE/agreements/revisions/preview" \
  -H 'Content-Type: application/json' -d "{
    \"agreement_id\": $AID,
    \"seen_version\": 1,
    \"valid_from\":\"2026-03-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"shares\":[
      {\"holder_code\":\"RS-A\",\"percent\":\"70\"},
      {\"holder_code\":\"RS-B\",\"percent\":\"20\"},
      {\"holder_code\":\"RS-C\",\"percent\":\"10\"}
    ]
  }")"
echo "$PREVIEW" | $J
TOKEN="$(echo "$PREVIEW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["preview_token"])')"

say "5) 预览只读不改账：协议仍是 v1、2 月仍只有一张单据"
$C "$BASE/agreements/$AID" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("version =",d["version"])'
$C "$BASE/statements?start=2026-02-01T00:00:00%2B08:00&end=2026-03-01T00:00:00%2B08:00" \
  | python3 -c 'import sys,json;print("statement batches =",len(json.load(sys.stdin)))'

say "6) 各种拒绝（均不修改数据）"
echo "-- 所见版本过期（seen_version=2 而现行是 1）=> 409 agreement_version_stale"
$C -o /dev/null -w '%{http_code}\n' -X POST "$BASE/agreements/revisions/preview" \
  -H 'Content-Type: application/json' -d "{
    \"agreement_id\": $AID, \"seen_version\": 2,
    \"valid_from\":\"2026-03-01T00:00:00+08:00\",
    \"shares\":[{\"holder_code\":\"RS-A\",\"percent\":\"100\"}]}"
echo "-- 比例合计不是 100% => 422"
$C -o /dev/null -w '%{http_code}\n' -X POST "$BASE/agreements/revisions/preview" \
  -H 'Content-Type: application/json' -d "{
    \"agreement_id\": $AID, \"seen_version\": 1,
    \"valid_from\":\"2026-03-01T00:00:00+08:00\",
    \"shares\":[{\"holder_code\":\"RS-A\",\"percent\":\"99\"}]}"
echo "-- 令牌被篡改 => 422 revision_token_invalid"
$C -o /dev/null -w '%{http_code}\n' -X POST "$BASE/agreements/revisions/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"agreement_id\": $AID, \"seen_version\": 1,
    \"valid_from\":\"2026-03-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"shares\":[
      {\"holder_code\":\"RS-A\",\"percent\":\"70\"},
      {\"holder_code\":\"RS-B\",\"percent\":\"20\"},
      {\"holder_code\":\"RS-C\",\"percent\":\"10\"}],
    \"preview_token\":\"${TOKEN}AA\"}"

say "7) 用原预览令牌确认改版 => 200，协议编号不变、版本升到 2"
$C -X POST "$BASE/agreements/revisions/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"agreement_id\": $AID,
    \"seen_version\": 1,
    \"valid_from\":\"2026-03-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"shares\":[
      {\"holder_code\":\"RS-A\",\"percent\":\"70\"},
      {\"holder_code\":\"RS-B\",\"percent\":\"20\"},
      {\"holder_code\":\"RS-C\",\"percent\":\"10\"}
    ],
    \"preview_token\":\"$TOKEN\"
  }" | $J

say "8) 旧令牌重复确认 => 409 agreement_version_stale（过期版本，拒绝且不改数据）"
$C -X POST "$BASE/agreements/revisions/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"agreement_id\": $AID, \"seen_version\": 1,
    \"valid_from\":\"2026-03-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"shares\":[
      {\"holder_code\":\"RS-A\",\"percent\":\"70\"},
      {\"holder_code\":\"RS-B\",\"percent\":\"20\"},
      {\"holder_code\":\"RS-C\",\"percent\":\"10\"}],
    \"preview_token\":\"$TOKEN\"}" | $J

say "9) 历史版本可追溯：v1 已 superseded、完整比例冻结；v2 current"
$C "$BASE/agreements/$AID/versions" | $J

say "10) 确认后按播放发生时刻用现行版本重算：2 月再出具 => 追补单 -63/-30/-7，P1 转待处理"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | $J

say "11) 3 月期间初次出具：P2 按 v2 比例 200 => 140/40/20"
$C -X POST "$BASE/statements/issue" -H 'Content-Type: application/json' \
  -d '{"start":"2026-03-01T00:00:00+08:00","end":"2026-04-01T00:00:00+08:00"}' \
  | $J

say "12) 预览后若播放更正链/协议/已出单期间变化，确认将 409 revision_state_changed"
echo "   （先 preview v2->v3，再导入一笔该作品新播放，再 confirm，即观察到拒绝）"
P2="$($C -X POST "$BASE/agreements/revisions/preview" \
  -H 'Content-Type: application/json' -d "{
    \"agreement_id\": $AID, \"seen_version\": 2,
    \"valid_from\":\"2026-01-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"shares\":[{\"holder_code\":\"RS-A\",\"percent\":\"80\"},
               {\"holder_code\":\"RS-B\",\"percent\":\"15\"},
               {\"holder_code\":\"RS-C\",\"percent\":\"5\"}]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["preview_token"])')"
$C -X POST "$BASE/plays/import" -H 'Content-Type: application/json' \
  -d "{\"import_id\":\"REV-$RUN-P3\",\"work_code\":\"W-REV-$RUN\",
       \"occurred_at\":\"2026-05-05T20:00:00+08:00\",\"amount\":33}" >/dev/null
$C -X POST "$BASE/agreements/revisions/confirm" \
  -H 'Content-Type: application/json' -d "{
    \"agreement_id\": $AID, \"seen_version\": 2,
    \"valid_from\":\"2026-01-01T00:00:00+08:00\",
    \"valid_to\":\"2027-01-01T00:00:00+08:00\",
    \"shares\":[{\"holder_code\":\"RS-A\",\"percent\":\"80\"},
               {\"holder_code\":\"RS-B\",\"percent\":\"15\"},
               {\"holder_code\":\"RS-C\",\"percent\":\"5\"}],
    \"preview_token\":\"$P2\"}" | $J
echo "（协议仍是 v2；重新 preview 后再 confirm 才会成功）"
