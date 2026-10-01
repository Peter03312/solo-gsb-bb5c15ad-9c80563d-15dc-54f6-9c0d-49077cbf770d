"""付款额度预留指令（两阶段付款）：端到端集成测试（pgserver 便携 PostgreSQL）。

运行: PYTHONPATH=. python3 tests/integration_instructions.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_ins_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    # 配置必须在设置环境变量后导入
    from fastapi.testclient import TestClient
    import psycopg
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档 ----
        c.post("/works", json={"code": "W", "title": "作品"})
        c.post("/rightsholders", json={"code": "A", "name": "甲"})
        c.post("/rightsholders", json={"code": "B", "name": "乙"})
        c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"},
                       {"holder_code": "B", "percent": "40"}]})
        period = {"start": "2026-02-01T00:00:00+08:00",
                  "end": "2026-03-01T00:00:00+08:00"}
        # 出具 100 分单据：A=60，B=40
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json=period)

        # ---- 无余额：预留被拒 ----
        c.post("/rightsholders", json={"code": "C", "name": "丙"})
        r = c.post("/payment-instructions",
                   json={"holder_code": "C", "instruction_ref": "I0", "amount": 1})
        check("no balance reserve reject", r.status_code == 409
              and r.json()["error"] == "instruction_exceeds_balance"
              and r.json()["detail"]["balance"] == 0
              and r.json()["detail"]["available"] == 0, r.text)

        # 权利人不存在 / 金额非法
        r = c.post("/payment-instructions",
                   json={"holder_code": "NOBODY", "instruction_ref": "IX", "amount": 1})
        check("reserve holder missing 404", r.status_code == 404
              and r.json()["error"] == "holder_not_found", r.text)
        for bad in (0, -1, 1.5, True):
            r = c.post("/payment-instructions",
                       json={"holder_code": "A", "instruction_ref": "IX", "amount": bad})
            check(f"reserve bad amount {bad!r} 422", r.status_code == 422, r.text)

        # ---- 预留 40（A 余 60）=> available 20 ----
        r = c.post("/payment-instructions",
                   json={"holder_code": "A", "instruction_ref": "IA1", "amount": 40})
        b1 = r.json()
        check("reserve 40 created", r.status_code == 201
              and b1["status"] == "created"
              and b1["instruction_status"] == "pending"
              and b1["balance"] == 60 and b1["reserved_total"] == 40
              and b1["available"] == 20 and b1["instruction_id"] >= 1, r.text)

        # 同号同内容重试 => 200 duplicate，原指令、无新预留
        r = c.post("/payment-instructions",
                   json={"holder_code": "A", "instruction_ref": "IA1", "amount": 40})
        b1r = r.json()
        check("reserve idempotent", r.status_code == 200
              and b1r["status"] == "duplicate"
              and b1r["instruction_id"] == b1["instruction_id"]
              and b1r["available"] == 20, r.text)

        # 同号异内容（金额/权利人不同）=> 409
        r = c.post("/payment-instructions",
                   json={"holder_code": "A", "instruction_ref": "IA1", "amount": 10})
        check("reserve ref conflict amount", r.status_code == 409
              and r.json()["error"] == "instruction_ref_conflict"
              and r.json()["detail"]["existing_amount"] == 40, r.text)
        r = c.post("/payment-instructions",
                   json={"holder_code": "B", "instruction_ref": "IA1", "amount": 40})
        check("reserve ref conflict holder", r.status_code == 409
              and r.json()["error"] == "instruction_ref_conflict"
              and r.json()["detail"]["existing_holder_code"] == "A", r.text)

        # 超可用额预留：再留 21 > 可用 20 => 拒绝，不留指令
        r = c.post("/payment-instructions",
                   json={"holder_code": "A", "instruction_ref": "IAX", "amount": 21})
        check("reserve over available", r.status_code == 409
              and r.json()["error"] == "instruction_exceeds_balance"
              and r.json()["detail"]["available"] == 20
              and r.json()["detail"]["pending_reserved"] == 40, r.text)

        # 再留 20 恰好 => 可用 0
        r = c.post("/payment-instructions",
                   json={"holder_code": "A", "instruction_ref": "IA2", "amount": 20})
        check("reserve exact available", r.status_code == 201
              and r.json()["reserved_total"] == 60 and r.json()["available"] == 0,
              r.text)

        # ---- 原付款接口兼容：新付款须避开待确认预留 ----
        r = c.post("/payments",
                   json={"holder_code": "A", "external_ref": "PA-DIRECT", "amount": 1})
        check("direct payment blocked by reservation", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["balance"] == 60
              and r.json()["detail"]["pending_reserved"] == 60
              and r.json()["detail"]["available"] == 0, r.text)
        # B 的 40 不受 A 预留影响，可直接付款
        r = c.post("/payments",
                   json={"holder_code": "B", "external_ref": "PB1", "amount": 40})
        check("B direct pay unaffected", r.status_code == 201
              and r.json()["balance_after"] == 0, r.text)

        # 对账单：待确认预留、可用额、指令状态可见
        led = c.get("/rightsholders/A/ledger").json()
        check("ledger reservations visible",
              led["balance"] == 60 and led["reserved_total"] == 60
              and led["available"] == 0 and led["paid_total"] == 0
              and [i["instruction_ref"] for i in led["payment_instructions"]]
              == ["IA1", "IA2"]
              and all(i["status"] == "pending"
                      for i in led["payment_instructions"]), str(led))

        # ---- 取消 IA2（20）=> 释放，可用回到 20 ----
        r = c.post("/payment-instructions/IA2/cancel")
        bc = r.json()
        check("cancel pending", r.status_code == 200
              and bc["status"] == "cancelled"
              and bc["instruction_status"] == "cancelled"
              and bc["cancelled_at"] is not None
              and bc["reserved_total"] == 40 and bc["available"] == 20, r.text)
        # 重复取消 => 200 duplicate，幂等
        r = c.post("/payment-instructions/IA2/cancel")
        check("cancel idempotent", r.status_code == 200
              and r.json()["status"] == "duplicate"
              and r.json()["instruction_status"] == "cancelled"
              and r.json()["available"] == 20, r.text)
        # 取消已取消指令不影响账
        led = c.get("/rightsholders/A/ledger").json()
        check("cancelled released once",
              led["reserved_total"] == 40 and led["available"] == 20
              and len(led["payment_instructions"]) == 2, str(led))

        # 取消不存在 => 404
        r = c.post("/payment-instructions/NOPE/cancel")
        check("cancel missing 404", r.status_code == 404
              and r.json()["error"] == "instruction_not_found", r.text)

        # ---- 确认 IA1：原子登记付款并结束预留 ----
        r = c.post("/payment-instructions/IA1/confirm",
                   json={"external_ref": "BANK-IA1"})
        cf = r.json()
        check("confirm created", r.status_code == 201
              and cf["status"] == "created"
              and cf["instruction_status"] == "confirmed"
              and cf["payment"]["external_ref"] == "BANK-IA1"
              and cf["payment"]["amount"] == 40
              and cf["payment"]["holder_code"] == "A"
              and cf["balance_after"] == 20
              and cf["available_after"] == 20, r.text)
        # 确认后：付款已入账、预留已结束；直接付款仍受其余预留约束（此时无 pending）
        led = c.get("/rightsholders/A/ledger").json()
        check("after confirm ledger",
              led["billed_total"] == 60 and led["paid_total"] == 40
              and led["balance"] == 20 and led["reserved_total"] == 0
              and led["available"] == 20
              and {i["instruction_ref"]: i["status"]
                   for i in led["payment_instructions"]}
              == {"IA1": "confirmed", "IA2": "cancelled"}, str(led))

        # 确认重试同流水号 => 200 duplicate，同一付款，不重复入账
        r = c.post("/payment-instructions/IA1/confirm",
                   json={"external_ref": "BANK-IA1"})
        cfr = r.json()
        check("confirm retry idempotent", r.status_code == 200
              and cfr["status"] == "duplicate"
              and cfr["payment"]["payment_id"] == cf["payment"]["payment_id"]
              and cfr["balance_after"] == 20, r.text)
        check("retry no double pay",
              c.get("/rightsholders/A/ledger").json()["paid_total"] == 40)

        # 已确认指令换流水号 => 409 instruction_already_confirmed
        r = c.post("/payment-instructions/IA1/confirm",
                   json={"external_ref": "BANK-OTHER"})
        check("confirm diff ref rejected", r.status_code == 409
              and r.json()["error"] == "instruction_already_confirmed"
              and r.json()["detail"]["bound_payment_ref"] == "BANK-IA1", r.text)
        # 已确认/已取消指令取消 => 409
        r = c.post("/payment-instructions/IA1/cancel")
        check("cancel confirmed rejected", r.status_code == 409
              and r.json()["error"] == "instruction_status_conflict", r.text)
        # 取消后的指令再确认 => 409
        r = c.post("/payment-instructions/IA2/confirm",
                   json={"external_ref": "BANK-IA2"})
        check("confirm cancelled rejected", r.status_code == 409
              and r.json()["error"] == "instruction_status_conflict", r.text)
        # 确认不存在指令 => 404
        r = c.post("/payment-instructions/NOPE/confirm",
                   json={"external_ref": "X"})
        check("confirm missing 404", r.status_code == 404
              and r.json()["error"] == "instruction_not_found", r.text)

        # GET 单指令
        r = c.get("/payment-instructions/IA1")
        g = r.json()
        check("get instruction", r.status_code == 200
              and g["instruction_status"] == "confirmed"
              and g["payment_ref"] == "BANK-IA1" and g["available"] == 20, r.text)
        r = c.get("/payment-instructions/NOPE")
        check("get missing 404", r.status_code == 404
              and r.json()["error"] == "instruction_not_found", r.text)

        # ---- 流水号被其他付款占用 => pending 确认拒绝且指令不变 ----
        # A 可用 20：预留 IA3=10
        r = c.post("/payment-instructions",
                   json={"holder_code": "A", "instruction_ref": "IA3", "amount": 10})
        check("reserve IA3", r.status_code == 201 and r.json()["available"] == 10,
              r.text)
        # 直接付款接口登记同流水号（A 余可用 10，付 5）
        r = c.post("/payments",
                   json={"holder_code": "A", "external_ref": "OCC-1", "amount": 5})
        check("direct 5 ok", r.status_code == 201
              and r.json()["balance_after"] == 15, r.text)
        # 用 OCC-1 确认 IA3（金额 10 != 5，权利人同）=> 409 occupied，指令仍 pending
        r = c.post("/payment-instructions/IA3/confirm",
                   json={"external_ref": "OCC-1"})
        check("occupied ref rejected", r.status_code == 409
              and r.json()["error"] == "payment_ref_occupied"
              and r.json()["detail"]["existing_amount"] == 5, r.text)
        g = c.get("/payment-instructions/IA3").json()
        check("still pending after occupied reject",
              g["instruction_status"] == "pending" and g["payment_ref"] is None
              and g["reserved_total"] == 10 and g["available"] == 5, r.text)
        # 即使占用付款权利人/金额都不同，同样拒绝。
        # B 此前 40 已付 PB1、余额 0；先全额退回加回 40，再由 B 付 1。
        r = c.post("/refunds", json={"payment_ref": "PB1",
                                     "refund_ref": "RET-PB1", "amount": 40})
        check("refund PB1 40", r.status_code == 201
              and r.json()["balance_after"] == 40, r.text)
        r = c.post("/payments",
                   json={"holder_code": "B", "external_ref": "OCC-2", "amount": 1})
        check("B pay 1 after refund", r.status_code == 201, r.text)
        r = c.post("/payment-instructions/IA3/confirm",
                   json={"external_ref": "OCC-2"})
        check("occupied other holder rejected", r.status_code == 409
              and r.json()["error"] == "payment_ref_occupied"
              and r.json()["detail"]["existing_holder_code"] == "B", r.text)
        # 用全新流水号确认 IA3 => 成功；余额 15（60-40-5）-10 = 5
        r = c.post("/payment-instructions/IA3/confirm",
                   json={"external_ref": "BANK-IA3"})
        check("confirm IA3 ok", r.status_code == 201
              and r.json()["balance_after"] == 5
              and r.json()["available_after"] == 5, r.text)

        # ---- 负额追补防护：更早创建的 pending 指令受保护 ----
        # 全新权利人 F + 独立作品 WF（F 100% 分成）与独立期间，单据/付款均干净
        c.post("/rightsholders", json={"code": "F", "name": "己"})
        c.post("/works", json={"code": "WF", "title": "F 专属作品"})
        c.post("/agreements", json={
            "work_code": "WF",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "F", "percent": "100"}]})
        period_f = {"start": "2026-05-01T00:00:00+08:00",
                    "end": "2026-06-01T00:00:00+08:00"}
        c.post("/plays/import", json={
            "import_id": "PF-1", "work_code": "WF",
            "occurred_at": "2026-05-10T20:00:00+08:00", "amount": 40})
        c.post("/statements/issue", json=period_f)
        # F: billed 40，无付款/退回，balance 40
        check("F billed 40 clean",
              c.get("/rightsholders/F/ledger").json()["billed_total"] == 40
              and c.get("/rightsholders/F/ledger").json()["balance"] == 40)
        r = c.post("/payment-instructions",
                   json={"holder_code": "F", "instruction_ref": "IF1", "amount": 20})
        check("F reserve IF1 20", r.status_code == 201
              and r.json()["available"] == 20, r.text)
        r = c.post("/payment-instructions",
                   json={"holder_code": "F", "instruction_ref": "IF2", "amount": 20})
        check("F reserve IF2 20", r.status_code == 201
              and r.json()["available"] == 0, r.text)
        # 负额追补：40 -> 16（追补 -24），余额 40-24=16 => 仍 >= 自身，
        # 但为构造"自身也不足"，下一轮再降到 15：40 -> 15（追补 -25）
        c.post("/plays/import", json={
            "import_id": "PF-1R1", "work_code": "WF",
            "occurred_at": "2026-05-10T20:00:00+08:00", "amount": 15,
            "corrects_id": "PF-1"})
        c.post("/statements/issue", json=period_f)
        led = c.get("/rightsholders/F/ledger").json()
        check("F after negative supp",
              led["billed_total"] == 15 and led["balance"] == 15
              and led["reserved_total"] == 40 and led["available"] == -25, str(led))
        # 确认更早的 IF1(20)：更早 pending 为 0，须留足自身 20；余额 15 < 20
        # => 拒绝，IF1 保持 pending
        r = c.post("/payment-instructions/IF1/confirm",
                   json={"external_ref": "BANK-IF1"})
        check("IF1 confirm blocked by own amount after supp", r.status_code == 409
              and r.json()["error"] == "instruction_exceeds_balance"
              and r.json()["detail"]["required_amount"] == 20
              and r.json()["detail"]["balance"] == 15
              and r.json()["detail"]["earlier_pending_reserved"] == 0, r.text)
        g = c.get("/payment-instructions/IF1").json()
        check("IF1 stays pending", g["instruction_status"] == "pending", r.text)

        # 构造"自身足额但更早指令不足"场景：
        # 先取消 IF1/IF2，余额 F=15；预留 IF3=10（更早）、IF4=5（更晚）
        c.post("/payment-instructions/IF1/cancel")
        c.post("/payment-instructions/IF2/cancel")
        led = c.get("/rightsholders/F/ledger").json()
        check("F available 15 after cancels",
              led["reserved_total"] == 0 and led["available"] == 15, str(led))
        r = c.post("/payment-instructions",
                   json={"holder_code": "F", "instruction_ref": "IF3", "amount": 10})
        check("F reserve IF3", r.status_code == 201 and r.json()["available"] == 5,
              r.text)
        r = c.post("/payment-instructions",
                   json={"holder_code": "F", "instruction_ref": "IF4", "amount": 5})
        check("F reserve IF4", r.status_code == 201 and r.json()["available"] == 0,
              r.text)
        # 负额追补：15 -> 9（再追补 -6），余额 15-6=9
        c.post("/plays/import", json={
            "import_id": "PF-1R2", "work_code": "WF",
            "occurred_at": "2026-05-10T20:00:00+08:00", "amount": 9,
            "corrects_id": "PF-1R1"})
        c.post("/statements/issue", json=period_f)
        led = c.get("/rightsholders/F/ledger").json()
        check("F balance now 9", led["billed_total"] == 9 and led["balance"] == 9
              and led["reserved_total"] == 15 and led["available"] == -6, str(led))
        # 确认更晚的 IF4(5)：自身 5 + 更早 IF3 的 10 = 15 > 9 => 拒绝，IF4 不变
        r = c.post("/payment-instructions/IF4/confirm",
                   json={"external_ref": "BANK-IF4"})
        check("later confirm must reserve earlier", r.status_code == 409
              and r.json()["error"] == "instruction_exceeds_balance"
              and r.json()["detail"]["earlier_pending_reserved"] == 10
              and r.json()["detail"]["required_amount"] == 15
              and r.json()["detail"]["balance"] == 9, r.text)
        check("IF4 stays pending",
              c.get("/payment-instructions/IF4").json()["instruction_status"]
              == "pending")
        # 更早的 IF3(10)：更早 pending 为 0，余额 9 < 10 => 也被自身金额拒绝
        r = c.post("/payment-instructions/IF3/confirm",
                   json={"external_ref": "BANK-IF3"})
        check("earlier blocked by own amount", r.status_code == 409
              and r.json()["detail"]["earlier_pending_reserved"] == 0
              and r.json()["detail"]["required_amount"] == 10, r.text)

        # 正额恢复后（9 -> 99，追补 +90），先确认更早的 IF3 成功；
        # 此时 F balance = 99；IF3 确认后余额 89，
        # 更晚 IF4 确认须为更早 pending（IF3 已确认 => 0）留足，89 >= 5 => 成功
        c.post("/plays/import", json={
            "import_id": "PF-1R3", "work_code": "WF",
            "occurred_at": "2026-05-10T20:00:00+08:00", "amount": 99,
            "corrects_id": "PF-1R2"})
        c.post("/statements/issue", json=period_f)
        led = c.get("/rightsholders/F/ledger").json()
        check("F recovered", led["billed_total"] == 99
              and led["balance"] == 99 and led["available"] == 84
              and led["reserved_total"] == 15, str(led))
        r = c.post("/payment-instructions/IF3/confirm",
                   json={"external_ref": "BANK-IF3"})
        check("IF3 confirm after recovery", r.status_code == 201
              and r.json()["balance_after"] == 89, r.text)
        r = c.post("/payment-instructions/IF4/confirm",
                   json={"external_ref": "BANK-IF4"})
        check("IF4 confirm after earlier done", r.status_code == 201
              and r.json()["balance_after"] == 84
              and r.json()["available_after"] == 84, r.text)

        # ---- 退回加回余额，可被预留/付款使用 ----
        # A: balance 5（60-55）；给 A 的 OCC-1(5) 全退 => balance 10
        c.post("/refunds", json={"payment_ref": "OCC-1",
                                 "refund_ref": "RET-A-1", "amount": 5})
        led = c.get("/rightsholders/A/ledger").json()
        check("refund adds back", led["balance"] == 10 and led["available"] == 10,
              str(led))
        r = c.post("/payment-instructions",
                   json={"holder_code": "A", "instruction_ref": "IA5", "amount": 7})
        check("reserve after refund", r.status_code == 201
              and r.json()["available"] == 3, r.text)

        # ---- 指令历史不可改写（触发器）：只能发生一次状态迁移 ----
        with db.pool.connection() as db_conn:
            try:
                db_conn.execute(
                    "UPDATE payment_instructions SET amount = 1 WHERE instruction_ref = %s",
                    ("IA1",))
                amount_frozen = False
            except psycopg.Error:
                amount_frozen = True
            try:
                db_conn.execute(
                    "UPDATE payment_instructions SET status = 'pending' "
                    "WHERE instruction_ref = %s", ("IA1",))
                reopen_blocked = False
            except psycopg.Error:
                reopen_blocked = True
            try:
                db_conn.execute(
                    "DELETE FROM payment_instructions WHERE instruction_ref = %s",
                    ("IA2",))
                delete_blocked = False
            except psycopg.Error:
                delete_blocked = True
        check("instruction immutable/reopen/delete",
              amount_frozen and reopen_blocked and delete_blocked)

        # ---- 并发：同一指令号并发预留，恰一笔创建，其余幂等 ----
        results = []

        def fire_reserve():
            rr = c.post("/payment-instructions",
                        json={"holder_code": "A", "instruction_ref": "IRACE",
                              "amount": 3})
            results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_reserve) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ids = {b["instruction_id"] for _, b in results}
        n_created = sum(1 for s, _ in results if s == 201)
        n_dup = sum(1 for s, _ in results if s == 200)
        check("concurrent reserve serialized",
              len(ids) == 1 and n_created == 1 and n_dup == 7,
              f"{[(s, b['status']) for s, b in results]}")
        check("single reservation held",
              c.get("/rightsholders/A/ledger").json()["reserved_total"]
              == 10)  # IA5=7 + IRACE=3

        # ---- 并发：多笔不同指令 + 直接付款抢同一可用额，绝不超用 ----
        # 取消两笔释放，得到可用 10
        c.post("/payment-instructions/IA5/cancel")
        c.post("/payment-instructions/IRACE/cancel")
        check("A available 10 before race",
              c.get("/rightsholders/A/ledger").json()["available"] == 10)
        outcomes = {"reserved": 0, "paid": 0, "rejected": 0, "errors": []}

        def fire_instruction(i):
            rr = c.post("/payment-instructions",
                        json={"holder_code": "A",
                              "instruction_ref": f"IC-{i}", "amount": 4})
            if rr.status_code == 201:
                outcomes["reserved"] += 4
            elif rr.status_code == 409:
                outcomes["rejected"] += 1
            else:
                outcomes["errors"].append((rr.status_code, rr.text))

        def fire_payment(i):
            rr = c.post("/payments",
                        json={"holder_code": "A",
                              "external_ref": f"EC-{i}", "amount": 4})
            if rr.status_code == 201:
                outcomes["paid"] += 4
            elif rr.status_code == 409:
                outcomes["rejected"] += 1
            else:
                outcomes["errors"].append((rr.status_code, rr.text))

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=fire_instruction, args=(i,)))
            threads.append(threading.Thread(target=fire_payment, args=(i,)))
        [t.start() for t in threads]
        [t.join() for t in threads]
        led = c.get("/rightsholders/A/ledger").json()
        # 10 的可用额被 4 分一笔的预留/付款抢占：恰 2 笔成功（占用 8，余 2），
        # 预留与付款合计绝不超过 10
        check("concurrent reserve+pay no overuse",
              not outcomes["errors"]
              and outcomes["reserved"] + outcomes["paid"] == 8
              and outcomes["rejected"] == 8
              and led["paid_total"] + led["reserved_total"]
              == led["billed_total"] + led["refunded_total"] - 2,
              f"{outcomes} {led}")

        # ---- 并发：负额追补（出单）与确认同屏障触发，多轮覆盖两种先后次序 ----
        # 每位独立权利人：100 分单据后预留 80（可用 20）；更正播放先导入，
        # 屏障同时放行"出追补单（100->70，-30，余额变 70）"与"确认 80 预留"：
        #   先确认后出单 => 成功（确认时余额 100 >= 80），随后余额变 -10；
        #   先出单后确认 => 拒绝（70 < 80），指令保持 pending、不留付款。
        rounds = 12
        race_stats = {"confirmed": 0, "rejected": 0, "errors": []}
        per_round = []
        for i in range(rounds):
            h = f"E{i}"
            w = f"WE{i}"
            month = i + 1  # 2026 年 1-12 月，每位权利人独占一个期间
            c.post("/works", json={"code": w, "title": f"并发作品{i}"})
            c.post("/rightsholders", json={"code": h, "name": f"戊{i}"})
            c.post("/agreements", json={
                "work_code": w,
                "valid_from": "2026-01-01T00:00:00+08:00",
                "valid_to": "2027-01-01T00:00:00+08:00",
                "shares": [{"holder_code": h, "percent": "100"}]})
            period_e = {"start": f"2026-{month:02d}-01T00:00:00+08:00",
                        "end": f"2026-{month:02d}-28T00:00:00+08:00"}
            c.post("/plays/import", json={
                "import_id": f"PE-{i}", "work_code": w,
                "occurred_at": f"2026-{month:02d}-15T20:00:00+08:00",
                "amount": 100})
            r = c.post("/statements/issue", json=period_e)
            if r.status_code != 200 or not r.json().get("payable"):
                race_stats["errors"].append(("issue", i, r.text))
                continue
            r = c.post("/payment-instructions",
                       json={"holder_code": h,
                             "instruction_ref": f"IE-{i}", "amount": 80})
            if r.status_code != 201:
                race_stats["errors"].append(("reserve", i, r.text))
                continue
            # 更正先落库（不影响已出具单据，直到重新出具）
            c.post("/plays/import", json={
                "import_id": f"PE-{i}R", "work_code": w,
                "occurred_at": f"2026-{month:02d}-15T20:00:00+08:00",
                "amount": 70, "corrects_id": f"PE-{i}"})
            barrier = threading.Barrier(2)
            outcome = {}

            def issue_neg():
                barrier.wait()
                c.post("/statements/issue", json=period_e)

            def do_confirm():
                barrier.wait()
                rrc = c.post(f"/payment-instructions/IE-{i}/confirm",
                             json={"external_ref": f"BANK-IE-{i}"})
                outcome["code"] = rrc.status_code
                outcome["body"] = rrc.json() if rrc.headers.get(
                    "content-type", "").startswith("application/json") else rrc.text

            ti = threading.Thread(target=issue_neg)
            tc = threading.Thread(target=do_confirm)
            ti.start(); tc.start(); ti.join(); tc.join()
            per_round.append((h, outcome))
            code = outcome.get("code")
            if code == 201:
                race_stats["confirmed"] += 1
            elif code == 409 and outcome["body"].get("error") == \
                    "instruction_exceeds_balance":
                race_stats["rejected"] += 1
            else:
                race_stats["errors"].append(("confirm", i, outcome))

        for i, (h, outcome) in enumerate(per_round):
            g = c.get(f"/payment-instructions/IE-{i}").json()
            led = c.get(f"/rightsholders/{h}/ledger").json()
            code = outcome.get("code")
            if code == 201:
                # 先确认后出单：confirmed 绑定流水号，付款 80 已登记；余额 -10
                check(f"round {i} confirmed atomically",
                      g["instruction_status"] == "confirmed"
                      and g["payment_ref"] == f"BANK-IE-{i}"
                      and led["paid_total"] == 80
                      and led["billed_total"] == 70
                      and led["balance"] == -10 and led["reserved_total"] == 0,
                      str(outcome))
            else:
                # 先出单后确认：指令保持 pending 不变，且不留任何付款
                check(f"round {i} rejected unchanged",
                      g["instruction_status"] == "pending"
                      and g["payment_ref"] is None
                      and led["paid_total"] == 0
                      and led["billed_total"] == 70
                      and led["balance"] == 70
                      and led["reserved_total"] == 80
                      and led["available"] == -10,
                      str(outcome))

        # 说明：issue_statement 先重算再加权利人行锁，而 confirm 几乎立即取行锁，
        # 同屏障并发时行锁次序天然不对称（确认通常先到）；"先出单后确认 => 拒绝"
        # 的次序已在上面 F 权利人场景中确定性覆盖。这里的并发循环重点验证：
        # 两种路径并发不死锁、结果都满足不变量（确认必原子、拒绝不留付款）。
        check("issue/confirm race invariants",
              not race_stats["errors"]
              and race_stats["confirmed"] + race_stats["rejected"] == rounds
              and race_stats["confirmed"] >= 1,
              str(race_stats))

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL PAYMENT-INSTRUCTION INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
