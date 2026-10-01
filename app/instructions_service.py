"""付款额度预留指令（两阶段付款）。

财务可凭"权利人编号（holder_code）+ 唯一指令号（instruction_ref）+
正整数分额（amount）"预留付款额度：

- 预留：仅当"当前余额 − 全部待确认预留"的可用额仍足额（为正且不小于金额）
  时才创建 pending 指令，返回指令状态与可用额；同号同内容重试幂等返回原指令，
  同号异内容（权利人或金额不同）拒绝（409 instruction_ref_conflict）。
- 取消：仅待确认指令可取消，取消后释放额度（pending -> cancelled）；
  对已取消指令重复取消幂等；对已确认指令取消拒绝（409 instruction_status_conflict）。
- 确认：提交外部付款流水号，原子地登记付款并结束预留（pending -> confirmed），
  返回付款记录及余额；同流水号重试幂等（status=duplicate），异流水号或流水号
  已被其他付款占用拒绝（409）。
- 负额追补防护：确认时除本指令外还须为"更早创建的待确认指令"留足额度——
  要求 当前余额 − 本笔付款 − 更早 pending 合计 >= 0，否则拒绝且指令保持 pending
  不变（409 instruction_exceeds_balance）。

并发协议（与付款登记、退票、出单共用固定加锁次序，无死锁）：
1) 指令号事务咨询锁 —— 同号并发请求（含预留/取消/确认）在此串行化；
   确认时另取"外部付款流水号"咨询锁（与付款登记、退票同命名空间），
   两种咨询锁按"指令号 → 付款流水号"次序获取；
2) 权利人行锁 FOR UPDATE —— 与付款登记/退票/出单互斥，保证余额、可用额、
   待确认预留合计与"更早 pending"判定在同一快照下一致。
"""
from .errors import APIError
from .payments_service import (
    INSTRUCTION_REF_NS,
    _combined_key,
    _ref_lock_key,
    get_balance,
    pending_reserved_total,
)

# 指令号咨询锁命名空间见 payments_service.INSTRUCTION_REF_NS（"PIN1"）

_INSTRUCTION_SELECT = """
SELECT i.id, i.instruction_ref, i.holder_code, i.amount, i.status,
       i.payment_id, i.created_at, i.confirmed_at, i.cancelled_at,
       p.external_ref AS payment_ref
FROM payment_instructions i
LEFT JOIN payments p ON p.id = i.payment_id
"""


def _instruction_payload(row, conn, *, status: str | None = None) -> dict:
    """组装指令响应；balance/available 取当时值。"""
    holder_code = row["holder_code"]
    balance = get_balance(conn, holder_code)
    reserved = pending_reserved_total(conn, holder_code)
    return {
        "instruction_id": row["id"],
        "instruction_ref": row["instruction_ref"],
        "holder_code": holder_code,
        "amount": int(row["amount"]),
        # 指令生命周期：pending | confirmed | cancelled
        "instruction_status": row["status"],
        "payment_id": row["payment_id"],
        "payment_ref": row["payment_ref"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "cancelled_at": row["cancelled_at"],
        "balance": balance,
        "reserved_total": reserved,
        "available": balance - reserved,
        # 请求结果：created=新建预留；duplicate=同号同内容重试，返回原指令
        **({"status": status} if status is not None else {}),
    }


def get_instruction(conn, holder_code: str | None, instruction_ref: str):
    sql = _INSTRUCTION_SELECT + "WHERE i.instruction_ref = %s"
    params: tuple = (instruction_ref,)
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    if holder_code is not None and row["holder_code"] != holder_code:
        return None
    return row


def reserve(conn, holder_code: str, instruction_ref: str, amount: int) -> dict:
    """预留付款额度。"""
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(INSTRUCTION_REF_NS, _ref_lock_key(instruction_ref)),),
        )

        existing = conn.execute(
            _INSTRUCTION_SELECT + "WHERE i.instruction_ref = %s",
            (instruction_ref,),
        ).fetchone()
        if existing is not None:
            if (
                existing["holder_code"] != holder_code
                or int(existing["amount"]) != amount
            ):
                raise _instruction_ref_conflict(instruction_ref, existing)
            return _instruction_payload(existing, conn, status="duplicate")

        # 锁定权利人行：与付款/退票/出单/其他指令互斥；不存在则 404。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:
            raise _holder_not_found(holder_code)

        balance = get_balance(conn, holder_code)
        reserved = pending_reserved_total(conn, holder_code)
        available = balance - reserved
        if available <= 0 or amount > available:
            raise _instruction_exceeds(holder_code, instruction_ref, amount,
                                       balance, reserved, available)

        row = conn.execute(
            """
            INSERT INTO payment_instructions(
                instruction_ref, holder_code, amount, status
            ) VALUES (%s, %s, %s, 'pending')
            RETURNING id, instruction_ref, holder_code, amount, status,
                      payment_id, created_at, confirmed_at, cancelled_at
            """,
            (instruction_ref, holder_code, amount),
        ).fetchone()
        row["payment_ref"] = None
        return _instruction_payload(row, conn, status="created")


def cancel(conn, instruction_ref: str) -> dict:
    """取消待确认指令并释放额度。"""
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(INSTRUCTION_REF_NS, _ref_lock_key(instruction_ref)),),
        )

        existing = conn.execute(
            _INSTRUCTION_SELECT + "WHERE i.instruction_ref = %s",
            (instruction_ref,),
        ).fetchone()
        if existing is None:
            raise _instruction_not_found(instruction_ref)

        if existing["status"] == "cancelled":
            # 重复取消幂等：指令、付款均不变
            return _instruction_payload(existing, conn, status="duplicate")
        if existing["status"] == "confirmed":
            raise _status_conflict(instruction_ref, existing["status"])

        # pending -> cancelled（触发器校验迁移合法性）
        row = conn.execute(
            """
            UPDATE payment_instructions
            SET status = 'cancelled', cancelled_at = now()
            WHERE id = %s AND status = 'pending'
            RETURNING id, instruction_ref, holder_code, amount, status,
                      payment_id, created_at, confirmed_at, cancelled_at
            """,
            (existing["id"],),
        ).fetchone()
        if row is None:  # 竞态兜底（行锁下正常不可达）
            raise _status_conflict(instruction_ref, existing["status"])
        row["payment_ref"] = None
        return _instruction_payload(row, conn, status="cancelled")


def confirm(
    conn, instruction_ref: str, external_ref: str
) -> dict:
    """提交外部付款流水号确认预留：原子登记付款并结束预留。"""
    with conn.transaction():
        # 1) 指令号咨询锁：同号确认/取消/预留串行
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(INSTRUCTION_REF_NS, _ref_lock_key(instruction_ref)),),
        )
        # 2) 外部付款流水号咨询锁（付款登记同一命名空间）：
        #    与同流水号的直接付款/其他确认串行，占用判定确定。
        from .payments_service import _PAYMENT_REF_NS

        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (_combined_key(_PAYMENT_REF_NS, _ref_lock_key(external_ref)),),
        )

        existing = conn.execute(
            _INSTRUCTION_SELECT + "WHERE i.instruction_ref = %s",
            (instruction_ref,),
        ).fetchone()
        if existing is None:
            raise _instruction_not_found(instruction_ref)

        holder_code = existing["holder_code"]
        amount = int(existing["amount"])

        # 已确认：同流水号幂等返回；异流水号拒绝
        if existing["status"] == "confirmed":
            if existing["payment_ref"] == external_ref:
                result_status = "duplicate"
                payment_row = conn.execute(
                    "SELECT id, holder_code, external_ref, amount, created_at "
                    "FROM payments WHERE id = %s",
                    (existing["payment_id"],),
                ).fetchone()
                return _confirm_payload(existing, payment_row, conn,
                                        status=result_status)
            raise _instruction_already_confirmed(
                instruction_ref, existing["payment_ref"], external_ref
            )
        if existing["status"] == "cancelled":
            raise _status_conflict(instruction_ref, "cancelled")

        # pending：流水号若已被任何付款占用即拒绝（"被其他付款占用"）。
        # 确认的"登记付款 + 结束预留"在同一事务原子完成，不存在
        # "付款已登记、指令仍 pending"的中间态；因此 pending 下命中的付款
        # 必来自直接付款接口（或其他指令），即使权利人/金额恰好相同也不绑定。
        payment_row = conn.execute(
            "SELECT id, holder_code, external_ref, amount, created_at "
            "FROM payments WHERE external_ref = %s FOR UPDATE",
            (external_ref,),
        ).fetchone()

        # 3) 锁定权利人行：与出单/付款/退票/其他指令互斥，余额判定确定。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:  # 外键已建档，理论不可达
            raise _holder_not_found(holder_code)

        if payment_row is not None:
            raise _payment_ref_occupied(external_ref, payment_row)

        balance = get_balance(conn, holder_code)
        # 更早创建的待确认指令：确认本指令后它们仍须能拿到自己预留的额度。
        # 新付款：余额须同时覆盖本笔与全部更早的 pending 指令
        # （本指令原占用的可用额在确认后转为实际付款，可用额不变；
        # 负额追补冲减余额时，更早的 pending 预留受保护）。
        earlier_pending = pending_reserved_total(
            conn, holder_code, before_id=existing["id"]
        )
        if balance < amount + earlier_pending:
            raise _confirm_exceeds(
                holder_code, instruction_ref, amount,
                balance, earlier_pending
            )
        payment_row = conn.execute(
            """
            INSERT INTO payments(holder_code, external_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id, holder_code, external_ref, amount, created_at
            """,
            (holder_code, external_ref, amount),
        ).fetchone()

        # pending -> confirmed（触发器校验：必须绑定 payment_id、置 confirmed_at）
        row = conn.execute(
            """
            UPDATE payment_instructions
            SET status = 'confirmed', payment_id = %s, confirmed_at = now()
            WHERE id = %s AND status = 'pending'
            RETURNING id, instruction_ref, holder_code, amount, status,
                      payment_id, created_at, confirmed_at, cancelled_at
            """,
            (payment_row["id"], existing["id"]),
        ).fetchone()
        if row is None:  # 竞态兜底（指令号锁下正常不可达）
            raise _status_conflict(instruction_ref, existing["status"])
        row["payment_ref"] = external_ref
        return _confirm_payload(row, payment_row, conn, status="created")


def _confirm_payload(row, payment_row, conn, *, status: str) -> dict:
    """确认成功响应：付款记录 + 余额 + 可用额（附指令状态）。"""
    holder_code = row["holder_code"]
    balance = get_balance(conn, holder_code)
    reserved = pending_reserved_total(conn, holder_code)
    return {
        "instruction_id": row["id"],
        "instruction_ref": row["instruction_ref"],
        "holder_code": holder_code,
        "amount": int(row["amount"]),
        "instruction_status": "confirmed",
        "payment": {
            "payment_id": payment_row["id"],
            "holder_code": payment_row["holder_code"],
            "external_ref": payment_row["external_ref"],
            "amount": int(payment_row["amount"]),
            "registered_at": payment_row["created_at"],
        },
        "confirmed_at": row["confirmed_at"],
        # 与 POST /payments 一致的付款记录口径
        "payment_id": payment_row["id"],
        "external_ref": payment_row["external_ref"],
        "registered_at": payment_row["created_at"],
        "balance_after": balance,
        "available_after": balance - reserved,
        # created=本次原子登记了新付款；duplicate=流水号已存在的幂等确认/重试
        "status": status,
    }


def _holder_not_found(holder_code: str):
    return APIError(
        404,
        "holder_not_found",
        f"rightsholder {holder_code} not found",
    )


def _instruction_not_found(instruction_ref: str):
    return APIError(
        404,
        "instruction_not_found",
        f"payment instruction {instruction_ref} not found",
        detail={"instruction_ref": instruction_ref},
    )


def _instruction_ref_conflict(instruction_ref: str, existing):
    return APIError(
        409,
        "instruction_ref_conflict",
        f"instruction ref {instruction_ref} already used with different content",
        detail={
            "instruction_ref": instruction_ref,
            "existing_holder_code": existing["holder_code"],
            "existing_amount": int(existing["amount"]),
            "existing_status": existing["status"],
        },
    )


def _status_conflict(instruction_ref: str, current_status: str):
    return APIError(
        409,
        "instruction_status_conflict",
        f"payment instruction {instruction_ref} is {current_status} and the "
        f"requested transition is not allowed",
        detail={"instruction_ref": instruction_ref, "status": current_status},
    )


def _instruction_already_confirmed(instruction_ref: str, bound_ref: str,
                                   requested_ref: str):
    return APIError(
        409,
        "instruction_already_confirmed",
        f"payment instruction {instruction_ref} already confirmed with payment "
        f"ref {bound_ref}; cannot confirm with different ref {requested_ref}",
        detail={
            "instruction_ref": instruction_ref,
            "bound_payment_ref": bound_ref,
            "requested_payment_ref": requested_ref,
        },
    )


def _payment_ref_occupied(external_ref: str, existing_payment):
    return APIError(
        409,
        "payment_ref_occupied",
        f"external payment ref {external_ref} already occupied by a different "
        f"payment; confirmation rejected and instruction left pending",
        detail={
            "external_ref": external_ref,
            "existing_holder_code": existing_payment["holder_code"],
            "existing_amount": int(existing_payment["amount"]),
        },
    )


def _instruction_exceeds(holder_code, instruction_ref, amount,
                         balance, reserved, available):
    return APIError(
        409,
        "instruction_exceeds_balance",
        f"reservation amount {amount} exceeds available balance {available} "
        f"of rightsholder {holder_code} (balance {balance}, pending "
        f"reservations {reserved}); reservation rejected",
        detail={
            "holder_code": holder_code,
            "instruction_ref": instruction_ref,
            "requested_amount": amount,
            "balance": balance,
            "pending_reserved": reserved,
            "available": available,
        },
    )


def _confirm_exceeds(holder_code, instruction_ref, amount,
                     balance, earlier_pending):
    return APIError(
        409,
        "instruction_exceeds_balance",
        f"confirming instruction {instruction_ref} for {amount} would leave "
        f"insufficient funds for earlier pending reservations {earlier_pending} "
        f"of rightsholder {holder_code} (balance {balance}); confirmation "
        f"rejected and instruction left pending",
        detail={
            "holder_code": holder_code,
            "instruction_ref": instruction_ref,
            "requested_amount": amount,
            "balance": balance,
            "earlier_pending_reserved": earlier_pending,
            "required_amount": amount + earlier_pending,
        },
    )
