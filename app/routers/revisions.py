"""协议改版（新适用区间 + 完整权利人比例）：先预览、后确认。"""
from fastapi import APIRouter, Depends

from .. import revisions_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    AgreementRevisionIn,
    AgreementVersionOut,
    RevisionConfirmIn,
    RevisionConfirmOut,
    RevisionPreviewOut,
)

router = APIRouter(prefix="/agreements", tags=["agreement-revisions"])


@router.post("/revisions/preview", response_model=RevisionPreviewOut)
def preview_revision(body: AgreementRevisionIn, conn=Depends(get_conn)):
    """按协议编号 + 所见版本预览一次协议改版（只读，不改账）。

    - 返回新版区间生效后受影响播放的待处理变化，以及已出单期间各权利人的
      应付差额（正/负），并签发一次性防篡改预览令牌；
    - 协议不存在 404 agreement_not_found；所见版本过期 409 agreement_version_stale；
      新区间与其他现行协议重叠 409 agreement_overlap；
      无效区间/比例合计不为 100%/时间不带时区 422。
    """
    return svc.preview_revision(conn, body)


@router.post("/revisions/confirm", response_model=RevisionConfirmOut)
def confirm_revision(body: RevisionConfirmIn, conn=Depends(get_conn)):
    """凭预览令牌确认改版。

    - 确认时若该作品的现行协议、相关播放更正链或相关期间单据在预览后发生变化，
      一律 409 revision_state_changed 拒绝且不修改任何数据；
    - 通过后协议编号保持稳定、版本号 +1，旧版可在 /agreements/{id}/versions 追溯；
    - 只切换现行版本，不出具、不改写任何既有单据、归因、付款或退回；
      已出单期间的正负差额随后由 /statements/issue 的原追补机制体现。
    """
    return svc.confirm_revision(conn, body)


@router.get("/{agreement_id}/versions", response_model=list[AgreementVersionOut])
def list_versions(agreement_id: int, conn=Depends(get_conn)):
    """列出协议的全部版本（含现行版本，current=true）；协议不存在 404。"""
    versions = svc.list_versions(conn, agreement_id)
    if versions is None:
        raise APIError(
            404, "agreement_not_found", f"agreement {agreement_id} not found"
        )
    return versions
