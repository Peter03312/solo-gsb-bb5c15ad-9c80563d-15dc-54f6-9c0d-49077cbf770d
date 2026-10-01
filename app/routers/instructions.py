from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import instructions_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    InstructionCancelOut,
    InstructionConfirmIn,
    InstructionConfirmOut,
    InstructionDetail,
    InstructionOut,
    InstructionReserveIn,
)

router = APIRouter(tags=["payment-instructions"])


@router.post("/payment-instructions")
def reserve_payment(body: InstructionReserveIn, conn=Depends(get_conn)):
    """预留付款额度（两阶段付款第一阶段）。

    - 凭权利人编号、唯一指令号与正整数分额预留；仅当"当前余额 − 全部
      待确认预留"的可用额仍足额（为正且不小于金额）才创建 pending 指令，
      返回指令状态（instruction_status）与可用额（available）；
    - 同指令号同内容重试幂等返回原指令（status=duplicate，HTTP 200）；
      同号异内容（权利人或金额不同）409 instruction_ref_conflict；
    - 权利人不存在 404 holder_not_found；金额非正整数 422；
      可用额不足 409 instruction_exceeds_balance（detail 回显
      balance / pending_reserved / available），拒绝时不留任何指令。
    """
    result = svc.reserve(conn, body.holder_code, body.instruction_ref, body.amount)
    status_code = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status_code,
        content=InstructionOut(**result).model_dump(mode="json"),
    )


@router.get("/payment-instructions/{instruction_ref}",
            response_model=InstructionDetail)
def get_payment_instruction(instruction_ref: str, conn=Depends(get_conn)):
    """查询单个付款预留指令及其状态与当时可用额；不存在 404 instruction_not_found。"""
    row = svc.get_instruction(conn, None, instruction_ref)
    if row is None:
        raise APIError(
            404,
            "instruction_not_found",
            f"payment instruction {instruction_ref} not found",
        )
    return svc._instruction_payload(row, conn)


@router.post("/payment-instructions/{instruction_ref}/cancel")
def cancel_payment_instruction(instruction_ref: str, conn=Depends(get_conn)):
    """取消待确认指令并释放额度。

    - 仅 pending 指令可取消（pending -> cancelled，额度立即释放）；
    - 对已取消指令重复取消幂等返回原指令（status=duplicate，HTTP 200）；
      对已确认指令取消 409 instruction_status_conflict；指令不存在 404。
    """
    result = svc.cancel(conn, instruction_ref)
    # 取消与对已取消指令的重复取消均返回 200（status 区分 cancelled/duplicate）
    return JSONResponse(
        status_code=200,
        content=InstructionCancelOut(**result).model_dump(mode="json"),
    )


@router.post("/payment-instructions/{instruction_ref}/confirm")
def confirm_payment_instruction(
    instruction_ref: str, body: InstructionConfirmIn, conn=Depends(get_conn)
):
    """确认预留（两阶段付款第二阶段）：提交外部付款流水号。

    - 原子登记付款并结束预留（pending -> confirmed），返回付款记录与余额；
    - 同流水号确认重试幂等（status=duplicate，HTTP 200，返回原付款记录）；
      已确认指令换流水号 409 instruction_already_confirmed；
      流水号已被其他付款占用 409 payment_ref_occupied（指令保持 pending 不变）；
    - 负额追补防护：余额须同时覆盖本指令金额与全部更早创建的待确认指令预留，
      否则 409 instruction_exceeds_balance（detail 含 earlier_pending_reserved、
      required_amount），拒绝且指令保持 pending；
    - 取消后的指令确认 409 instruction_status_conflict；指令不存在 404。
    """
    result = svc.confirm(conn, instruction_ref, body.external_ref)
    status_code = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status_code,
        content=InstructionConfirmOut(**result).model_dump(mode="json"),
    )
