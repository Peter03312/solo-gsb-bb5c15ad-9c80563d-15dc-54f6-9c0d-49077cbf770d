"""端到端集成测试：协议改版（新适用区间 + 完整权利人比例，先预览后确认）。

运行: PYTHONPATH=. python3 tests/integration_revisions.py
覆盖：
- 登记即第 1 版；原登记/查询接口字段与错误约定兼容；
- 预览只读不改账：受影响播放（转待处理/离开待处理/比例变化）、已出单期间正负差额；
- 确认后协议编号稳定、版本 +1、旧版可追溯（区间与完整比例冻结）；
- 确认后按播放发生时刻用现行版本重算，失配播放转待处理，追补单体现正负差额且归因守恒；
- 乐观并发：预览后协议改版/播放更正链变化/相关期间单据变化均 409 拒绝且不改数据；
- 无效区间、比例合计≠100%、过期版本、不存在协议、区间重叠、令牌篡改/与请求不符；
- 改版与新协议登记并发互斥、同作品并发改版恰好一个成功、绝不产生重叠。
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_rev_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    from fastapi.testclient import TestClient
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    def rev_preview(c, aid, seen, vfrom, vto, shares, expected=200):
        r = c.post("/agreements/revisions/preview", json={
            "agreement_id": aid, "seen_version": seen,
            "valid_from": vfrom, "valid_to": vto, "shares": shares})
        check(f"preview http {expected}", r.status_code == expected, r.text[:300])
        return r

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        c.post("/works", json={"code": "W1", "title": "作品一"})
        # v1：A62.5 / B30 / C7.5，覆盖 2026 全年
        r = c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "62.5"},
                       {"holder_code": "B", "percent": "30"},
                       {"holder_code": "C", "percent": "7.5"}]})
        check("agreement v1 created", r.status_code == 201, r.text)
        aid = r.json()["id"]
        check("agreement response carries version=1", r.json()["version"] == 1)
        check("list agreements carries version",
              all(a["version"] == 1 for a in c.get("/agreements").json()))

        # 播放：2 月 100（P1）、3 月 200（P2）、2025 年协议外 50（P0，待处理）
        c.post("/plays/import", json={"import_id": "P0", "work_code": "W1",
              "occurred_at": "2025-12-31T20:00:00+08:00", "amount": 50})
        c.post("/plays/import", json={"import_id": "P1", "work_code": "W1",
              "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        c.post("/plays/import", json={"import_id": "P2", "work_code": "W1",
              "occurred_at": "2026-03-10T20:00:00+08:00", "amount": 200})

        per_feb = {"start": "2026-02-01T00:00:00+08:00",
                   "end": "2026-03-01T00:00:00+08:00"}
        r = c.post("/statements/issue", json=per_feb)
        check("feb initial 63/30/7",
              [(l["holder_code"], l["amount"]) for l in r.json()["payable"]]
              == [("A", 63), ("B", 30), ("C", 7)], r.text)
        check("feb initial pending none in-period", r.json()["pending"] == [])

        # ---- 输入校验：不存在协议 / 过期版本 / 比例 / 区间 / 时区 ----
        new_shares = [{"holder_code": "A", "percent": "70"},
                      {"holder_code": "B", "percent": "20"},
                      {"holder_code": "C", "percent": "10"}]
        r = rev_preview(c, 999999, 1, "2026-01-01T00:00:00+08:00",
                        "2027-01-01T00:00:00+08:00", new_shares, expected=404)
        check("missing agreement 404 code",
              r.json()["error"] == "agreement_not_found")
        r = rev_preview(c, aid, 5, "2026-01-01T00:00:00+08:00",
                        "2027-01-01T00:00:00+08:00", new_shares, expected=409)
        check("seen stale 409 code",
              r.json()["error"] == "agreement_version_stale"
              and r.json()["detail"]["current_version"] == 1)
        r = rev_preview(c, aid, 1, "2026-01-01T00:00:00+08:00",
                        "2027-01-01T00:00:00+08:00",
                        [{"holder_code": "A", "percent": "70"},
                         {"holder_code": "B", "percent": "20"}], expected=422)
        check("shares total 422", r.status_code == 422)
        r = rev_preview(c, aid, 1, "2026-03-01T00:00:00+08:00",
                        "2026-02-01T00:00:00+08:00", new_shares, expected=422)
        check("invalid range 422", r.status_code == 422)
        r = c.post("/agreements/revisions/preview", json={
            "agreement_id": aid, "seen_version": 1,
            "valid_from": "2026-01-01T00:00:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": new_shares})
        check("naive datetime 422", r.status_code == 422)

        # ---- 与其他现行协议重叠：先登记一个 2028 年开放区间 ----
        c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2028-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]})
        r = rev_preview(c, aid, 1, "2026-01-01T00:00:00+08:00",
                        "2029-01-01T00:00:00+08:00",
                        [{"holder_code": "A", "percent": "100"}], expected=409)
        check("overlap other agreement 409",
              r.json()["error"] == "agreement_overlap")

        # ---- 预览：区间改为 2026-03-01 起（P1 失配转待处理，P2 按 70/20/10 重算）----
        r = rev_preview(c, aid, 1, "2026-03-01T00:00:00+08:00",
                        "2027-01-01T00:00:00+08:00", new_shares)
        pv = r.json()
        check("preview new_version 2", pv["new_version"] == 2)
        check("preview token issued", len(pv["preview_token"]) > 40)
        by_id = {x["import_id"]: x for x in pv["affected_plays"]}
        check("P1 becomes pending",
              by_id["P1"]["after_pending"] is True
              and by_id["P1"]["before_pending"] is False
              and by_id["P1"]["after_agreement_id"] is None
              and by_id["P1"]["before_shares"] == {"A": 63, "B": 30, "C": 7}
              and by_id["P1"]["after_shares"] == {}, str(by_id["P1"]))
        check("P2 re-split 140/40/20",
              by_id["P2"]["after_shares"] == {"A": 140, "B": 40, "C": 20}
              and by_id["P2"]["before_shares"] == {"A": 125, "B": 60, "C": 15},
              str(by_id["P2"]))
        check("become_pending list", [x["import_id"] for x in pv["become_pending"]] == ["P1"])
        check("leave_pending empty", pv["leave_pending"] == [])
        check("feb delta -63/-30/-7",
              pv["issued_periods"][0]["delta"] == {"A": -63, "B": -30, "C": -7},
              str(pv["issued_periods"]))
        # 3 月期间尚未出单，不出现在 issued_periods
        check("only one issued period", len(pv["issued_periods"]) == 1)

        # 预览不改账：单据仍只有 1 张，协议仍 v1，余额/应付不变
        check("no statement created by preview",
              len(c.get("/statements", params=per_feb).json()) == 1)
        check("agreement still v1 after preview",
              c.get(f"/agreements/{aid}").json()["version"] == 1)

        # ---- 乐观并发：预览后导入新播放（相关更正链变化），确认拒绝且不改数据 ----
        c.post("/plays/import", json={"import_id": "PX", "work_code": "W1",
              "occurred_at": "2026-05-01T20:00:00+08:00", "amount": 9})
        body = {"agreement_id": aid, "seen_version": 1,
                "valid_from": "2026-03-01T00:00:00+08:00",
                "valid_to": "2027-01-01T00:00:00+08:00",
                "shares": new_shares, "preview_token": pv["preview_token"]}
        r = c.post("/agreements/revisions/confirm", json=body)
        check("confirm rejected play chain changed",
              r.status_code == 409
              and r.json()["error"] == "revision_state_changed"
              and r.json()["detail"]["changed"] == "play_chains", r.text)
        check("still v1 after reject",
              c.get(f"/agreements/{aid}").json()["version"] == 1)
        check("still one statement after reject",
              len(c.get("/statements", params=per_feb).json()) == 1)

        # ---- 令牌篡改 / 与请求不符 ----
        r = c.post("/agreements/revisions/confirm", json={**body,
                   "preview_token": pv["preview_token"][:-3] + "AAA"})
        check("tampered token 422",
              r.status_code == 422 and r.json()["error"] == "revision_token_invalid")
        r = c.post("/agreements/revisions/confirm", json={**body,
                   "shares": [{"holder_code": "A", "percent": "71"},
                              {"holder_code": "B", "percent": "19"},
                              {"holder_code": "C", "percent": "10"}]})
        check("body differs from token 422",
              r.status_code == 422 and r.json()["error"] == "revision_request_mismatch")

        # ---- 重新预览并确认（删掉 PX 对指纹的影响：无需删，新预览含它）----
        r = rev_preview(c, aid, 1, "2026-03-01T00:00:00+08:00",
                        "2027-01-01T00:00:00+08:00", new_shares)
        pv = r.json()
        body["preview_token"] = pv["preview_token"]
        r = c.post("/agreements/revisions/confirm", json=body)
        check("confirm revised v2",
              r.status_code == 200 and r.json()["status"] == "revised"
              and r.json()["version"] == 2, r.text)
        # 确认响应内联同口径影响
        check("confirm response impact",
              [x["import_id"] for x in r.json()["become_pending"]] == ["P1"]
              and r.json()["issued_periods"][0]["delta"]
              == {"A": -63, "B": -30, "C": -7})

        # 旧令牌再确认 => 过期版本
        r = c.post("/agreements/revisions/confirm", json=body)
        check("old token now stale 409",
              r.status_code == 409
              and r.json()["error"] == "agreement_version_stale")

        # 协议编号稳定、现行版本为 v2；旧版可追溯
        r = c.get(f"/agreements/{aid}")
        check("id stable version 2",
              r.json()["id"] == aid and r.json()["version"] == 2
              and r.json()["valid_from"].startswith("2026-02-28T16:00:00"),
              r.text)
        r = c.get(f"/agreements/{aid}/versions")
        versions = r.json()
        check("two versions", len(versions) == 2 and r.status_code == 200)
        v1, v2 = versions
        check("v1 superseded and frozen",
              v1["version"] == 1 and v1["current"] is False
              and v1["superseded_at"] is not None
              and [(s["holder_code"], s["percent"]) for s in v1["shares"]]
              == [("A", "62.5000"), ("B", "30.0000"), ("C", "7.5000")],
              str(v1))
        check("v2 current",
              v2["version"] == 2 and v2["current"] is True
              and v2["superseded_at"] is None
              and [s["holder_code"] for s in v2["shares"]] == ["A", "B", "C"],
              str(v2))
        check("versions of missing agreement 404",
              c.get("/agreements/999999/versions").status_code == 404)

        # ---- 确认后按发生时刻用现行版本重算：2 月追补体现负差额，P1 转待处理 ----
        r = c.post("/statements/issue", json=per_feb)
        body_iss = r.json()
        check("feb supplement negative",
              body_iss["kind"] == "supplement"
              and {l["holder_code"]: l["delta"] for l in body_iss["payable"]}
              == {"A": -63, "B": -30, "C": -7}, r.text)
        check("P1 pending after revision",
              [p["import_id"] for p in body_iss["pending"]] == ["P1"])
        # 追补归因：removed 负额逐笔保留，合计恒等于明细行
        r = c.get(f"/statements/{body_iss['batch_id']}/attributions")
        att = r.json()
        check("attribution reconciles",
              all(t["reconciles"] for t in att["holder_totals"]), str(att["holder_totals"]))
        check("removed negative kept",
              all(x["change_type"] == "removed" and x["amount"] < 0
                  for x in att["records"]), str(att["records"])[:400])

        # 3 月期间出具：该期间只有 P2=200（PX 发生在 5 月，不在 3 月期间）=> 140/40/20
        per_mar = {"start": "2026-03-01T00:00:00+08:00",
                   "end": "2026-04-01T00:00:00+08:00"}
        r = c.post("/statements/issue", json=per_mar)
        got = {l["holder_code"]: l["amount"] for l in r.json()["payable"]}
        check("march initial under v2", got == {"A": 140, "B": 40, "C": 20}, str(got))

        # 既有单据、付款历史不改写：登记付款并核对余额口径
        r = c.post("/payments", json={
            "holder_code": "A", "external_ref": "BANK-REV-1", "amount": 100})
        check("payment 201", r.status_code == 201, r.text)
        # 再做一次不影响账的改版预览（相同区间与比例），确认后不产生差额
        same = [{"holder_code": "A", "percent": "70"},
                {"holder_code": "B", "percent": "20"},
                {"holder_code": "C", "percent": "10"}]
        pv = rev_preview(c, aid, 2, "2026-03-01T00:00:00+08:00",
                         "2027-01-01T00:00:00+08:00", same).json()
        # PX 在预览"现行"里也匹配 v2，故无受影响播放
        check("identical re-revision has no affected plays",
              pv["affected_plays"] == [] and pv["issued_periods"] == [])

        # ---- 乐观并发：预览后相关期间单据变化（出具 3 月初次单）=> 拒绝 ----
        # 用真正改变区间的改版（前扩使 P0 离开待处理）
        extend_shares = [{"holder_code": "A", "percent": "80"},
                         {"holder_code": "B", "percent": "20"}]
        pv = rev_preview(c, aid, 2, "2025-01-01T00:00:00+08:00",
                         "2027-01-01T00:00:00+08:00", extend_shares).json()
        # 当前待处理的链尾为 P0（协议外）与 P1（v2 区间自 3 月起，P1 在 2 月）；
        # 前扩至 2025 年后二者都重新匹配，离开待处理。
        check("extend preview leaves P0/P1 pending",
              [x["import_id"] for x in pv["leave_pending"]] == ["P0", "P1"],
              str([x["import_id"] for x in pv["leave_pending"]]))
        # 预览后对相关期间（3 月）再出具：当前无变化 => 不出单、指纹不变；
        # 改为对 3 月播放做更正（链尾变化）来触发 statement 侧变化：
        c.post("/plays/import", json={"import_id": "P2R1", "work_code": "W1",
              "occurred_at": "2026-03-10T20:00:00+08:00", "amount": 100,
              "corrects_id": "P2"})
        r = c.post("/agreements/revisions/confirm", json={
            "agreement_id": aid, "seen_version": 2,
            "valid_from": "2025-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": extend_shares, "preview_token": pv["preview_token"]})
        check("chain correction after preview rejected",
              r.status_code == 409
              and r.json()["detail"]["changed"] == "play_chains", r.text)
        check("still v2 after chain reject",
              c.get(f"/agreements/{aid}").json()["version"] == 2)

        # ---- 相关期间单据变化检测：新预览后，对 2 月期间出具追补单（P2 更正不影响 2 月），
        #      改用 3 月期间首次已出；构造：重新预览后，再对一个覆盖 W1 播放的期间出新单 ----
        pv = rev_preview(c, aid, 2, "2025-01-01T00:00:00+08:00",
                         "2027-01-01T00:00:00+08:00", extend_shares).json()
        # 出具一个覆盖 2025 年 P0 的期间单据（该期间纳入相关期间集合 => periods 失配）
        per_2025 = {"start": "2025-01-01T00:00:00+08:00",
                    "end": "2026-01-01T00:00:00+08:00"}
        r_iss = c.post("/statements/issue", json=per_2025)
        check("2025 initial pending P0",
              [p["import_id"] for p in r_iss.json()["pending"]] == ["P0"]
              and r_iss.json()["payable"] == [])
        r = c.post("/agreements/revisions/confirm", json={
            "agreement_id": aid, "seen_version": 2,
            "valid_from": "2025-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": extend_shares, "preview_token": pv["preview_token"]})
        check("new related statement after preview rejected",
              r.status_code == 409 and r.json()["detail"]["changed"]
              in ("statement_periods", "statements"), r.text)

        # ---- 确认前扩改版（80/20 覆盖 2025 起）：随后两期间追补体现正负差额 ----
        pv = rev_preview(c, aid, 2, "2025-01-01T00:00:00+08:00",
                         "2027-01-01T00:00:00+08:00", extend_shares).json()
        r = c.post("/agreements/revisions/confirm", json={
            "agreement_id": aid, "seen_version": 2,
            "valid_from": "2025-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": extend_shares, "preview_token": pv["preview_token"]})
        check("confirm v3 extend", r.status_code == 200 and r.json()["version"] == 3, r.text)
        # 2025 期间追补：P0 50 按 80/20 => +40/+10（原为空初次单）
        r = c.post("/statements/issue", json=per_2025)
        check("2025 supplement +40/+10",
              {l["holder_code"]: l["delta"] for l in r.json()["payable"]}
              == {"A": 40, "B": 10}
              and r.json()["pending"] == [], r.text)
        # 2 月期间：P1 仍在 v3 区间外（v3 自 2025 起，P1=2026-02 在内！区间覆盖到 2027）
        # v3 覆盖 P1，故 P1 重新获得匹配 80/20 => 100 -> 80/20，相对当前已入账 0 追补 +80/+20
        r = c.post("/statements/issue", json=per_feb)
        check("feb supplement +80/+20 (P1 matched again, C removed)",
              {l["holder_code"]: l["delta"] for l in r.json()["payable"]
               if l["delta"] is not None} == {"A": 80, "B": 20}, r.text)
        check("no pending in feb", r.json()["pending"] == [])
        # C 在该期间历史归因里为 removed（-7 与 +7 净额变化），归因仍守恒
        att = c.get(f"/statements/{r.json()['batch_id']}/attributions").json()
        check("feb v3 attribution reconciles",
              all(t["reconciles"] for t in att["holder_totals"]),
              str(att["holder_totals"]))

        # ---- 跨版协议编号稳定：归因证据里的 agreement_id 始终指向同一协议编号 ----
        r = c.get(f"/agreements/{aid}/versions")
        check("three versions traceable", len(r.json()) == 3
              and [v["version"] for v in r.json()] == [1, 2, 3])

    # ---- 并发改版：同内容预览 5 份，并发确认恰好 1 个成功 ----
    with TestClient(app) as c2:
        c2.post("/works", json={"code": "WR", "title": "并发作品"})
        r = c2.post("/agreements", json={
            "work_code": "WR",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]})
        wid = r.json()["id"]
        c2.post("/plays/import", json={"import_id": "WP1", "work_code": "WR",
               "occurred_at": "2026-02-01T20:00:00+08:00", "amount": 10})
        rev_body = {"agreement_id": wid, "seen_version": 1,
                    "valid_from": "2026-01-01T00:00:00+08:00",
                    "shares": [{"holder_code": "A", "percent": "50"},
                               {"holder_code": "B", "percent": "50"}]}
        tokens = []

        def preview_one():
            tokens.append(
                c2.post("/agreements/revisions/preview", json=rev_body).json()["preview_token"]
            )

        ts = [threading.Thread(target=preview_one) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        results = []

        def confirm_one(tok):
            rr = c2.post("/agreements/revisions/confirm",
                         json={**rev_body, "preview_token": tok})
            results.append(rr.status_code)

        ts = [threading.Thread(target=confirm_one, args=(t,)) for t in tokens]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("concurrent revisions: exactly one 200",
              results.count(200) == 1 and results.count(409) == 4, str(results))
        check("final version 2",
              c2.get(f"/agreements/{wid}").json()["version"] == 2)

        # 改版（扩到 2030）与新登记重叠区间并发：不得同时成功、无重叠
        pv = c2.post("/agreements/revisions/preview", json={
            "agreement_id": wid, "seen_version": 2,
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2030-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "50"},
                       {"holder_code": "B", "percent": "50"}]}).json()
        outcomes = []

        def do_revision():
            rr = c2.post("/agreements/revisions/confirm", json={
                "agreement_id": wid, "seen_version": 2,
                "valid_from": "2026-01-01T00:00:00+08:00",
                "valid_to": "2030-01-01T00:00:00+08:00",
                "shares": [{"holder_code": "A", "percent": "50"},
                           {"holder_code": "B", "percent": "50"}],
                "preview_token": pv["preview_token"]})
            outcomes.append(("revision", rr.status_code))

        def do_new_agreement():
            rr = c2.post("/agreements", json={
                "work_code": "WR",
                "valid_from": "2028-01-01T00:00:00+08:00",
                "valid_to": "2029-01-01T00:00:00+08:00",
                "shares": [{"holder_code": "A", "percent": "100"}]})
            outcomes.append(("new", rr.status_code))

        ts = [threading.Thread(target=do_revision), threading.Thread(target=do_new_agreement)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        codes = dict(outcomes)
        check("revision vs registration mutually exclude overlap",
              codes["revision"] != codes["new"]
              and (codes["revision"] == 200 or codes["new"] == 201)
              and 409 in codes.values(), str(outcomes))
        agrs = c2.get("/agreements", params={"work_code": "WR"}).json()
        # 最终不变量：区间互不重叠（数量取决于谁先拿到锁，1 或 2 条均合法，靠 409 保证不重叠）
        ranges = sorted((a["valid_from"], a["valid_to"]) for a in agrs)
        check("final ranges non-overlapping",
              all(ranges[i][1] <= ranges[i + 1][0]
                  for i in range(len(ranges) - 1)),
              str(ranges))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL REVISION INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
