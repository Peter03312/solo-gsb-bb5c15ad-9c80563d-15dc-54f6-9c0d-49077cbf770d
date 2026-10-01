from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import settings


def _tz_aware(dt: datetime, field: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{field} must include a timezone, e.g. 2026-01-15T20:00:00+08:00")
    return dt


class WorkIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)


class WorkOut(BaseModel):
    code: str
    title: str
    created: bool


class HolderIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=500)


class HolderOut(BaseModel):
    code: str
    name: str | None
    created: bool


class ShareIn(BaseModel):
    # 用字符串接收，避免浮点误差，如 "62.5"；数字也兼容
    holder_code: str = Field(min_length=1, max_length=200)
    percent: Decimal = Field(gt=0, le=100)

    @field_validator("percent")
    @classmethod
    def scale_limit(cls, v: Decimal) -> Decimal:
        exp = v.as_tuple().exponent
        digits = -exp if isinstance(exp, int) and exp < 0 else 0
        if digits > settings.percent_scale:
            raise ValueError(
                f"percent supports at most {settings.percent_scale} decimal places"
            )
        return v


class AgreementIn(BaseModel):
    work_code: str = Field(min_length=1)
    valid_from: datetime
    valid_to: datetime | None = None
    note: str | None = None
    shares: list[ShareIn] = Field(min_length=1)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def tz(cls, v: datetime | None, info):
        if v is not None:
            _tz_aware(v, info.field_name)
        return v

    @model_validator(mode="after")
    def validate_model(self):
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        holder_codes = [s.holder_code for s in self.shares]
        if len(set(holder_codes)) != len(holder_codes):
            raise ValueError("duplicate holder_code in shares")
        total = sum((s.percent for s in self.shares), Decimal(0))
        if total != Decimal(100):
            raise ValueError(f"shares percent must total exactly 100, got {total}")
        return self


class AgreementShareOut(BaseModel):
    holder_code: str
    percent: str


class AgreementOut(BaseModel):
    id: int
    work_code: str
    valid_from: datetime
    valid_to: datetime | None
    note: str | None
    # 当前版本号：首次登记为 1，每次改版 +1（协议编号 id 保持稳定）
    version: int = 1
    shares: list[AgreementShareOut]


class AgreementVersionOut(BaseModel):
    """协议的一个历史版本（现行版本的 version 等于 GET /agreements/{id} 的值）。"""
    agreement_id: int
    version: int
    work_code: str
    valid_from: datetime
    valid_to: datetime | None
    note: str | None
    created_at: datetime
    # 该版本被下一版取代的时间；现行版本为 None
    superseded_at: datetime | None
    current: bool
    shares: list[AgreementShareOut]


# ---- 协议改版（新适用区间 + 完整权利人比例）：先预览、后确认 ----

class AgreementRevisionIn(BaseModel):
    """改版请求（预览与确认同一入参）：
    按协议编号定位，seen_version 为调用方所见的现行版本号。
    """
    agreement_id: int
    seen_version: int = Field(ge=1)
    valid_from: datetime
    valid_to: datetime | None = None
    note: str | None = None
    shares: list[ShareIn] = Field(min_length=1)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def tz(cls, v: datetime | None, info):
        if v is not None:
            _tz_aware(v, info.field_name)
        return v

    @model_validator(mode="after")
    def validate_model(self):
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        holder_codes = [s.holder_code for s in self.shares]
        if len(set(holder_codes)) != len(holder_codes):
            raise ValueError("duplicate holder_code in shares")
        total = sum((s.percent for s in self.shares), Decimal(0))
        if total != Decimal(100):
            raise ValueError(f"shares percent must total exactly 100, got {total}")
        return self


class RevisionConfirmIn(AgreementRevisionIn):
    """确认改版：完整提交新版内容 + 预览令牌（防篡改、绑定所见版本与预览时数据指纹）。"""
    preview_token: str = Field(min_length=10, max_length=8192)


class AffectedPlayOut(BaseModel):
    """受改版影响的链尾播放（按播放发生时刻重新匹配后）。"""
    import_id: str
    root_import_id: str
    work_code: str
    occurred_at: datetime
    amount: int
    # 现行匹配协议编号（可能为 None=当前待处理）
    before_agreement_id: int | None
    # 新版区间生效后的匹配协议编号（None=将失去协议匹配，转待处理）
    after_agreement_id: int | None
    # 现行逐笔分摊（无协议匹配时为空对象）
    before_shares: dict[str, int]
    # 新版逐笔分摊（无协议匹配时为空对象）
    after_shares: dict[str, int]
    # 当前是否在待处理清单
    before_pending: bool
    # 新版生效后是否转待处理
    after_pending: bool


class IssuedPeriodImpactOut(BaseModel):
    """已出单期间的应付影响预览（确认后须经原出具/追补机制体现，不自动改写单据）。"""
    period_start: datetime
    period_end: datetime
    # 该期间已出具单据累计（已入账应付），权利人 -> 金额
    billed: dict[str, int]
    # 按现行数据重算的当前应付
    current_payable: dict[str, int]
    # 新版区间生效后的应付
    projected_payable: dict[str, int]
    # 本次改版造成的应付差额 = projected - current（各权利人，可正可负）
    delta: dict[str, int]


class RevisionPreviewOut(BaseModel):
    agreement_id: int
    work_code: str
    seen_version: int
    new_version: int
    valid_from: datetime
    valid_to: datetime | None
    shares: list[AgreementShareOut]
    # 确认凭证：绑定所见版本与预览时的协议/播放更正链/期间单据指纹
    preview_token: str
    affected_plays: list[AffectedPlayOut]
    # 现行在账、新版后失去协议匹配而转待处理的播放
    become_pending: list[AffectedPlayOut]
    # 现行待处理、新版区间覆盖后重新获得协议匹配的播放
    leave_pending: list[AffectedPlayOut]
    # 已出单期间各权利人的应付差额（仅列差额非零的期间）
    issued_periods: list[IssuedPeriodImpactOut]


class RevisionConfirmOut(BaseModel):
    """确认改版成功响应：版本切换结果 + 与预览同口径的影响明细（确认本身不出单）。"""
    # 固定 "revised"
    status: str
    agreement_id: int
    work_code: str
    version: int
    valid_from: datetime
    valid_to: datetime | None
    shares: list[AgreementShareOut]
    affected_plays: list[AffectedPlayOut]
    become_pending: list[AffectedPlayOut]
    leave_pending: list[AffectedPlayOut]
    issued_periods: list[IssuedPeriodImpactOut]


class PlayIn(BaseModel):
    import_id: str = Field(min_length=1, max_length=200)
    work_code: str = Field(min_length=1)
    occurred_at: datetime
    # 非负整数，最小货币单位
    amount: int = Field(ge=0)
    # 更正时引用原记录编号，本记录必须使用新编号
    corrects_id: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v

    @field_validator("occurred_at")
    @classmethod
    def tz(cls, v: datetime):
        return _tz_aware(v, "occurred_at")

    @model_validator(mode="after")
    def validate_model(self):
        if self.corrects_id is not None and self.corrects_id == self.import_id:
            raise ValueError("corrects_id must differ from import_id")
        return self


class PlayBulkIn(BaseModel):
    plays: list[PlayIn] = Field(min_length=1, max_length=1000)


class PlayResult(BaseModel):
    import_id: str
    status: str  # created | duplicate
    is_correction: bool
    corrects_id: str | None
    chain_tail: bool


class PlayChainItem(BaseModel):
    import_id: str
    work_code: str
    occurred_at: datetime
    amount: int
    corrects_id: str | None
    chain_tail: bool


class PlayChainOut(BaseModel):
    import_id: str
    chain: list[PlayChainItem]


class PeriodParams(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def tz(cls, v: datetime, info):
        return _tz_aware(v, info.field_name)

    @model_validator(mode="after")
    def validate_model(self):
        if self.end <= self.start:
            raise ValueError("end must be later than start")
        return self


class PendingItem(BaseModel):
    import_id: str
    work_code: str
    occurred_at: datetime
    amount: int
    reason: str


class StatementLineOut(BaseModel):
    holder_code: str
    amount: int
    delta: int | None = None


class StatementOut(BaseModel):
    batch_id: int
    kind: str
    supersedes_batch_id: int | None
    period_start: datetime
    period_end: datetime
    issued_at: datetime
    note: str | None = None
    # 是否具备逐笔归因证据；升级前的存量单据为 false（不可追溯）
    traceable: bool = True
    payable: list[StatementLineOut]
    pending: list[PendingItem]


class StatementSummary(BaseModel):
    batch_id: int
    kind: str
    supersedes_batch_id: int | None
    created_at: datetime
    traceable: bool = True
    lines: list[StatementLineOut]
    pending: list[PendingItem]


# ---- 逐笔归因证据 ----

class AttributionRecord(BaseModel):
    """单条逐笔归因：某权利人在某更正链（按原始编号归集）上的分摊。"""
    holder_code: str
    change_type: str  # added | removed | legacy
    # 更正链原始编号；legacy（旧账差额）行为 None
    root_import_id: str | None = None
    # 出具时冻结的链尾与核算上下文；legacy 行为 None
    tail_import_id: str | None = None
    work_code: str | None = None
    occurred_at: datetime | None = None
    agreement_id: int | None = None
    tail_amount: int | None = None
    share_percent: str | None = None
    # 该笔分摊对本单据的贡献：added 非负、removed 为负（撤销）、legacy 为旧账差额
    amount: int


class AttributionHolderTotal(BaseModel):
    holder_code: str
    # 该权利人在本单据全部归因记录的金额合计（含撤销负额、旧账差额）
    attributed_total: int
    # 该权利人在原单据明细行上的金额（初次单为应付，追补单为差额）
    statement_line_amount: int
    # 两者是否一致；不可追溯的存量单据恒为 false（无逐笔证据可核对）
    reconciles: bool


class AttributionPage(BaseModel):
    batch_id: int
    traceable: bool
    holder_code: str | None = None
    page: int
    page_size: int
    total_records: int
    # 各权利人合计（受 holder_code 过滤）；不可追溯单据为空列表
    holder_totals: list[AttributionHolderTotal]
    # 当前页归因记录
    records: list[AttributionRecord]


# ---- 付款登记与对账 ----

class PaymentIn(BaseModel):
    # 权利人编号
    holder_code: str = Field(min_length=1, max_length=200)
    # 外部付款流水号（调用方提供，全局唯一）
    external_ref: str = Field(min_length=1, max_length=200)
    # 正整数分额（最小货币单位）
    amount: int = Field(strict=True, ge=1)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool_amount(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v


class PaymentOut(BaseModel):
    payment_id: int
    holder_code: str
    external_ref: str
    amount: int
    registered_at: datetime
    # created=新登记；duplicate=同流水号同内容重试，返回原记录
    status: str
    # 登记后余额（同流水号重试时为当前余额）
    balance_after: int


# 单据增减条目（全部期间已出具单据的明细行，追补差额保留正负号）
class LedgerStatementItem(BaseModel):
    batch_id: int
    kind: str  # initial | supplement
    period_start: datetime
    period_end: datetime
    amount: int
    issued_at: datetime


# 付款流水条目（已登记付款，从余额中扣减）
class LedgerPaymentItem(BaseModel):
    payment_id: int
    external_ref: str
    amount: int
    registered_at: datetime


# 银行退回条目（凭原付款流水号登记，逐笔退回加回余额）
class LedgerRefundItem(BaseModel):
    refund_id: int
    # 退回流水号
    refund_ref: str
    # 原付款记录编号与原付款流水号
    payment_id: int
    payment_ref: str
    amount: int
    registered_at: datetime


# 付款预留指令条目（对账单可见待确认预留与全部指令状态，历史指令不可改写）
class LedgerInstructionItem(BaseModel):
    instruction_id: int
    instruction_ref: str
    amount: int
    # pending | confirmed | cancelled
    status: str
    payment_id: int | None = None
    payment_ref: str | None = None
    created_at: datetime
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None


class HolderLedger(BaseModel):
    holder_code: str
    name: str | None
    # 全部期间已出具单据明细的正负金额累计（不含试算、不含待处理播放）
    billed_total: int
    # 已登记付款累计
    paid_total: int
    # 已登记银行退回累计
    refunded_total: int
    # 当前余额 = billed_total - paid_total + refunded_total
    # （可为负：跨期负额追补后留待后续抵扣；退回只加回余额，不改变可负性）
    balance: int
    # 待确认预留（pending 指令）合计
    reserved_total: int = 0
    # 可用额 = balance - reserved_total（新付款与新预留均须在此额度内）
    available: int = 0
    statements: list[LedgerStatementItem]
    payments: list[LedgerPaymentItem]
    # 逐笔银行退回（原付款与单据不可变，退回只追加）
    refunds: list[LedgerRefundItem]
    # 全部付款预留指令及状态（历史指令不可改写）
    payment_instructions: list[LedgerInstructionItem] = []


# ---- 银行退票（付款退回）登记 ----

class RefundIn(BaseModel):
    # 原付款流水号（必须指向一笔已登记付款；不存在 404 payment_not_found）
    payment_ref: str = Field(min_length=1, max_length=200)
    # 退回流水号（调用方提供，全局唯一；同内容重试幂等、异内容 409）
    refund_ref: str = Field(min_length=1, max_length=200)
    # 正整数分额（最小货币单位）；累计退回超过原付款金额 409
    amount: int = Field(strict=True, ge=1)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool_amount(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v


class RefundOut(BaseModel):
    refund_id: int
    payment_id: int
    payment_ref: str
    refund_ref: str
    amount: int
    registered_at: datetime
    # created=新登记；duplicate=同退回流水号同内容重试，返回原记录
    status: str
    # 登记后余额（重试时为当前余额）
    balance_after: int


# ---- 付款额度预留指令（两阶段付款） ----

class InstructionReserveIn(BaseModel):
    # 权利人编号（须已建档；不存在 404 holder_not_found）
    holder_code: str = Field(min_length=1, max_length=200)
    # 唯一指令号（调用方提供，全局唯一；同内容重试幂等、异内容 409）
    instruction_ref: str = Field(min_length=1, max_length=200)
    # 正整数分额（最小货币单位）；须不超过当时可用额
    amount: int = Field(strict=True, ge=1)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool_amount(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v


class InstructionConfirmIn(BaseModel):
    # 外部付款流水号（全局唯一；同流水号确认重试幂等，异流水号/被占用 409）
    external_ref: str = Field(min_length=1, max_length=200)


# 指令本身的字段（生命周期状态 + 绑定的付款）
class InstructionBase(BaseModel):
    instruction_id: int
    instruction_ref: str
    holder_code: str
    amount: int
    # pending=待确认预留；confirmed=已确认（已登记付款）；cancelled=已取消
    instruction_status: str
    payment_id: int | None = None
    payment_ref: str | None = None
    created_at: datetime
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None


class InstructionOut(InstructionBase):
    # 请求结果：created=新建预留；duplicate=同号同内容重试，返回原指令
    status: str
    # 返回时的当前余额、待确认预留合计与可用额（可用额 = 余额 - 待确认预留）
    balance: int
    reserved_total: int
    available: int


class InstructionDetail(InstructionBase):
    # 查询单个指令：仅生命周期状态与当时余额/可用额
    balance: int
    reserved_total: int
    available: int


class InstructionCancelOut(InstructionBase):
    # cancelled=本次取消；duplicate=对已取消指令重复取消（幂等）
    status: str
    balance: int
    reserved_total: int
    available: int


class InstructionPaymentOut(BaseModel):
    # 确认时登记/命中的付款记录（字段与 POST /payments 输出保持一致）
    payment_id: int
    holder_code: str
    external_ref: str
    amount: int
    registered_at: datetime


class InstructionConfirmOut(BaseModel):
    instruction_id: int
    instruction_ref: str
    holder_code: str
    amount: int
    instruction_status: str  # confirmed
    payment: InstructionPaymentOut
    confirmed_at: datetime
    # 与 PaymentOut 同口径的顶层付款字段
    payment_id: int
    external_ref: str
    registered_at: datetime
    # 登记后余额；可用额 = 余额 - 其余待确认预留（本指令已结束预留）
    balance_after: int
    available_after: int
    # created=本次原子登记新付款；duplicate=同流水号确认重试，返回原付款
    status: str


# ---- 银行回单批次导入与付款核验 ----

class ReceiptLineIn(BaseModel):
    # 回单编号（全局内容绑定：异内容复用整批拒绝）
    receipt_no: str = Field(min_length=1, max_length=200)
    # 外部付款流水号（对照已登记付款的依据）
    external_ref: str = Field(min_length=1, max_length=200)
    # 权利人编号（回单所载，不要求已建档；与付款不一致即权利人不符）
    holder_code: str = Field(min_length=1, max_length=200)
    # 正整数分额（最小货币单位）
    amount: int = Field(strict=True, ge=1)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_bool_amount(cls, v):
        # JSON 的 true/false 虽是 int 子类，但不是合法金额
        if isinstance(v, bool):
            raise ValueError("amount must be an integer, not boolean")
        return v


class ReceiptBatchIn(BaseModel):
    # 批次编号（同内容重试幂等；异内容复用整批拒绝）
    batch_no: str = Field(min_length=1, max_length=200)
    receipts: list[ReceiptLineIn] = Field(min_length=1, max_length=1000)


class ReceiptLineOut(BaseModel):
    receipt_no: str
    external_ref: str
    holder_code: str
    amount: int
    # 是否重复回单（跨批次同一流水号，仅首次成功导入的参与核验）
    is_duplicate: bool


class ReceiptDifferenceOut(BaseModel):
    receipt_no: str
    external_ref: str
    # payment_not_registered | mismatched | duplicate
    status: str
    # 不符项：holder_mismatch / amount_mismatch；两项均不符同时列出
    differences: list[str]
    receipt_holder_code: str
    receipt_amount: int
    # 核验时冻结的已登记付款快照；付款未登记/重复回单为 None
    payment_holder_code: str | None
    payment_amount: int | None
    # 核验时冻结的"原付款累计退回额"；付款未登记/重复回单为 None。
    # 退票不改变按原付款字段判定的匹配/不符结论；该额仅随核验版本冻结。
    payment_refunded_total: int | None = None


class ReceiptVerificationLineOut(ReceiptDifferenceOut):
    # 匹配行（status=matched）的不符项恒为空；其余同差异行字段，
    # payment_refunded_total 为该版冻结时原付款的累计退回额。
    pass


class ReceiptVerificationOut(BaseModel):
    batch_no: str
    version: int
    created_at: datetime
    # 状态计数：matched / payment_not_registered / mismatched / duplicate / total
    counts: dict[str, int]
    # 差异行：全部非 matched 行（含重复回单），按回单编号升序
    differences: list[ReceiptDifferenceOut]
    # 全部回单行的冻结结论（含 matched 行），按回单编号升序；
    # 每行带该版冻结时原付款的累计退回额 payment_refunded_total
    lines: list[ReceiptVerificationLineOut] = []


class ReceiptBatchOut(BaseModel):
    batch_no: str
    # created=新导入；duplicate=同批次同内容重试，返回原批次
    status: str
    created_at: datetime
    receipts: list[ReceiptLineOut]
    # 导入时冻结的第 1 版逐行核验结论
    verification: ReceiptVerificationOut


class ReceiptBatchDetail(BaseModel):
    batch_no: str
    created_at: datetime
    line_count: int
    latest_version: int
    # 原始回单行（始终可查，不随核验版本变化）
    receipts: list[ReceiptLineOut]


class ReceiptReverifyOut(BaseModel):
    batch_no: str
    # created=结论变化已生成新版本；unchanged=结论不变，未新增版本
    status: str
    verification: ReceiptVerificationOut


class ReceiptVersionSummary(BaseModel):
    version: int
    created_at: datetime
    counts: dict[str, int]


class ReceiptVersionList(BaseModel):
    batch_no: str
    versions: list[ReceiptVersionSummary]
