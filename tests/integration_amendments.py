"""协议修订（amendment）端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

运行: PYTHONPATH=. python3 tests/integration_amendments.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_amd_pg_")) / "pgdata"
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

    def body(r):
        return r.json()

    with TestClient(app) as c:
        c.post("/works", json={"code": "W1", "title": "作品一"})
        c.post("/works", json={"code": "W2", "title": "作品二"})
        # 协议 1：2026 全年，A/B/C = 62.5/30/7.5
        r = c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "62.5"},
                       {"holder_code": "B", "percent": "30"},
                       {"holder_code": "C", "percent": "7.5"}]})
        check("agreement create", r.status_code == 201, r.text)
        aid = r.json()["id"]
        check("agreement v1 field", r.json()["version"] == 1, r.text)

        feb = {"start": "2026-02-01T00:00:00+08:00",
               "end": "2026-03-01T00:00:00+08:00"}
        jun = {"start": "2026-06-01T00:00:00+08:00",
               "end": "2027-01-01T00:00:00+08:00"}

        # 2 月播放 P1（100 分，区间内）、P0（2025 年，无协议 -> 待处理）
        r = c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        check("P1", r.status_code == 201)
        r = c.post("/plays/import", json={
            "import_id": "P0", "work_code": "W1",
            "occurred_at": "2025-12-31T20:00:00+08:00", "amount": 40})
        check("P0", r.status_code == 201)

        # ---- 出具 2 月初次单：100 => 63/30/7；P0 不在期间 ----
        r = c.post("/statements/issue", json=feb)
        b1 = r.json()
        got = {x["holder_code"]: x["amount"] for x in b1["payable"]}
        check("initial 63/30/7", got == {"A": 63, "B": 30, "C": 7}, str(got))

        # 修订载荷：区间收窄到 [2026-05-01, 2027-01-01)；A/B 50/50
        # （与下方 2027 年协议 a2 仅半开相接，不重叠）
        amend = {
            "valid_from": "2026-05-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "50"},
                       {"holder_code": "B", "percent": "50"}],
            "note": "v2",
        }

        # ---- 1) 预览：P1 失去匹配转待处理；2 月差额 -63/-30/-7；不改账 ----
        r = c.post(f"/agreements/{aid}/amendments/preview", json=amend)
        check("preview 200", r.status_code == 200, r.text)
        pv = r.json()
        check("preview version labels",
              pv["current_version"] == 1 and pv["proposed_version"] == 2
              and pv["agreement"]["proposed_version"] == 2)
        check("preview affected P1",
              [p["import_id"] for p in pv["affected_plays"]] == ["P1"]
              and pv["affected_plays"][0]["change_type"] == "become_pending",
              str(pv["affected_plays"]))
        check("preview old alloc",
              pv["affected_plays"][0]["old"]["allocation"] == {"A": 63, "B": 30, "C": 7}
              and pv["affected_plays"][0]["old"]["version"] == 1
              and pv["affected_plays"][0]["new"]["agreement_id"] is None)
        check("preview to_pending",
              [p["import_id"] for p in pv["pending_changes"]["to_pending"]] == ["P1"]
              and pv["pending_changes"]["from_pending"] == [])
        check("preview one period", len(pv["affected_periods"]) == 1, str(pv["affected_periods"]))
        pe = pv["affected_periods"][0]
        d = {x["holder_code"]: x["amount"] for x in pe["payable_delta_work"]}
        check("preview feb work delta", d == {"A": -63, "B": -30, "C": -7}, str(d))
        d2 = {x["holder_code"]: x["amount"] for x in pe["projected_supplement"]}
        check("preview feb projected", d2 == {"A": -63, "B": -30, "C": -7}, str(d2))
        check("preview pending_after P1",
              [p["import_id"] for p in pe["pending_after"]] == ["P1"],
              str(pe["pending_after"]))
        token = pv["preview_token"]
        check("token issued", "." in token and len(token) > 40)

        # 预览不改账：协议仍为 v1、单据无新增、再试算 delta=0
        r = c.get(f"/agreements/{aid}")
        check("preview no write version", r.json()["version"] == 1)
        r = c.get("/statements", params=feb)
        check("preview no new batch", len(r.json()) == 1)
        r = c.get("/statements/preview", params=feb)
        zeros = {x["holder_code"]: x["delta"] for x in r.json()["payable"]}
        check("preview no ledger change", zeros == {"A": 0, "B": 0, "C": 0}, str(zeros))

        # ---- 2) 静态校验：无效区间 / 比例合计 / 重叠 / 未知权利人 / 不存在协议 ----
        bad_range = dict(amend, valid_from="2026-05-01T00:00:00+08:00",
                         valid_to="2026-05-01T00:00:00+08:00")
        r = c.post(f"/agreements/{aid}/amendments/preview", json=bad_range)
        check("invalid range 422", r.status_code == 422, r.text)
        no_tz = dict(amend, valid_from="2026-05-01T00:00:00")
        r = c.post(f"/agreements/{aid}/amendments/preview", json=no_tz)
        check("naive time 422", r.status_code == 422)
        bad_total = dict(amend, shares=[{"holder_code": "A", "percent": "50"},
                                        {"holder_code": "B", "percent": "40"}])
        r = c.post(f"/agreements/{aid}/amendments/preview", json=bad_total)
        check("bad total 422", r.status_code == 422)
        dup_holder = dict(amend, shares=[{"holder_code": "A", "percent": "50"},
                                         {"holder_code": "A", "percent": "50"}])
        r = c.post(f"/agreements/{aid}/amendments/preview", json=dup_holder)
        check("dup holder 422", r.status_code == 422)
        unknown = dict(amend, shares=[{"holder_code": "A", "percent": "50"},
                                      {"holder_code": "ZZ", "percent": "50"}])
        r = c.post(f"/agreements/{aid}/amendments/preview", json=unknown)
        check("unknown holder 422",
              r.status_code == 422 and r.json()["error"] == "holder_not_found", r.text)
        # 与其他现行协议重叠：先在同作品建一个 2027 全年的有限区间协议，
        # 再尝试把本协议修订为 [2026-12-01, 2027-03-01) —— 与之重叠；
        # 而正式修订（2026-05-01 开放上界）与 2027 协议仅相接，不重叠。
        r = c.post("/agreements", json={
            "work_code": "W1",
            "valid_from": "2027-01-01T00:00:00+08:00",
            "valid_to": "2028-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "C", "percent": "100"}]})
        a2 = r.json()["id"]
        overlap_amend = {
            "valid_from": "2026-12-01T00:00:00+08:00",
            "valid_to": "2027-03-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}],
        }
        r = c.post(f"/agreements/{aid}/amendments/preview", json=overlap_amend)
        check("overlap preview 409",
              r.status_code == 409 and r.json()["error"] == "agreement_overlap"
              and r.json()["detail"]["other_agreement_id"] == a2, r.text)
        r = c.post(f"/agreements/999999/amendments/preview", json=amend)
        check("missing agreement 404",
              r.status_code == 404 and r.json()["error"] == "agreement_not_found")

        # ---- 3) 确认：内容与令牌不符 / 坏令牌 ----
        changed = dict(amend, note="tampered")
        r = c.post(f"/agreements/{aid}/amendments/confirm",
                   json=dict(changed, preview_token=token))
        check("confirm content mismatch 409",
              r.status_code == 409
              and r.json()["error"] == "amendment_preview_invalid", r.text)
        r = c.post(f"/agreements/{aid}/amendments/confirm",
                   json=dict(amend, preview_token="garbage.token"))
        check("bad token 409",
              r.status_code == 409
              and r.json()["error"] == "amendment_preview_invalid")
        r = c.post(f"/agreements/{aid}/amendments/confirm",
                   json=dict(amend, preview_token=""))
        check("empty token 422", r.status_code == 422)

        # 令牌不能跨协议使用
        r = c.post(f"/agreements/{a2}/amendments/confirm",
                   json=dict(amend, preview_token=token))
        check("token cross agreement 409",
              r.status_code == 409
              and r.json()["error"] == "amendment_preview_invalid")

        # ---- 4) 预览后播放更正链变化 => 确认 stale ----
        r = c.post("/plays/import", json={
            "import_id": "P1R1", "work_code": "W1",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100,
            "corrects_id": "P1"})
        check("P1 correction", r.status_code == 201)
        r = c.post(f"/agreements/{aid}/amendments/confirm",
                   json=dict(amend, preview_token=token))
        check("stale after correction 409",
              r.status_code == 409 and r.json()["error"] == "amendment_preview_stale",
              r.text)
        # 拒绝后数据未变（仍 v1；更正后的 P1 仍由 v1 匹配）
        r = c.get(f"/agreements/{aid}")
        check("rejected keeps v1", r.json()["version"] == 1)

        # 重新预览后确认成功
        r = c.post(f"/agreements/{aid}/amendments/preview", json=amend)
        token = r.json()["preview_token"]
        pv = r.json()
        check("repreview affected chain tail",
              pv["affected_plays"][0]["import_id"] == "P1R1"
              and pv["affected_plays"][0]["root_import_id"] == "P1",
              str(pv["affected_plays"]))
        r = c.post(f"/agreements/{aid}/amendments/confirm",
                   json=dict(amend, preview_token=token))
        check("confirm 201", r.status_code == 201, r.text)
        cf = r.json()
        check("confirm status", cf["status"] == "amended")
        check("confirm version bump",
              cf["agreement"]["previous_version"] == 1
              and cf["agreement"]["current_version"] == 2
              and cf["current_version"] == 2 and cf["proposed_version"] == 2)

        # ---- 5) 编号稳定、旧版可追溯、镜像现行 ----
        r = c.get(f"/agreements/{aid}")
        got_a = r.json()
        check("stable id + v2 current",
              got_a["id"] == aid and got_a["version"] == 2
              and [(s["holder_code"], s["percent"]) for s in got_a["shares"]]
              == [("A", "50.0000"), ("B", "50.0000")], str(got_a))
        r = c.get(f"/agreements/{aid}/versions")
        vl = r.json()
        check("versions list", vl["current_version"] == 2
              and len(vl["versions"]) == 2
              and vl["versions"][0]["version"] == 1
              and vl["versions"][0]["is_current"] is False
              and vl["versions"][0]["superseded_at"] is not None
              and vl["versions"][1]["is_current"] is True
              and vl["versions"][1]["superseded_at"] is None, str(vl))
        r = c.get(f"/agreements/{aid}/versions/1")
        v1 = r.json()
        check("v1 traceable",
              [(s["holder_code"], s["percent"]) for s in v1["shares"]]
              == [("A", "62.5000"), ("B", "30.0000"), ("C", "7.5000")]
              and v1["valid_to"].startswith("2026-12-31T16:00:00"), str(v1))
        r = c.get(f"/agreements/{aid}/versions/9")
        check("missing version 404",
              r.status_code == 404
              and r.json()["error"] == "agreement_version_not_found")

        # ---- 6) 现行匹配：2 月 P1R1 失去协议转待处理；6 月播放按 v2 50/50 ----
        r = c.post("/plays/import", json={
            "import_id": "P2", "work_code": "W1",
            "occurred_at": "2026-06-10T20:00:00+08:00", "amount": 100})
        check("P2", r.status_code == 201)
        r = c.get("/statements/preview", params=feb)
        j = r.json()
        check("feb P1R1 pending now",
              len(j["pending"]) == 1 and j["pending"][0]["import_id"] == "P1R1",
              str(j["pending"]))
        # 当前应付投影为 0（零额行保留、delta 为相对已入账 63/30/7 的负数）
        check("feb current payable 0",
              {x["holder_code"]: x["amount"] for x in j["payable"]}
              == {"A": 0, "B": 0, "C": 0}, str(j["payable"]))

        # ---- 7) 已出单期间经原追补机制体现负差额；历史单据/归因不改写 ----
        # 先冻结 b1 证据基线（b1 是 P1 更正在账时的历史初次单）
        attr_before = c.get(f"/statements/{b1['batch_id']}/attributions").json()
        r = c.post("/statements/issue", json=feb)
        b2 = r.json()
        check("feb supplement kind", b2["kind"] == "supplement", r.text)
        sup = {x["holder_code"]: (x["amount"], x["delta"]) for x in b2["payable"]}
        check("feb supplement -63/-30/-7",
              sup == {"A": (0, -63), "B": (0, -30), "C": (0, -7)}, str(sup))
        check("feb supplement pending P1R1",
              [p["import_id"] for p in b2["pending"]] == ["P1R1"])
        # 初次单归因冻结不变：协议修订与追补出具均不改写历史单据证据
        attr_after = c.get(f"/statements/{b1['batch_id']}/attributions").json()
        check("old evidence unchanged",
              attr_after["records"] == attr_before["records"]
              and attr_after["total_records"] == attr_before["total_records"])
        # 追补单逐笔撤销 + 待处理播放不产生归因
        attr2 = c.get(f"/statements/{b2['batch_id']}/attributions").json()
        check("supplement removed evidence",
              {(x["root_import_id"], x["holder_code"], x["change_type"]): x["amount"]
               for x in attr2["records"]}
              == {("P1", "A", "removed"): -63,
                  ("P1", "B", "removed"): -30,
                  ("P1", "C", "removed"): -7},
              str(attr2["records"]))

        # ---- 8) 第二版修订（v2 -> v3）：比例变化的重分摊 + 区间延伸待处理返回 ----
        # 出具 6 月~年底初次单：P2 100 按 v2 => 50/50
        r = c.post("/statements/issue", json=jun)
        b3 = r.json()
        got = {x["holder_code"]: x["amount"] for x in b3["payable"]}
        check("jun initial 50/50", got == {"A": 50, "B": 50}, str(got))

        # v3：区间从 2025-06-01 到 2027-01-01（延伸覆盖 P0，使其离开待处理，
        # 上界与 a2 的 2027 年协议相接不重叠）、70/30
        amend3 = {
            "valid_from": "2025-06-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "70"},
                       {"holder_code": "B", "percent": "30"}],
        }
        r = c.post(f"/agreements/{aid}/amendments/preview", json=amend3)
        check("preview v3 200", r.status_code == 200, r.text)
        pv3 = r.json()
        by_id = {p["import_id"]: p for p in pv3["affected_plays"]}
        check("v3 reallocated P2",
              by_id["P2"]["change_type"] == "reallocated"
              and by_id["P2"]["old"]["version"] == 2
              and by_id["P2"]["new"]["version"] == 3
              and by_id["P2"]["old"]["allocation"] == {"A": 50, "B": 50}
              and by_id["P2"]["new"]["allocation"] == {"A": 70, "B": 30},
              str(by_id["P2"]))
        check("v3 P0 becomes allocated",
              by_id["P0"]["change_type"] == "become_allocated"
              and by_id["P0"]["old"]["agreement_id"] is None
              and by_id["P0"]["new"]["agreement_id"] == aid
              and by_id["P0"]["new"]["allocation"] == {"A": 28, "B": 12},
              str(by_id["P0"]))
        # 2 月链尾 P1R1 在 v2 下待处理，v3 区间延伸后也重新获得匹配
        check("v3 P1R1 becomes allocated",
              by_id["P1R1"]["change_type"] == "become_allocated"
              and by_id["P1R1"]["new"]["allocation"] == {"A": 70, "B": 30},
              str(by_id["P1R1"]))
        check("v3 from_pending",
              sorted(p["import_id"] for p in pv3["pending_changes"]["from_pending"])
              == ["P0", "P1R1"],
              str(pv3["pending_changes"]["from_pending"]))
        # 两个已出单期间受影响：2 月（P1R1 回归）、6 月（P2 重分摊）
        # 期间端点以 UTC 返回：2 月期末为 2026-02-28T16:00Z，6 月期末为 2026-12-31T16:00Z
        def pe_by_month_end(month_end_utc: str):
            return next(p for p in pv3["affected_periods"]
                        if p["period_end"].startswith(month_end_utc))
        feb_pe = pe_by_month_end("2026-02-28")
        jun_pe = pe_by_month_end("2026-12-31")
        d = {x["holder_code"]: x["amount"]
             for x in feb_pe["payable_delta_work"]}
        check("v3 feb delta 70/30 (P1R1 back)", d == {"A": 70, "B": 30}, str(d))
        check("v3 feb pending cleared", feb_pe["pending_after"] == [])
        d = {x["holder_code"]: x["amount"]
             for x in jun_pe["payable_delta_work"]}
        check("v3 jun delta", d == {"A": 20, "B": -20}, str(d))

        # 令牌重放：同一令牌确认两次，第二次为过期版本
        r = c.post(f"/agreements/{aid}/amendments/confirm",
                   json=dict(amend3, preview_token=pv3["preview_token"]))
        check("confirm v3 201", r.status_code == 201, r.text)
        r = c.post(f"/agreements/{aid}/amendments/confirm",
                   json=dict(amend3, preview_token=pv3["preview_token"]))
        check("replay expired version 409",
              r.status_code == 409
              and r.json()["error"] == "agreement_version_conflict"
              and r.json()["detail"]["previewed_version"] == 2
              and r.json()["detail"]["current_version"] == 3, r.text)

        # ---- 9) 再出具：2 月（P1R1 回归 70/30）与 6 月（+20/-20）各出追补 ----
        r = c.post("/statements/issue", json=jun)
        b4 = r.json()
        sup = {x["holder_code"]: (x["amount"], x["delta"]) for x in b4["payable"]}
        check("jun supplement 70/30 +20/-20",
              sup == {"A": (70, 20), "B": (30, -20)}, str(sup))
        r = c.post("/statements/issue", json=feb)
        bf = r.json()
        sup = {x["holder_code"]: (x["amount"], x["delta"]) for x in bf["payable"]}
        check("feb supplement P1R1 back 70/30",
              bf["kind"] == "supplement"
              and sup == {"A": (70, 70), "B": (30, 30), "C": (0, None)}, str(sup))
        check("feb pending empty after v3", bf["pending"] == [])
        # 再出具无变化：不产生新单据
        r = c.post("/statements/issue", json=feb)
        check("feb no dup when unchanged", r.json()["batch_id"] == bf["batch_id"])

        # ---- 10) 预览后相关期间单据变化 => stale ----
        amend4 = {"valid_from": "2025-06-01T00:00:00+08:00",
                  "valid_to": "2027-01-01T00:00:00+08:00",
                  "shares": [{"holder_code": "A", "percent": "80"},
                             {"holder_code": "B", "percent": "20"}]}
        r = c.post(f"/agreements/{aid}/amendments/preview", json=amend4)
        check("preview v4", r.status_code == 200, r.text)
        tok4 = r.json()["preview_token"]
        # 相关期间（6 月）再出具一次（无变化，不产生新单据，但最新快照不变 -> 不 stale）；
        # 真正制造变化：导入同作品新播放落在 6 月（更正链变化即已足够）。
        c.post("/plays/import", json={
            "import_id": "P3", "work_code": "W1",
            "occurred_at": "2026-07-01T20:00:00+08:00", "amount": 10})
        r = c.post(f"/agreements/{aid}/amendments/confirm",
                   json=dict(amend4, preview_token=tok4))
        # P3 导入本身已改变播放更正链 => stale
        check("stale after play+issue 409",
              r.status_code == 409 and r.json()["error"] == "amendment_preview_stale",
              r.text)

        # 无关作品变化不使令牌失效：预览 W2 协议不受影响（不同锁/不同指纹）
        c.post("/agreements", json={
            "work_code": "W2",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]})
        wid2 = c.get("/agreements", params={"work_code": "W2"}).json()[0]["id"]
        w2_amend = {"valid_from": "2026-03-01T00:00:00+08:00",
                    "shares": [{"holder_code": "B", "percent": "100"}]}
        r = c.post(f"/agreements/{wid2}/amendments/preview", json=w2_amend)
        tokw2 = r.json()["preview_token"]
        # W1 再补一版，不影响 W2 令牌
        pv4 = c.post(f"/agreements/{aid}/amendments/preview", json=amend4).json()
        c.post(f"/agreements/{aid}/amendments/confirm",
               json=dict(amend4, preview_token=pv4["preview_token"]))
        r = c.post(f"/agreements/{wid2}/amendments/confirm",
                   json=dict(w2_amend, preview_token=tokw2))
        check("other work amend unaffected", r.status_code == 201, r.text)

        # ---- 11) 不可变性：版本行与比例不可改写（直接 SQL 尝试） ----
        from app import db as dbmod
        with dbmod.pool.connection() as conn:
            for sql, args in (
                ("UPDATE agreement_versions SET note='x' WHERE agreement_id=%s AND version=1",
                 (aid,)),
                ("UPDATE agreement_version_shares SET percent=1 WHERE agreement_id=%s AND version=1",
                 (aid,)),
                ("DELETE FROM agreements WHERE id=%s", (aid,)),
                ("UPDATE agreements SET work_code='W2' WHERE id=%s", (aid,)),
                # 受控更新之外的版本号跳变也拒绝
                ("UPDATE agreements SET current_version=99 WHERE id=%s", (aid,)),
            ):
                rejected = False
                try:
                    with conn.transaction():
                        conn.execute(sql, args)
                except Exception:
                    rejected = True
                check(f"frozen rejects: {sql[:45]}", rejected)

        # ---- 12) 同作品并发修订：只有一个成功提交，另一个 stale（不覆盖） ----
        c.post("/works", json={"code": "WR", "title": "并发修订作品"})
        rid = c.post("/agreements", json={
            "work_code": "WR", "valid_from": "2026-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]}).json()["id"]
        c.post("/plays/import", json={
            "import_id": "PR1", "work_code": "WR",
            "occurred_at": "2026-02-01T00:00:00+08:00", "amount": 10})
        a_amend = {"valid_from": "2026-01-01T00:00:00+08:00",
                   "shares": [{"holder_code": "A", "percent": "60"},
                              {"holder_code": "B", "percent": "40"}]}
        b_amend = {"valid_from": "2026-01-01T00:00:00+08:00",
                   "shares": [{"holder_code": "A", "percent": "70"},
                              {"holder_code": "B", "percent": "30"}]}
        tok_a = c.post(f"/agreements/{rid}/amendments/preview", json=a_amend).json()["preview_token"]
        tok_b = c.post(f"/agreements/{rid}/amendments/preview", json=b_amend).json()["preview_token"]
        results = []

        def fire(payload, tok):
            rr = c.post(f"/agreements/{rid}/amendments/confirm",
                        json=dict(payload, preview_token=tok))
            results.append((rr.status_code, rr.json().get("error")))

        threads = [
            threading.Thread(target=fire, args=(a_amend, tok_a)),
            threading.Thread(target=fire, args=(b_amend, tok_b)),
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        check("concurrent amend one wins",
              sorted(results) == sorted([(201, None), (409, "amendment_preview_stale")])
              or sorted(results) == sorted([(201, None), (409, "agreement_version_conflict")]),
              str(results))
        final = c.get(f"/agreements/{rid}").json()
        check("winner is v2", final["version"] == 2)

        # ---- 13) 兼容：原登记/查询接口入参与字段仍在 ----
        c.post("/works", json={"code": "W9", "title": "兼容作品"})
        r = c.post("/agreements", json={
            "work_code": "W9",
            "valid_from": "2030-01-01T00:00:00+08:00",
            "valid_to": "2031-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "100"}]})
        check("old create contract",
              r.status_code == 201 and {"id", "work_code", "valid_from", "valid_to",
                                        "note", "shares"} <= set(r.json()))
        r = c.get("/agreements", params={"work_code": "W1"})
        check("list filter works",
              len(r.json()) == 2 and all(x["work_code"] == "W1" for x in r.json()))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL AMENDMENT INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
