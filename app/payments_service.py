"""付款登记、银行退回与权利人对账。

余额口径：全部期间已出具单据明细行（初次单与正负追补单）金额的代数和，
减去全部已登记付款，再加上全部已登记银行退回。试算（preview）不出具、
待处理播放不计入应付，因此二者天然不进入余额。

并发协议（与出单互斥，杜绝超付/超退）：
- 登记付款先取"外部流水号事务咨询锁"（序列化同一流水号的并发重试），
  再对该权利人行取行锁；余额校验与付款插入在同一事务内原子完成。
- 登记银行退回先取"退回流水号事务咨询锁"，再锁定原付款行
  （同一原付款的多笔并发退回在此串行，累计退回绝不合计超额），
  随后锁定权利人行（与付款/出单的余额校验互斥）。
- 出单（statements_service）在写任何明细行前，按权利人编号排序锁定
  本次受影响的全部权利人行。加锁次序统一为"付款/退回流水号咨询锁 →
  付款行（仅退回）→ 按编号升序的权利人行"，无死锁。
"""
import hashlib

# 付款流水号咨询锁命名空间
_PAYMENT_REF_NS = 0x50415931  # "PAY1"
# 付款预留指令号咨询锁命名空间（instructions_service 复用同一命名空间约定）
INSTRUCTION_REF_NS = 0x50494E31  # "PIN1"


def _combined_key(namespace: int, key: int) -> int:
    n = ((namespace & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
    return n - (1 << 64) if n >= (1 << 63) else n


def _ref_lock_key(external_ref: str) -> int:
    digest = hashlib.blake2b(external_ref.encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big", signed=False)
    return n - (1 << 64) if n >= (1 << 63) else n


# 全部期间已出具单据的正负金额累计；追补差额为有符号值，直接代数相加。
# 待处理播放不入 statement_lines，故不影响余额；preview 根本不出单。
_BILLED_TOTAL_SQL = """
SELECT COALESCE(SUM(amount), 0) AS billed
FROM statement_lines
WHERE holder_code = %s
"""

_PAID_TOTAL_SQL = """
SELECT COALESCE(SUM(amount), 0) AS paid
FROM payments
WHERE holder_code = %s
"""

# 银行退回累计：payment_refunds 经 payment_id 关联付款，退回加回余额。
_REFUNDED_TOTAL_SQL = """
SELECT COALESCE(SUM(r.amount), 0) AS refunded
FROM payment_refunds r
JOIN payments p ON p.id = r.payment_id
WHERE p.holder_code = %s
"""


def _billed_total(conn, holder_code: str) -> int:
    return int(conn.execute(_BILLED_TOTAL_SQL, (holder_code,)).fetchone()["billed"])


def _paid_total(conn, holder_code: str) -> int:
    return int(conn.execute(_PAID_TOTAL_SQL, (holder_code,)).fetchone()["paid"])


def _refunded_total(conn, holder_code: str) -> int:
    return int(
        conn.execute(_REFUNDED_TOTAL_SQL, (holder_code,)).fetchone()["refunded"]
    )


def get_balance(conn, holder_code: str) -> int:
    """当前余额 = 已出具单据累计 - 已登记付款 + 已退回累计（可为负）。"""
    return (
        _billed_total(conn, holder_code)
        - _paid_total(conn, holder_code)
        + _refunded_total(conn, holder_code)
    )


# 全部待确认预留指令的分额合计：仅 status='pending' 占用可用额，
# 已确认的已转为实际付款（payments 中扣减），已取消的不再占用。
_PENDING_RESERVED_SQL = """
SELECT COALESCE(SUM(amount), 0) AS reserved
FROM payment_instructions
WHERE holder_code = %s AND status = 'pending'
"""


def pending_reserved_total(conn, holder_code: str, *, before_id: int | None = None) -> int:
    """待确认预留合计；before_id 给定（如待确认的本指令自身 id）时只计更早创建的指令。

    "更早创建"按指令 id（即创建次序，与 created_at 同序）严格小于判定。
    确认指令做负额追补防护时，必须为这些更早的 pending 指令留足额度。
    """
    sql = _PENDING_RESERVED_SQL
    params: tuple = (holder_code,)
    if before_id is not None:
        sql += " AND id < %s"
        params = (holder_code, before_id)
    return int(conn.execute(sql, params).fetchone()["reserved"])


def get_available(conn, holder_code: str) -> int:
    """可用额 = 当前余额 - 全部待确认预留（可为负：负额追补后余额被冲减）。"""
    return get_balance(conn, holder_code) - pending_reserved_total(conn, holder_code)


def lock_holders_for_billing(conn, holder_codes) -> None:
    """出单侧调用：按编号升序逐个锁定本次受影响的权利人，再写单据明细。

    与付款登记持有的权利人行锁互斥；逐个、升序加锁，使多权利人事务间
    加锁次序全局一致，避免死锁（不依赖数据库执行计划的取行顺序）。
    """
    codes = sorted({c for c in holder_codes if c})
    for code in codes:
        conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (code,),
        )


def register_payment(conn, holder_code: str, external_ref: str, amount: int) -> dict:
    """登记一笔已完成付款。

    - 权利人不存在：404 holder_not_found，不留付款；
    - 同一流水号同内容重试：返回原付款记录与登记后余额（幂等）；
    - 同一流水号异内容（权利人或金额不同）：409 payment_ref_conflict；
    - 当前正可用额不足（可用额 <= 0 或 amount > 可用额）：
      409 payment_exceeds_balance，拒绝且不留付款。
      可用额 = 当前余额 - 全部待确认预留：原"不超过正余额"的超额拒绝约定保持
      兼容（错误码、balance 字段不变），新付款同时避开待确认预留额度。
    """
    with conn.transaction():
        # 先按流水号加锁：并发的同流水号请求在此串行化，
        # 避免唯一约束竞态下误报，也保证"同内容/异内容"判定确定。
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_PAYMENT_REF_NS, _ref_lock_key(external_ref)),),
        )

        existing = conn.execute(
            "SELECT id, holder_code, amount, created_at FROM payments WHERE external_ref = %s",
            (external_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["holder_code"] != holder_code
                or int(existing["amount"]) != amount
            ):
                raise _conflict(external_ref, existing)
            return {
                "payment_id": existing["id"],
                "holder_code": existing["holder_code"],
                "external_ref": external_ref,
                "amount": int(existing["amount"]),
                "registered_at": existing["created_at"],
                "status": "duplicate",
                "balance_after": get_balance(conn, existing["holder_code"]),
            }

        # 锁定权利人行：与出单侧行锁、预留/确认/取消持有的行锁互斥；
        # 权利人不存在则 404。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:
            raise _holder_not_found(holder_code)

        balance = get_balance(conn, holder_code)
        reserved = pending_reserved_total(conn, holder_code)
        available = balance - reserved
        # 新付款须避开待确认预留：仍只允许在正的可用额内入账
        if available <= 0 or amount > available:
            raise _exceeds_balance(holder_code, amount, balance, reserved, available)

        row = conn.execute(
            """
            INSERT INTO payments(holder_code, external_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id, created_at
            """,
            (holder_code, external_ref, amount),
        ).fetchone()
        return {
            "payment_id": row["id"],
            "holder_code": holder_code,
            "external_ref": external_ref,
            "amount": amount,
            "registered_at": row["created_at"],
            "status": "created",
            "balance_after": balance - amount,
        }


def _holder_not_found(holder_code: str):
    from .errors import APIError

    return APIError(
        404,
        "holder_not_found",
        f"rightsholder {holder_code} not found",
    )


def _conflict(external_ref: str, existing) -> "object":
    from .errors import APIError

    return APIError(
        409,
        "payment_ref_conflict",
        f"external payment ref {external_ref} already registered with different content",
        detail={
            "external_ref": external_ref,
            "existing_holder_code": existing["holder_code"],
            "existing_amount": int(existing["amount"]),
        },
    )


def _exceeds_balance(
    holder_code: str,
    amount: int,
    balance: int,
    reserved: int = 0,
    available: int | None = None,
):
    from .errors import APIError

    if available is None:
        available = balance - reserved
    return APIError(
        409,
        "payment_exceeds_balance",
        f"payment amount {amount} exceeds current available balance {available} "
        f"of rightsholder {holder_code} (balance {balance}, pending reservations "
        f"{reserved}); payment rejected",
        detail={
            "holder_code": holder_code,
            "requested_amount": amount,
            "balance": balance,
            # 新增字段（向后兼容，原有 balance 约定不变）
            "pending_reserved": reserved,
            "available": available,
        },
    )


def get_holder_ledger(conn, holder_code: str):
    """按权利人查询：基本信息、单据增减、付款流水与当前余额。"""
    holder = conn.execute(
        "SELECT code, name FROM rightsholders WHERE code = %s",
        (holder_code,),
    ).fetchone()
    if holder is None:
        return None

    statement_rows = conn.execute(
        """
        SELECT b.id AS batch_id, b.kind, b.period_start, b.period_end,
               b.created_at AS issued_at, l.amount
        FROM statement_lines l
        JOIN statement_batches b ON b.id = l.batch_id
        WHERE l.holder_code = %s
        ORDER BY b.id
        """,
        (holder_code,),
    ).fetchall()

    payment_rows = conn.execute(
        """
        SELECT id, external_ref, amount, created_at
        FROM payments
        WHERE holder_code = %s
        ORDER BY id
        """,
        (holder_code,),
    ).fetchall()

    refund_rows = conn.execute(
        """
        SELECT r.id AS refund_id, r.refund_ref, r.amount, r.created_at,
               r.payment_id, p.external_ref AS payment_ref
        FROM payment_refunds r
        JOIN payments p ON p.id = r.payment_id
        WHERE p.holder_code = %s
        ORDER BY r.id
        """,
        (holder_code,),
    ).fetchall()

    instruction_rows = conn.execute(
        """
        SELECT i.id, i.instruction_ref, i.amount, i.status, i.created_at,
               i.confirmed_at, i.cancelled_at, i.payment_id,
               p.external_ref AS payment_ref
        FROM payment_instructions i
        LEFT JOIN payments p ON p.id = i.payment_id
        WHERE i.holder_code = %s
        ORDER BY i.id
        """,
        (holder_code,),
    ).fetchall()

    billed_total = sum(int(r["amount"]) for r in statement_rows)
    paid_total = sum(int(r["amount"]) for r in payment_rows)
    refunded_total = sum(int(r["amount"]) for r in refund_rows)
    balance = billed_total - paid_total + refunded_total
    # 仅待确认预留占用可用额；已确认的预留已转为实际付款（计入 paid_total）
    reserved_total = sum(
        int(r["amount"]) for r in instruction_rows if r["status"] == "pending"
    )

    return {
        "holder_code": holder["code"],
        "name": holder["name"],
        "billed_total": billed_total,
        "paid_total": paid_total,
        "refunded_total": refunded_total,
        "balance": balance,
        # 待确认预留合计与可用额（可用额 = 余额 - 待确认预留，可为负）
        "reserved_total": reserved_total,
        "available": balance - reserved_total,
        "statements": [
            {
                "batch_id": r["batch_id"],
                "kind": r["kind"],
                "period_start": r["period_start"],
                "period_end": r["period_end"],
                "amount": int(r["amount"]),
                "issued_at": r["issued_at"],
            }
            for r in statement_rows
        ],
        "payments": [
            {
                "payment_id": r["id"],
                "external_ref": r["external_ref"],
                "amount": int(r["amount"]),
                "registered_at": r["created_at"],
            }
            for r in payment_rows
        ],
        "refunds": [
            {
                "refund_id": r["refund_id"],
                "refund_ref": r["refund_ref"],
                "payment_id": r["payment_id"],
                "payment_ref": r["payment_ref"],
                "amount": int(r["amount"]),
                "registered_at": r["created_at"],
            }
            for r in refund_rows
        ],
        "payment_instructions": [
            {
                "instruction_id": r["id"],
                "instruction_ref": r["instruction_ref"],
                "amount": int(r["amount"]),
                "status": r["status"],
                "payment_id": r["payment_id"],
                "payment_ref": r["payment_ref"],
                "created_at": r["created_at"],
                "confirmed_at": r["confirmed_at"],
                "cancelled_at": r["cancelled_at"],
            }
            for r in instruction_rows
        ],
    }
