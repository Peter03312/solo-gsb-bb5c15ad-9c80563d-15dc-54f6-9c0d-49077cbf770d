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
    # 现行版本号（协议修订后递增；协议编号 id 保持稳定）
    version: int = 1
    shares: list[AgreementShareOut]


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


# ---- 协议修订（按协议编号提交新版本的适用区间与完整比例） ----

class AmendmentIn(BaseModel):
    # 新适用区间（半开 [valid_from, valid_to)，时间必须带时区）；
    # valid_to 省略表示长期有效；valid_to <= valid_from 为无效区间（422）
    valid_from: datetime
    valid_to: datetime | None = None
    note: str | None = None
    # 完整权利人比例（不是增量），须合计恰好 100%
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


class AmendmentConfirmIn(AmendmentIn):
    # 预览返回的令牌；缺失/篡改/过期（状态已变化）均拒绝
    preview_token: str = Field(min_length=1, max_length=8000)


class AmendmentPlayRef(BaseModel):
    import_id: str
    root_import_id: str
    work_code: str
    occurred_at: datetime
    amount: int


class AmendmentAllocationSide(BaseModel):
    # 旧/新匹配协议；无匹配（待处理）时为 None
    agreement_id: int | None
    # 旧/新匹配版本；匹配的是其他协议时为其现行版本，本协议时为旧版/新版
    version: int | None
    shares: list[AgreementShareOut] | None
    # 逐笔分摊（holder -> 整数分额；无匹配为 None）
    allocation: dict[str, int] | None


class AmendmentAffectedPlay(AmendmentPlayRef):
    # become_allocated=原本待处理、修订后获得本协议匹配；
    # become_pending=原本由本协议覆盖、修订后失去匹配转待处理；
    # reallocated=修订前后均有匹配（区间/比例变化导致逐笔分摊变化，
    #   含新区间延伸到此前由其他协议覆盖的播放、或让出给其他协议的播放）
    change_type: str
    old: AmendmentAllocationSide
    new: AmendmentAllocationSide


class AmendmentPendingChanges(BaseModel):
    # 确认后将转入待处理的播放（旧区间覆盖、新区间不覆盖）
    to_pending: list[AmendmentPlayRef]
    # 确认后将离开待处理、获得匹配的播放（新区间延伸覆盖）
    from_pending: list[AmendmentPlayRef]


class AmendmentPeriodDelta(BaseModel):
    period_start: datetime
    period_end: datetime
    # 仅因本作品协议修订导致的该作品应付变化（holder -> 有符号差额）
    payable_delta_work: list[StatementLineOut]
    # 当前（修订前）该期间已冻结待处理清单中、确认后将离开待处理的播放
    currently_pending: list[AmendmentPlayRef]
    # 确认并重算后该期间的完整待处理清单预测
    pending_after: list[PendingItem]
    # 再出具时预计生成的追补单各权利人差额（当前全部应付投影 - 已入账累计，
    #   可正可负；为 0 的权利人不列出）
    projected_supplement: list[StatementLineOut]


class AmendmentPreviewOut(BaseModel):
    status: str  # preview
    agreement: dict
    agreement_id: int
    work_code: str
    current_version: int
    proposed_version: int
    valid_from: datetime
    valid_to: datetime | None
    note: str | None
    shares: list[AgreementShareOut]
    affected_plays: list[AmendmentAffectedPlay]
    pending_changes: AmendmentPendingChanges
    affected_periods: list[AmendmentPeriodDelta]
    preview_token: str


class AmendmentConfirmOut(BaseModel):
    status: str  # amended
    agreement: dict
    agreement_id: int
    work_code: str
    current_version: int
    proposed_version: int
    valid_from: datetime
    valid_to: datetime | None
    note: str | None
    shares: list[AgreementShareOut]
    affected_plays: list[AmendmentAffectedPlay]
    pending_changes: AmendmentPendingChanges
    affected_periods: list[AmendmentPeriodDelta]


class AgreementVersionOut(BaseModel):
    version: int
    is_current: bool
    valid_from: datetime
    valid_to: datetime | None
    note: str | None
    created_at: datetime
    superseded_at: datetime | None
    shares: list[AgreementShareOut]


class AgreementVersionsOut(BaseModel):
    agreement_id: int
    work_code: str
    current_version: int
    versions: list[AgreementVersionOut]


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
