# 演出录音播放版税结算服务

Python + FastAPI + PostgreSQL 实现的播放版税结算服务，支持：

- 作品与权利人登记，分成协议登记（适用起止时间、多名权利人及各自比例）；
  - 同一作品协议区间不得重叠（"每作品事务咨询锁 + 区间重叠检测"强制，**无需任何 contrib 扩展**）；
  - 每笔协议比例须合计 **100%**（应用层 + 数据库延迟约束触发器双重校验）。
  - **协议修订（运营按协议编号提交带时区的新适用区间与完整权利人比例）**：
    - 先 **预览** 受影响播放的待处理变化（获得匹配/失去匹配/重新分摊）
      与**已出单期间各权利人的应付差额**，预览**不改任何账**，返回一次性预览令牌；
    - **确认**时若该作品的**现行协议、相关播放更正链或相关期间单据**在预览后变化，
      或所基于版本已过期、令牌/内容不符，一律 **409 拒绝且不修改数据**；
      新区间与其他现行协议**不得重叠**、比例须合计 **100%**，
      无效区间、未知权利人和不存在的协议同样拒绝；
    - 确认后**协议编号保持稳定、版本递增、旧版可追溯查询**（版本只追加，
      版本行/比例/协议主表由触发器保护不可改写或删除）；
    - 播放自确认后**按发生时刻使用现行版本**重算：失去协议匹配的播放**转待处理**，
      新区间延伸覆盖的播放离开待处理；**已出单期间不自动改写**，
      各权利人正负差额在再出具时**经原追补机制**体现（含逐笔 removed/added 归因）；
    - 既有单据、逐笔归因、付款/预留/退回历史**一律不改写**；
      原协议登记与查询接口的**入参、已有响应字段与错误约定保持兼容**
      （登记/详情响应仅新增 `version` 字段）。
- 播放记录导入：调用方唯一编号、作品、发生时间、非负整数金额（最小货币单位）。
  - 同编号同内容 **幂等**；同编号异内容 **拒绝（409）**；
  - 更正必须引用原编号（`corrects_id`）并使用新编号；**分叉、循环更正拒绝**；
    原记录留痕，仅更正链 **链尾** 参与核算。
- 按播放 **发生时间** 匹配协议；缺协议的播放列入 **待处理清单**，不计入应付。
- 逐笔按比例分摊到最小货币单位：先向下取整，余数按 **小数部分降序、权利人编号升序** 补足，逐笔总额严格守恒。
- 按期间出具权利人应付额与待处理清单：
  - 已出具单据 **不可覆盖**；
  - 后补协议或播放更正后，再出具按 **新应付额 − 此前单据累计额** 出具 **追补单**，差额 **可正可负**；
  - 应付无变化不产生新单据；同一期间并发出具经咨询锁串行化，**不会产生重复单据**。
- **逐笔归因证据（财务核对）**：每次出具同步冻结"每位权利人的每笔金额来自哪些播放"：
  - 按 **更正链原始编号** 归集，固化参与核算的 **链尾编号、作品、发生时间、匹配协议、比例与逐笔分摊**；
  - 追补单逐笔记录相对上次出具的差额：`added`（新增）/ `removed`（撤销，**负额逐笔保留不轧差**），
    跨期更正、协议后补造成的撤销与新增均可分辨；
  - 归因记录 **一经冻结不可修改/删除**（触发器强制），历史单据不随日后播放或协议变化；
    待处理播放不计入应付、不产生证据；
  - 按 **单据 + 权利人分页** 查询归因记录与逐权利人合计，
    合计 **恒等于该单据原有明细行**（`reconciles=true`）；
  - 升级前已存在、无逐笔证据的存量单据自动标记 **不可追溯**（`traceable=false`）；
    其追补中无法归因的旧账差额单列为 `legacy`（无播放字段），**绝不用当前播放冒充历史**；
  - 并发出具不会重复生成证据；应付无变化不生成新单据，也不生成归因。
- **按权利人付款核对（财务实付）**：
  - 权利人当前余额 = **全部期间已出具单据明细的正负金额累计 − 已登记付款 + 已登记银行退回**；
    试算（preview）不出具、待处理播放不入应付，**均不进余额**；
  - 登记付款须提供权利人编号、外部付款流水号与**正整数分额**，
    仅当金额 **不超过当时的正余额** 才入账，返回付款记录与登记后余额；
  - 同一流水号同内容重试 **幂等返回原记录**（`status=duplicate`，HTTP 200），
    异内容（权利人或金额不同）返回 **409 `payment_ref_conflict`**；
    权利人不存在 404、金额非正整数 422、余额不为正或超额 **409 `payment_exceeds_balance`**，
    拒绝时**不留任何付款**；
  - 跨期更正产生负额追补后 **不撤销历史付款**：余额变负，留待后续正额抵扣，
    **余额不为正时禁止付款**；付款记录一经登记不可修改/删除（触发器强制）；
  - 按权利人查询单据增减、付款流水、逐笔退回与余额（`/rightsholders/{code}/ledger`）；
  - 付款与出单/其他付款并发时，以「流水号事务咨询锁 + 权利人行锁（出单侧按编号升序加锁）」
    保证**校验与入账一致、绝不超付、无死锁**。
- **付款额度预留指令（两阶段付款）**：
  - 财务可凭 **权利人编号 + 唯一指令号 + 正整数分额** 预留付款额度；
    仅当「**当前余额 − 全部待确认预留**」的**可用额**仍为正且足额才创建 `pending` 指令，
    返回指令状态与可用额；同指令号同内容重试**幂等返回原指令**，**异内容拒绝**（409）；
  - 待确认指令可 **取消并释放额度**（重复取消幂等；已确认不可取消）；
  - **确认**时提交外部付款流水号，**原子登记付款并结束预留**，返回付款记录及余额；
    同流水号确认重试**幂等**，**异流水号或流水号已被其他付款占用**拒绝；
  - **负额追补防护**：确认时除本指令外还须为**更早创建的待确认指令**留足额度
    （`余额 ≥ 本笔金额 + 更早 pending 合计`），否则拒绝且**指令保持 pending 不变**；
  - 原 `POST /payments` 的入参、响应与超额拒绝约定**保持兼容**
    （错误码、`detail.balance` 不变，另附 `pending_reserved`、`available`），
    但**新付款须避开待确认预留**（只允许在正可用额内入账）；**退回继续加回余额**；
  - 权利人对账单新增 **待确认预留合计 `reserved_total`、可用额 `available`
    与全部指令及状态 `payment_instructions[]`**；单据、付款、指令历史**不可改写**
    （指令仅允许 pending → confirmed/cancelled 一次迁移，触发器强制）。
- **银行退票（付款退回）登记（财务退票）**：
  - 财务收到银行退票时，凭 **原付款流水号、退回流水号、正整数分额** 登记退回，
    返回退回记录及登记后余额；**原付款和单据保持不可变**，退回只追加记录；
  - 同一退回流水号 **同内容重试幂等返回原记录**（`status=duplicate`，HTTP 200），
    **异内容**（原付款流水号或金额不同）返回 **409 `refund_ref_conflict`**；
    原付款不存在 **404 `payment_not_found`**、金额非正整数 **422**、
    累计退回超过原付款金额 **409 `refund_exceeds_payment`**，拒绝时**不留任何退回**；
  - 权利人余额 = **已出具单据正负累计 − 付款累计 + 退回累计**（可为负）；
    **后续付款仍只允许在正余额内登记**；退回记录一经登记不可修改/删除（触发器强制），
    并有「累计退回不得超过原付款金额」触发器兜底；
  - 并发协议：「退回流水号事务咨询锁 → **原付款行锁**（同付款多笔退回串行，
    多个退回绝不合计超额）→ 权利人行锁（与付款/出单互斥）」，
    退回、付款、出单并发时**余额校验一致、无死锁**；
  - 对账单**保留付款总额**，另列 **退回总额、逐笔退回（携带原付款流水号）及余额**。
- **银行回单批次导入与付款核验（财务对账）**：
  - 按批次导入回单：批次编号 + 回单行（回单编号、外部付款流水号、权利人编号、正整数分额）；
    批次同内容重试 **幂等返回原批次**（`status=duplicate`，HTTP 200）；
    批次号或回单编号 **异内容复用**、批内重复流水号、无效字段均 **整批拒绝**，
    不留任何回单；
  - 跨批次同一付款流水号 **仅首次成功导入的回单参与核验**，其余标为 **重复回单**；
  - 有效回单 **仍按原付款字段**（流水号、权利人、金额）对照已登记付款，区分
    **匹配 / 付款未登记 / 权利人不符 / 金额不符**，两项均不符 **同时列出**
    （退票不改变匹配/不符判定）；导入时 **冻结逐行结论与该付款累计退回额**（第 1 版）；
  - 付款迟到或银行退票后可 **复核**：**原核验结论或冻结退回额变化**才生成 **新版本**，
    都不变不新增版本；**旧版本结论与冻结值不改写、始终可查**；
  - 每版输出 **状态计数、全部逐行冻结结论（含匹配行的累计退回额）及差异行**；
    付款/退回与复核并发时，一版内全部结论来自 **同一已提交状态**（单语句快照）；
    核验 **不改写付款、退回、余额或单据**。

## 一键启动

要求 Docker 20.10+（带 Compose v2）。

```bash
docker compose up --build -d
```

首次启动会自动：

1. 启动 PostgreSQL 16 并建立数据卷；
2. 等待数据库健康检查通过；
3. 构建并启动 API 容器，自动执行建表脚本（约束、触发器，无需额外扩展）。

查看日志：

```bash
docker compose logs -f api
```

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据库卷
```

## 地址与端口

| 服务 | 默认地址 |
| --- | --- |
| API 基地址 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| Swagger UI（交互式文档） | http://localhost:8000/docs |
| OpenAPI JSON | http://localhost:8000/openapi.json |
| PostgreSQL | localhost:5432 |

## 配置（可选）

均通过环境变量覆盖，docker-compose 已内置默认值；可在项目根目录放 `.env`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PORT` | `8000` | API 映射到宿主机的端口 |
| `POSTGRES_PORT` | `5432` | 数据库映射到宿主机的端口 |
| `POSTGRES_USER` | `royalty` | 数据库用户 |
| `POSTGRES_PASSWORD` | `royalty` | 数据库密码 |
| `POSTGRES_DB` | `royalty` | 数据库名 |
| `DATABASE_URL`（仅 API 容器） | `postgresql://royalty:royalty@db:5432/royalty` | 数据库连接串 |
| `AMENDMENT_TOKEN_SECRET`（仅 API 容器） | 内置默认值 | 协议修订预览令牌的 HMAC 签名密钥；默认值可单机直接运行，**多实例/生产请覆盖为同一密钥**（令牌仅防篡改、不承担保密，且不含付款等无关数据） |

本地裸机运行（不使用 Docker）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 让 DATABASE_URL 指向本地 PG（无需安装任何 contrib 扩展）
export DATABASE_URL="postgresql://royalty:royalty@localhost:5432/royalty"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 金额一律使用 **非负整数、最小货币单位**（如"分"），避免浮点误差；比例以字符串小数传输（如 `"62.5"`），最多 4 位小数（0.0001%）。
> 所有时间必须带时区（如 `2026-02-10T20:00:00+08:00`），内部统一按 UTC 比较；协议区间为半开 `[from, to)`，相邻区间允许，未填 `valid_to` 表示长期有效。

## HTTP API 一览

### 登记

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/works` | 登记作品（同编号幂等，标题冲突 409） |
| GET | `/works` | 作品列表 |
| POST | `/rightsholders` | 登记权利人（建协议时也会自动建档） |
| GET | `/rightsholders` | 权利人列表 |
| POST | `/agreements` | 登记分成协议（区间重叠 409；比例合计≠100% 422） |
| GET | `/agreements?work_code=` | 协议列表（可按作品过滤） |
| GET | `/agreements/{id}` | 协议详情（含现行版本号 `version`） |
| POST | `/agreements/{id}/amendments/preview` | 预览协议修订（新区间+完整比例）：受影响播放待处理变化、已出单期间各权利人应付差额；不改账，返回预览令牌 |
| POST | `/agreements/{id}/amendments/confirm` | 凭预览令牌确认修订：追加新版本（编号稳定、旧版可追溯）；过期/篡改/预览后状态变化 409 |
| GET | `/agreements/{id}/versions` | 协议全部版本（标注现行版本、旧版被取代时间） |
| GET | `/agreements/{id}/versions/{version}` | 指定版本详情（不存在 404 `agreement_version_not_found`） |

### 播放记录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plays/import` | 导入/更正单条；幂等返回 `status=duplicate`，异内容 409，分叉 409 |
| POST | `/plays/bulk-import` | 批量导入（整批一个事务，任一失败整批拒绝） |
| GET | `/plays/{import_id}/chain` | 更正链（从原始记录到链尾，标注 `chain_tail`） |

### 结算单据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/statements/issue` | 按期间出具；首次初次单，其后差额追补单（无变化不出新单）；同步冻结逐笔归因 |
| GET | `/statements/preview?start=&end=` | 不出具，试算当前应付、待处理、潜在差额 |
| GET | `/statements?start=&end=` | 列出该期间全部单据及明细（含 `traceable` 可追溯标记） |
| GET | `/statements/{batch_id}/attributions` | 按单据分页查询逐笔归因记录与逐权利人合计（可按 `holder_code` 过滤） |

### 付款核对与银行退票

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/payments` | 登记已完成付款；新登记 201，同流水号同内容重试 200 返回原记录；新付款避开待确认预留 |
| POST | `/refunds` | 登记银行退票（原付款流水号 + 退回流水号 + 正整数分额）；新登记 201，同退回流水号同内容重试 200 |
| GET | `/rightsholders/{holder_code}/ledger` | 按权利人查询单据增减、付款流水、逐笔退回、**待确认预留与可用额**及当前余额（不存在 404） |

### 付款额度预留指令（两阶段付款）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/payment-instructions` | 预留额度（权利人 + 唯一指令号 + 正整数分额）；新建 201，同号同内容重试 200，异内容 409，超可用额 409 |
| POST | `/payment-instructions/{instruction_ref}/cancel` | 取消待确认指令并释放额度；200（`status=cancelled`），重复取消 200（`duplicate`），已确认 409 |
| POST | `/payment-instructions/{instruction_ref}/confirm` | 提交外部付款流水号确认：原子登记付款并结束预留；新登记 201，同流水号重试 200 |
| GET | `/payment-instructions/{instruction_ref}` | 查询单个指令状态与当时余额/可用额；不存在 404 `instruction_not_found` |

### 银行回单核验

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/receipts/import` | 按批次导入回单并冻结第 1 版核验结论；同内容重试 200 返回原批次；异内容复用 409；批内重复/无效字段 422 整批拒绝 |
| POST | `/receipts/{batch_no}/reverify` | 按当前已登记付款复核；结论变化生成新版本（201），不变不新增（200 返回最新版） |
| GET | `/receipts/{batch_no}` | 批次原始回单行（含重复回单标记）与最新版本号 |
| GET | `/receipts/{batch_no}/versions` | 全部核验版本及各版状态计数（旧版本始终可查） |
| GET | `/receipts/{batch_no}/versions/{version}` | 指定版本的状态计数及差异行（结论已冻结） |

`POST /payments` 请求体：

```json
{ "holder_code": "RS-A", "external_ref": "BANK-20260301-0001", "amount": 19 }
```

- `amount` 为正整数、最小货币单位（分）；`external_ref` 为外部付款流水号，全局唯一。
- 成功响应（含登记后余额）：

```json
{
  "payment_id": 1,
  "holder_code": "RS-A",
  "external_ref": "BANK-20260301-0001",
  "amount": 19,
  "registered_at": "2026-03-01T10:00:00Z",
  "status": "created",
  "balance_after": 0
}
```

- 幂等重试时 `status="duplicate"`、HTTP 200，`payment_id` 与原记录相同，
  `balance_after` 为**当前**余额；不产生第二笔付款。
- 错误：同流水号异内容 → 409 `payment_ref_conflict`（响应 `detail` 回显原权利人/金额）；
  余额不为正或超额 → 409 `payment_exceeds_balance`（`detail.balance` 为当时余额）；
  权利人不存在 → 404 `holder_not_found`；金额为 0、负数或非整数 → 422。
  上述拒绝均**不写入**任何付款。

`GET /rightsholders/{holder_code}/ledger` 响应要点：

- `billed_total`：该权利人全部期间已出具单据明细（初次单与正负追补单）有符号累计；
- `paid_total`：已登记付款累计（**保留付款总额，不因退票而冲减**）；
  `refunded_total`：已登记银行退回累计；
  `balance = billed_total − paid_total + refunded_total`（可为负）；
- `statements[]`：每张涉及该权利人的单据（`batch_id`、`kind`、期间、`amount` 带正负号、`issued_at`），
  按出具次序；`payments[]`：付款流水（`payment_id`、`external_ref`、`amount`、`registered_at`）；
- `refunds[]`：**逐笔银行退回**（`refund_id`、`refund_ref`、`payment_id`、
  `payment_ref` 原付款流水号、`amount`、`registered_at`），按登记次序；
- 试算与待处理播放不在内；负余额表示"历史付款暂未被后续正额抵扣完"，仍可核对。

### 银行退票登记

`POST /refunds` 请求体：

```json
{
  "payment_ref": "BANK-20260301-0001",
  "refund_ref": "BANK-RET-20260305-0001",
  "amount": 4
}
```

- `payment_ref` 为**原付款流水号**（必须指向一笔已登记付款）；
  `refund_ref` 为**退回流水号**（调用方提供，全局唯一）；
  `amount` 为正整数、最小货币单位（分）。
- 成功响应（201，`status=created`，含退回记录与登记后余额）：

```json
{
  "refund_id": 1,
  "payment_id": 1,
  "payment_ref": "BANK-20260301-0001",
  "refund_ref": "BANK-RET-20260305-0001",
  "amount": 4,
  "registered_at": "2026-03-05T10:00:00Z",
  "status": "created",
  "balance_after": 4
}
```

- 幂等重试时 `status="duplicate"`、HTTP 200，`refund_id` 与原记录相同，
  `balance_after` 为**当前**余额；不产生第二笔退回，**原付款与单据不变**。
- 错误：原付款不存在 → 404 `payment_not_found`；同退回流水号异内容 →
  409 `refund_ref_conflict`（`detail` 回显原原付款流水号/金额）；
  累计退回（含本笔）超过原付款金额 → 409 `refund_exceeds_payment`
  （`detail` 含 `payment_amount`、`already_refunded`、`requested_amount`）；
  金额为 0、负数或非整数 → 422。上述拒绝均**不写入**任何退回。
- 余额口径随之变为 **单据累计 − 付款累计 + 退回累计**（可为负）；
  退回加回的余额可供后续付款，但**付款仍只允许在当时的正余额内登记**。
- 并发保证：同一退回流水号的并发请求串行（恰一笔登记，其余幂等）；
  同一原付款的多笔并发退回经**原付款行锁**串行，合计绝不超过原付款金额；
  退回与付款/出单经**权利人行锁**互斥，余额校验所见即所入账，加锁次序统一、无死锁。

期间同样是半开区间 `[start, end)`，与协议区间语义一致。

#### 付款额度预留指令（两阶段付款）

**预留** `POST /payment-instructions` 请求体：

```json
{ "holder_code": "RS-A", "instruction_ref": "PI-20260301-0001", "amount": 40 }
```

- `instruction_ref` 为调用方提供的**唯一指令号**（全局唯一）；`amount` 为正整数分额。
- 仅当 **可用额 = 当前余额 − 全部待确认预留（status=pending）** 为正且不小于金额时才创建；
  成功响应（201，`status=created`）返回指令状态、当前余额、待确认预留合计与可用额：

```json
{
  "instruction_id": 1,
  "instruction_ref": "PI-20260301-0001",
  "holder_code": "RS-A",
  "amount": 40,
  "instruction_status": "pending",
  "payment_id": null,
  "payment_ref": null,
  "created_at": "2026-03-01T10:00:00Z",
  "confirmed_at": null,
  "cancelled_at": null,
  "status": "created",
  "balance": 60,
  "reserved_total": 40,
  "available": 20
}
```

- 同指令号同内容重试：200、`status=duplicate`、`instruction_id` 不变，余额/可用额为**当时值**；
- 同号异内容（权利人或金额不同）：409 `instruction_ref_conflict`
  （`detail` 回显原权利人/金额/状态）；可用额不足：409 `instruction_exceeds_balance`
  （`detail` 含 `balance`、`pending_reserved`、`available`），拒绝不留任何指令；
  权利人不存在 404 `holder_not_found`；金额非正整数 422。

**取消** `POST /payment-instructions/{instruction_ref}/cancel`（无请求体）：

- 仅 `pending` 可取消（`pending → cancelled`，额度立即释放），200、`status=cancelled`；
- 对已取消指令重复取消幂等：200、`status=duplicate`，指令与付款均不变；
  对已确认指令取消：409 `instruction_status_conflict`；指令不存在 404 `instruction_not_found`。

**确认** `POST /payment-instructions/{instruction_ref}/confirm` 请求体：

```json
{ "external_ref": "BANK-20260301-0001" }
```

- 在**同一事务内原子**完成「登记 `payments` 付款 + 指令 `pending → confirmed` 绑定付款」，
  返回付款记录与登记后余额（201，`status=created`）；响应同时给出与 `POST /payments`
  一致的付款字段（`payment_id`/`external_ref`/`registered_at`/`balance_after`）
  与完整 `payment` 对象、`available_after`：

```json
{
  "instruction_id": 1,
  "instruction_ref": "PI-20260301-0001",
  "holder_code": "RS-A",
  "amount": 40,
  "instruction_status": "confirmed",
  "payment": {
    "payment_id": 1, "holder_code": "RS-A",
    "external_ref": "BANK-20260301-0001", "amount": 40,
    "registered_at": "2026-03-01T11:00:00Z"
  },
  "confirmed_at": "2026-03-01T11:00:00Z",
  "payment_id": 1,
  "external_ref": "BANK-20260301-0001",
  "registered_at": "2026-03-01T11:00:00Z",
  "balance_after": 20,
  "available_after": 20,
  "status": "created"
}
```

- 同流水号确认重试：200、`status=duplicate`，返回原付款记录，不产生第二笔付款；
- 已确认指令换流水号：409 `instruction_already_confirmed`（`detail` 回显已绑定流水号）；
  流水号已被**其他付款**（直接付款接口或其他指令）占用：409 `payment_ref_occupied`
  （`detail` 回显占用付款的权利人/金额），**指令保持 pending 不变**；
  取消后的指令确认：409 `instruction_status_conflict`；指令不存在 404。
- **负额追补防护**：确认时要求 `当前余额 ≥ 本指令金额 + 更早创建的待确认指令合计`
  （"更早创建"按指令 id 升序），否则 409 `instruction_exceeds_balance`
  （`detail` 含 `balance`、`earlier_pending_reserved`、`required_amount`），
  **拒绝且指令保持 pending、不留任何付款**；等待正额恢复或更早指令先确认/取消后可再确认。

**对账单新增字段**（`GET /rightsholders/{holder_code}/ledger`）：

- `reserved_total`：全部待确认（pending）指令分额合计；
  `available = balance − reserved_total`（可为负：负额追补冲减余额后）；
- `payment_instructions[]`：全部预留指令，按创建次序，每条含 `instruction_ref`、
  `amount`、`status`（`pending`/`confirmed`/`cancelled`）、绑定的
  `payment_id`/`payment_ref` 及 `confirmed_at`/`cancelled_at`；
  历史单据、付款与指令状态均不可改写。

**并发协议**：指令号事务咨询锁 → 外部付款流水号事务咨询锁（仅确认，与付款登记同命名空间）
→ 权利人行锁；与付款登记、银行退票、出单共用统一加锁次序，
预留/取消/确认、直接付款与负额追补并发时余额判定一致、绝不超付、无死锁。

#### 银行回单批次导入与核验

`POST /receipts/import` 请求体：

```json
{
  "batch_no": "RC-20260301-01",
  "receipts": [
    {"receipt_no": "RC-1", "external_ref": "BANK-20260301-0001", "holder_code": "RS-A", "amount": 19},
    {"receipt_no": "RC-2", "external_ref": "BANK-20260302-0002", "holder_code": "RS-B", "amount": 9}
  ]
}
```

- 回单行字段：`receipt_no`（回单编号）、`external_ref`（外部付款流水号）、
  `holder_code`（权利人编号，回单所载，不要求已建档）、`amount`（正整数分额）；
  空编号、非正整数金额等无效字段 **422 整批拒绝**，不留任何回单。
- 导入响应（201，`status=created`）含原始回单行（`is_duplicate` 标记）与
  导入时冻结的第 1 版核验结论 `verification`。每版除 `differences[]`（差异行）外
  另含 `lines[]`（**全部回单行的冻结结论，含 matched 行**），每行带
  `payment_refunded_total`（核验时冻结的**原付款累计退回额**；
  付款未登记/重复回单时为 `null`）：

```json
{
  "batch_no": "RC-20260301-01",
  "status": "created",
  "created_at": "2026-03-02T02:00:00Z",
  "receipts": [
    {"receipt_no": "RC-1", "external_ref": "BANK-20260301-0001",
     "holder_code": "RS-A", "amount": 19, "is_duplicate": false}
  ],
  "verification": {
    "batch_no": "RC-20260301-01",
    "version": 1,
    "created_at": "2026-03-02T02:00:00Z",
    "counts": {"matched": 1, "payment_not_registered": 0,
               "mismatched": 1, "duplicate": 0, "total": 2},
    "differences": [
      {"receipt_no": "RC-2", "external_ref": "BANK-20260302-0002",
       "status": "mismatched", "differences": ["holder_mismatch"],
       "receipt_holder_code": "RS-B", "receipt_amount": 9,
       "payment_holder_code": "RS-C", "payment_amount": 9,
       "payment_refunded_total": 0}
    ],
    "lines": [
      {"receipt_no": "RC-1", "external_ref": "BANK-20260301-0001",
       "status": "matched", "differences": [],
       "receipt_holder_code": "RS-A", "receipt_amount": 19,
       "payment_holder_code": "RS-A", "payment_amount": 19,
       "payment_refunded_total": 0},
      {"receipt_no": "RC-2", "external_ref": "BANK-20260302-0002",
       "status": "mismatched", "differences": ["holder_mismatch"],
       "receipt_holder_code": "RS-B", "receipt_amount": 9,
       "payment_holder_code": "RS-C", "payment_amount": 9,
       "payment_refunded_total": 0}
    ]
  }
}
```

- 逐行结论 `status`：`matched`（流水号命中已登记付款且权利人、金额均一致）、
  `payment_not_registered`（付款未登记）、`mismatched`（`differences` 列出
  `holder_mismatch` / `amount_mismatch`，**两项均不符同时列出**）、
  `duplicate`（跨批次同一流水号的重复回单，不参与核验）；
  差异行附回单与付款双方快照（`receipt_*` / `payment_*`）。
- **退票联动**：有效回单**仍按原付款字段**判定匹配/不符，银行退回不改变结论；
  导入与复核时另冻结该付款当时的 **累计退回额** `payment_refunded_total`，
  因此付款即使已全额退回，回单结论仍可为 `matched`。
- 幂等与拒绝：批次同内容重试 **200 返回原批次**（`status=duplicate`，含第 1 版结论）；
  批次号异内容复用 **409 `receipt_batch_conflict`**；
  回单编号异内容复用 **409 `receipt_no_conflict`**（`detail` 回显已导入内容）；
  批内重复流水号 / 重复回单编号 **422**；上述拒绝均 **整批不留痕**。
- 跨批次同一流水号：仅 **首次成功导入** 的回单参与核验；后续批次中同号回单
  标为 `is_duplicate=true`、结论 `duplicate`（同内容复用回单编号亦按此处理）。

`POST /receipts/{batch_no}/reverify`（付款迟到或银行退票后复核）：

- 按 **当前已登记付款与当前累计退回额** 重算逐行结论：**原核验结论（状态/不符项/
  付款快照）或冻结的累计退回额任一变化**才冻结为新版本
  （201，`status=created`）；都不变不新增版本
  （200，`status=unchanged`，返回现有最新版）；
- 付款/退回与复核并发时，一版内全部结论来自 **同一已提交状态**（单语句快照）；
- 复核只新增核验版本，**绝不改写付款、退回、余额或单据**，旧版本冻结值不改写；
  批次不存在 404。

查询（旧版本与原始回单始终可查）：

- `GET /receipts/{batch_no}`：原始回单行（含 `is_duplicate`）与 `latest_version`；
- `GET /receipts/{batch_no}/versions`：全部版本及各版状态计数；
- `GET /receipts/{batch_no}/versions/{version}`：指定版本的状态计数及差异行；
  版本不存在 404 `receipt_version_not_found`。

#### 逐笔归因查询参数

| 参数 | 位置 | 说明 |
| --- | --- | --- |
| `batch_id` | 路径 | 单据编号；不存在返回 `404 statement_not_found` |
| `holder_code` | query | 可选，只看某位权利人；权利人不存在返回 `404 holder_not_found` |
| `page` | query | 页码，从 1 开始（默认 1；<1 返回 422） |
| `page_size` | query | 每页条数，默认 50，范围 1–500（越界 422） |

响应要点：

- `records[]`：按"权利人 → 原始编号 → 性质（removed 先于 added）"稳定排序，字段含
  `root_import_id`（更正链原始编号）、`tail_import_id`（链尾）、`work_code`、
  `occurred_at`、`agreement_id`、`tail_amount`、`share_percent`、`amount`；
- `change_type`：`added`=新增（非负）、`removed`=撤销（负额）、`legacy`=不可追溯旧账差额（无播放字段）；
- `holder_totals[]`：每位权利人 `attributed_total`（归因合计）、
  `statement_line_amount`（原单据明细行金额）与 `reconciles`（两者是否相等）；
- `traceable=false`：存量不可追溯单据，`records` 为空、`reconciles` 恒为 false。

#### 协议修订（按协议编号提交新版本）

运营可按**协议编号**及其**现行版本**，提交带时区的**新适用区间**与**完整权利人比例**
（不是增量）。分两步：先 **preview**（预览，不改账）、再 **confirm**（确认，追加版本）。

**预览** `POST /agreements/{agreement_id}/amendments/preview` 请求体与登记协议一致
（`work_code` 由协议编号确定，无需也不能在体中给出）：

```json
{
  "valid_from": "2026-05-01T00:00:00+08:00",
  "valid_to": "2027-01-01T00:00:00+08:00",
  "note": "2026 年 5 月起改为两方各半",
  "shares": [
    {"holder_code": "RS-A", "percent": "50"},
    {"holder_code": "RS-B", "percent": "50"}
  ]
}
```

- `valid_to` 可省略（长期有效）；区间为半开 `[valid_from, valid_to)`，
  `valid_to <= valid_from` 或时间**不带时区** → **422**；
- `shares` 为**完整比例**，须合计恰好 **100%**、权利人编号不得重复，否则 **422**；
  比例中的权利人**必须已建档**（预览无任何写副作用，不自动建档）→ 否则
  **422 `holder_not_found`**（`detail.missing_holder_codes` 列出缺失编号）；
- 新区间与该作品**其他现行协议**（目标协议自身除外）重叠 →
  **409 `agreement_overlap`**（`detail.other_agreement_id`）；协议不存在 → 404。

预览响应（HTTP 200，`status="preview"`）**不修改任何数据**，要点：

- `agreement`：`id`（协议编号，稳定）、`current_version`（所见版本）、`proposed_version`；
- `affected_plays[]`：受影响的**更正链链尾**播放，每条给出
  `import_id`/`root_import_id`（更正链原始编号）/`occurred_at`/`amount`，以及
  `change_type`：
  - `become_allocated`：原无匹配（待处理），修订后**获得**本协议匹配；
  - `become_pending`：原本由本协议覆盖，修订后**失去匹配转待处理**；
  - `reallocated`：前后均有匹配（区间/比例变化，或区间让出给/延伸自其他现行协议）；
  - 并附 `old`/`new` 两侧的 `agreement_id`、`version`、`shares` 与逐笔 `allocation`；
- `pending_changes`：`to_pending[]`（确认后转待处理）、`from_pending[]`（确认后离开待处理）；
- `affected_periods[]`：每个覆盖了受影响播放的**已出单期间**：
  - `payable_delta_work[]`：仅因**本作品本次修订**导致的该期间各权利人**应付差额**（可正可负）；
  - `currently_pending[]`：该期间当前待处理清单中、确认后将获得匹配的播放；
  - `pending_after[]`：确认并按现行版本重算后该期间的**完整待处理清单预测**；
  - `projected_supplement[]`：确认后**再出具**时将经原追补机制生成的各权利人差额
    （= 修订后全部应付投影 − 该期间已入账累计；为 0 不列出，差额**可正可负**）；
- `preview_token`：一次性预览令牌（HMAC 签名，内含**所见版本**、**拟提交内容**与
  **状态指纹**）。

**确认** `POST /agreements/{agreement_id}/amendments/confirm` 请求体 = 预览体 +
`preview_token`：

```json
{
  "valid_from": "2026-05-01T00:00:00+08:00",
  "valid_to": "2027-01-01T00:00:00+08:00",
  "note": "2026 年 5 月起改为两方各半",
  "shares": [
    {"holder_code": "RS-A", "percent": "50"},
    {"holder_code": "RS-B", "percent": "50"}
  ],
  "preview_token": "<上一步返回的令牌>"
}
```

- 成功 **201**、`status="amended"`：追加新版本，响应结构与预览一致
  （`agreement.previous_version/current_version` 分别为旧/新版本号）；
- 协议不存在 → **404 `agreement_not_found`**；
- 令牌缺失/为空/格式错误/签名不符/属于其他协议/与**预览时的区间或比例不一致** →
  **409 `amendment_preview_invalid`**；
- 令牌所基于的现行版本**已过期**（他人已先确认一次修订）→
  **409 `agreement_version_conflict`**（`detail.previewed_version/current_version`），
  同一令牌重放亦报此错；
- 预览后该作品的**现行协议、相关播放更正链或相关期间单据**发生变化 →
  **409 `amendment_preview_stale`**，**拒绝且不修改任何数据**；请重新预览后再确认；
- 确认时在同一事务内**重新校验**区间/比例/重叠全部规则（任一不满足同样拒绝，不留半成品）。

确认后的结算口径：

- **协议编号（`id`）保持稳定、版本号 `current_version` 递增**；版本只追加，
  旧版的区间、比例、note 一经写入**不可改写或删除**（数据库触发器强制），
  旧版被取代时仅允许置位一次 `superseded_at`；协议主表除 `current_version`
  推进与现行镜像列同步外不可改、不可删；
- 播放**自确认后按发生时刻使用现行版本**重算（单语句快照，与修订并发不会读到撕裂状态）：
  失去匹配的播放**转待处理**，新区间延伸覆盖的播放离开待处理并参与分摊；
- **确认只改协议版本本身，不自动重出任何单据**；既有单据、逐笔归因、
  付款/预留指令/银行退回历史**一律不改写**。已出单期间的各权利人正负差额在
  再调用 `POST /statements/issue` 时**经原追补机制**体现（逐笔 `removed`/`added`
  归因照常冻结，负额逐笔保留不轧差），试算口径见 `GET /statements/preview`。

版本追溯（旧版始终可查）：

- `GET /agreements/{id}/versions`：`current_version` 与 `versions[]`
  （每版 `version`、`is_current`、区间、`note`、`created_at`、`superseded_at`、`shares`）；
- `GET /agreements/{id}/versions/{version}`：指定版本；不存在 →
  **404 `agreement_version_not_found`**。

兼容性：`POST /agreements`、`GET /agreements`、`GET /agreements/{id}` 的**入参不变**，
已有响应字段与错误约定不变，仅在响应中**新增**现行版本号字段 `version`。

## 调用示例

一键演示脚本（请先启动服务），覆盖幂等、异内容拒绝、区间重叠、更正链/分叉拒绝、
后补协议、逐笔分摊、初次单与正负追补单、待处理清单等全部场景：

```bash
bash examples/demo.sh
```

两阶段付款（额度预留/取消/确认、幂等与冲突、负额追补防护、对账单可见性）另有一套
独立演示脚本：

```bash
bash examples/demo_instructions.sh
```

协议修订（预览受影响播放与已出单期间差额、确认追加版本、过期/篡改/状态变化拒绝、
旧版可追溯、失去匹配转待处理、再出具经追补机制体现正负差额）的独立演示脚本：

```bash
bash examples/demo_amendments.sh
```

手工调用（以下为关键几步）：

```bash
# 1. 登记作品
curl -sS -X POST http://localhost:8000/works \
  -H 'Content-Type: application/json' \
  -d '{"code":"W-LIVE-001","title":"仲夏之夜现场录音"}'

# 2. 登记分成协议（合计 100%）
curl -sS -X POST http://localhost:8000/agreements \
  -H 'Content-Type: application/json' \
  -d '{
    "work_code":"W-LIVE-001",
    "valid_from":"2026-01-01T00:00:00+08:00",
    "valid_to":"2027-01-01T00:00:00+08:00",
    "shares":[
      {"holder_code":"RS-A","percent":"62.5"},
      {"holder_code":"RS-B","percent":"30"},
      {"holder_code":"RS-C","percent":"7.5"}
    ]
  }'

# 3. 导入播放（100 分）
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":100}'

# 4. 更正：新编号 + corrects_id 引用原编号
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R1","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":80,
       "corrects_id":"PLAY-1001"}'

# 5. 出具 2026 年 2 月单据（80 分 => 50/24/6）
curl -sS -X POST http://localhost:8000/statements/issue \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}'

# 6. 再次更正为 30 分后重新出具 => 追补单差额 -31/-15/-4
curl -sS -X POST http://localhost:8000/plays/import \
  -H 'Content-Type: application/json' \
  -d '{"import_id":"PLAY-1001-R2","work_code":"W-LIVE-001",
       "occurred_at":"2026-02-10T20:00:00+08:00","amount":30,
       "corrects_id":"PLAY-1001-R1"}'
curl -sS -X POST http://localhost:8000/statements/issue \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}'
```

协议修订（按上面协议编号 1 提交 v2；先预览后确认，再对已出单期间追补）：

```bash
# 7a. 预览：区间收窄到 [2026-05-01, 2027-01-01)、两方各半；返回受影响播放、
#     已出单期间各权利人应付差额与 preview_token（不改账）
curl -sS -X POST http://localhost:8000/agreements/1/amendments/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "valid_from":"2026-05-01T00:00:00+08:00",
    "valid_to":"2027-01-01T00:00:00+08:00",
    "shares":[{"holder_code":"RS-A","percent":"50"},
              {"holder_code":"RS-B","percent":"50"}]
  }' | python3 -m json.tool

# 7b. 确认（把上一步 preview_token 原样带回；区间/比例须与预览一致）
curl -sS -X POST http://localhost:8000/agreements/1/amendments/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "valid_from":"2026-05-01T00:00:00+08:00",
    "valid_to":"2027-01-01T00:00:00+08:00",
    "shares":[{"holder_code":"RS-A","percent":"50"},
              {"holder_code":"RS-B","percent":"50"}],
    "preview_token":"<preview_token>"
  }' | python3 -m json.tool

# 7c. 协议编号仍为 1、版本升为 2；旧版可追溯
curl -sS http://localhost:8000/agreements/1/versions | python3 -m json.tool

# 7d. 2 月播放失去协议匹配：再出具 => 负额追补（经原追补机制），播放转待处理；
#     历史初次单与逐笔归因不改写
curl -sS -X POST http://localhost:8000/statements/issue \
  -H 'Content-Type: application/json' \
  -d '{"start":"2026-02-01T00:00:00+08:00","end":"2026-03-01T00:00:00+08:00"}' \
  | python3 -m json.tool
```

出具后，财务按单据核对每笔金额来自哪些播放（归因记录分页查询）：

```bash
# 初次单 batch_id=1：查看权利人 RS-A 的逐笔归因（第 1 页，每页 20 条）
curl -sS "http://localhost:8000/statements/1/attributions?holder_code=RS-A&page=1&page_size=20"

# 追补单 batch_id=2：撤销(removed，负数)与新增(added)分列、负额保留；
# holder_totals 中 attributed_total 恒等于该单原明细行金额（reconciles=true）
curl -sS "http://localhost:8000/statements/2/attributions" | python3 -m json.tool

# 不存在的单据明确报错
curl -sS -i "http://localhost:8000/statements/999999/attributions"   # 404 statement_not_found
```

财务按权利人核对单据与实付（第 5、6 步后 RS-A 单据累计 19 分）：

```bash
# 登记付款 19 分（不超过正余额）=> 201，balance_after=0
curl -sS -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":19}'

# 同流水号同内容重试 => 200 status=duplicate，返回原记录，不重复入账
curl -sS -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":19}'

# 超额（余额已为 0）=> 409 payment_exceeds_balance，不留付款
curl -sS -i -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0002","amount":1}'

# 同流水号异内容 => 409 payment_ref_conflict；权利人不存在 => 404；金额非正整数 => 422
curl -sS -i -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-20260301-0001","amount":10}'
curl -sS -i -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"NOBODY","external_ref":"X","amount":1}'

# 对账单：单据增减（含负追补）、付款流水、billed/paid/balance
curl -sS "http://localhost:8000/rightsholders/RS-A/ledger" | python3 -m json.tool
```

财务按批次导入银行回单，核验已登记付款（承上：BANK-20260301-0001 已付 RS-A 19 分）：

```bash
# 1) 再登记两笔付款：RS-B 9 分（回单将错记权利人）、RS-C 2 分（回单迟到后补登）
curl -sS -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-B","external_ref":"BANK-20260302-0002","amount":9}'

# 2) 导入批次 RC-20260301-01：匹配 / 权利人不符 / 付款未登记 各一行
curl -sS -X POST http://localhost:8000/receipts/import -H 'Content-Type: application/json' \
  -d '{
    "batch_no":"RC-20260301-01",
    "receipts":[
      {"receipt_no":"RC-1","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19},
      {"receipt_no":"RC-2","external_ref":"BANK-20260302-0002","holder_code":"RS-C","amount":9},
      {"receipt_no":"RC-3","external_ref":"BANK-20260303-0003","holder_code":"RS-C","amount":2}
    ]
  }' | python3 -m json.tool
# => 201，verification(version=1)：counts matched=1 / mismatched=1(RC-2 holder_mismatch)
#    / payment_not_registered=1(RC-3)

# 3) 同内容重试 => 200 status=duplicate 返回原批次；异内容复用批次号 => 409
curl -sS -i -X POST http://localhost:8000/receipts/import -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-01","receipts":[
      {"receipt_no":"RC-1","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19},
      {"receipt_no":"RC-2","external_ref":"BANK-20260302-0002","holder_code":"RS-C","amount":9},
      {"receipt_no":"RC-3","external_ref":"BANK-20260303-0003","holder_code":"RS-C","amount":2}]}' | head -1
curl -sS -i -X POST http://localhost:8000/receipts/import -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-01","receipts":[
      {"receipt_no":"RC-9","external_ref":"BANK-X","holder_code":"RS-A","amount":1}]}' | head -1

# 4) 付款迟到：补登 RS-C 2 分后复核 => 生成第 2 版（RC-3 转为 matched）
curl -sS -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-C","external_ref":"BANK-20260303-0003","amount":2}'
curl -sS -X POST http://localhost:8000/receipts/RC-20260301-01/reverify | python3 -m json.tool
# 结论不变再复核 => 200 status=unchanged，不新增版本
curl -sS -X POST http://localhost:8000/receipts/RC-20260301-01/reverify | python3 -m json.tool

# 5) 旧版本与原始回单始终可查
curl -sS "http://localhost:8000/receipts/RC-20260301-01/versions" | python3 -m json.tool
curl -sS "http://localhost:8000/receipts/RC-20260301-01/versions/1" | python3 -m json.tool
curl -sS "http://localhost:8000/receipts/RC-20260301-01" | python3 -m json.tool

# 6) 跨批次同一流水号：后续批次同号回单标为重复回单，不参与核验
curl -sS -X POST http://localhost:8000/receipts/import -H 'Content-Type: application/json' \
  -d '{"batch_no":"RC-20260301-02","receipts":[
      {"receipt_no":"RC-4","external_ref":"BANK-20260301-0001","holder_code":"RS-A","amount":19}
    ]}' | python3 -m json.tool
# => RC-4 is_duplicate=true，结论 status=duplicate
```

财务收到银行退票时，凭原付款流水号登记退回（承上：RS-B 的 BANK-20260302-0002 已付 9 分）：

```bash
# 1) 部分退回 4 分：原付款/单据不变，余额加回 4 => 201，balance_after=4
curl -sS -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' \
  -d '{"payment_ref":"BANK-20260302-0002",
       "refund_ref":"BANK-RET-20260305-0001","amount":4}' | python3 -m json.tool

# 2) 同退回流水号同内容重试 => 200 status=duplicate 返回原记录，不重复入账
curl -sS -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' \
  -d '{"payment_ref":"BANK-20260302-0002",
       "refund_ref":"BANK-RET-20260305-0001","amount":4}'

# 3) 异内容冲突 / 原付款不存在 / 金额非正整数
curl -sS -i -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' \
  -d '{"payment_ref":"BANK-20260302-0002",
       "refund_ref":"BANK-RET-20260305-0001","amount":3}' | head -1   # 409 refund_ref_conflict
curl -sS -i -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' \
  -d '{"payment_ref":"BANK-NOT-EXIST","refund_ref":"X","amount":1}' | head -1  # 404 payment_not_found
curl -sS -i -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' \
  -d '{"payment_ref":"BANK-20260302-0002","refund_ref":"X","amount":0}' | head -1  # 422

# 4) 累计退回超过原付款金额 => 409 refund_exceeds_payment，不留记录
#    （已退 4，再退 6 => 累计 10 > 9）
curl -sS -i -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' \
  -d '{"payment_ref":"BANK-20260302-0002",
       "refund_ref":"BANK-RET-20260305-0002","amount":6}' | head -1

# 5) 对账单：保留付款总额，另列退回总额、逐笔退回与余额（billed-paid+refunded）
curl -sS http://localhost:8000/rightsholders/RS-B/ledger | python3 -m json.tool

# 6) 复核回单批次：有效回单仍按原付款字段判定，冻结的累计退回额变化也产生新版本
curl -sS -X POST http://localhost:8000/receipts/RC-20260301-01/reverify \
  | python3 -m json.tool
```

两阶段付款：先预留额度、银行实付后凭流水号确认（完整脚本见
`examples/demo_instructions.sh`）：

```bash
# 1) 预留 40 分（权利人 RS-A + 唯一指令号）=> 201，available=余额-待确认预留
curl -sS -X POST http://localhost:8000/payment-instructions \
  -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","instruction_ref":"PI-20260301-0001","amount":40}' \
  | python3 -m json.tool

# 2) 同号同内容重试 => 200 status=duplicate；同号异内容 => 409 instruction_ref_conflict
curl -sS -X POST http://localhost:8000/payment-instructions \
  -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","instruction_ref":"PI-20260301-0001","amount":40}'
curl -sS -i -X POST http://localhost:8000/payment-instructions \
  -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","instruction_ref":"PI-20260301-0001","amount":10}' | head -1

# 3) 待确认期间，原付款接口新付款须避开预留：可用额不足 => 409 payment_exceeds_balance
#    （detail 另含 pending_reserved、available；错误码与 balance 字段保持兼容）
curl -sS -i -X POST http://localhost:8000/payments -H 'Content-Type: application/json' \
  -d '{"holder_code":"RS-A","external_ref":"BANK-DIRECT-1","amount":1}' | head -1

# 4) 取消第二笔预留并释放额度；重复取消幂等；已确认指令取消 => 409
curl -sS -X POST http://localhost:8000/payment-instructions/PI-20260301-0002/cancel
curl -sS -X POST http://localhost:8000/payment-instructions/PI-20260301-0002/cancel

# 5) 确认：提交外部付款流水号，原子登记付款并结束预留 => 201，返回付款记录与余额
curl -sS -X POST http://localhost:8000/payment-instructions/PI-20260301-0001/confirm \
  -H 'Content-Type: application/json' \
  -d '{"external_ref":"BANK-20260301-0001"}' | python3 -m json.tool

# 6) 同流水号确认重试 => 200 status=duplicate；异流水号 => 409 instruction_already_confirmed；
#    流水号被其他付款占用 => 409 payment_ref_occupied（指令保持 pending）；
#    负额追补导致不足以为更早的待确认指令留足 => 409 instruction_exceeds_balance（指令不变）
curl -sS -X POST http://localhost:8000/payment-instructions/PI-20260301-0001/confirm \
  -H 'Content-Type: application/json' -d '{"external_ref":"BANK-20260301-0001"}'

# 7) 单指令查询；对账单可见 reserved_total / available / payment_instructions[]
curl -sS http://localhost:8000/payment-instructions/PI-20260301-0001 | python3 -m json.tool
curl -sS http://localhost:8000/rightsholders/RS-A/ledger | python3 -m json.tool
```

## 分摊示例

金额 100，比例 62.5% / 30% / 7.5%：

- 精确值 62.5 / 30 / 7.5 → 先取整 62 / 30 / 7，余 1；
- 小数部分 `RS-A`、`RS-C` 同为 0.5（`RS-B` 为 0），按权利人编号升序补 1 给 `RS-A`；
- 结果 **63 / 30 / 7 = 100**。

金额 80：50 / 24 / 6（精确整除，无余数）；金额 30：18.75 / 9 / 2.25，
取整 18/9/2 余 1，小数部分最大的 `RS-A`（0.75）补 1 → **19/9/2**。每笔之和恒等于原金额。

## 测试

核心分摊算法的单元测试（无需数据库）：

```bash
python3 -m unittest discover -s tests -v
```

端到端集成测试（用 [`pgserver`](https://pypi.org/project/pgserver/) 自动拉起便携 PostgreSQL，
**无需 Docker / root**，覆盖登记、幂等、重叠、更正链、待处理、初次/追补单、并发出具等全链路；
逐笔归因证据（冻结/撤销负额/分页对账/不可变/存量不可追溯与 legacy 旧账/错误码）另有一套；
付款核对（余额口径/幂等与冲突/超额拒绝不留痕/负额追补不撤销与后续抵扣/
试算待处理不入账/付款与出单及其他付款并发不超付）再有一套；
付款额度预留指令（预留可用额校验/同号幂等与异内容拒绝/取消释放与重复取消幂等/
确认原子登记与流水号幂等/流水号被占用拒绝且指令不变/负额追补为更早 pending 留足、
拒绝后指令不变/原付款接口兼容且避开预留/退回加回/对账单预留与状态可见/
指令不可改写触发器/同号并发预留、预留与付款并发抢占、确认与负额追补并发）单独一套；
银行退票（登记与退回后余额/原付款单据不可变/幂等与冲突/累计超额拒绝不留痕/
同付款多笔并发退回不合计超额/退回与付款及出单并发余额一致无死锁/
对账单付款与退回总额分列）与回单核验（整批拒绝/幂等重试/异内容复用/跨批次重复回单/
冻结结论与累计退回额/迟到付款及退回复核版本化/并发复核与并发导入/
付款退回与复核并发快照一致/核验不改写账务）各有一套；
协议修订（版本化与旧版追溯/预览静态校验与影响试算/确认令牌与乐观并发
（内容不符/坏令牌/过期版本/预览后协议·更正链·单据变化 stale）/
拒绝不改数据/确认后按现行版本匹配、失去匹配转待处理/再出具经原追补机制正负差额/
历史单据·归因·付款退回不改写/版本与协议表触发器不可变/同作品并发修订仅一胜）
另有一套：

```bash
pip install pgserver httpx
PYTHONPATH=. python3 tests/integration_pgserver.py       # 原有全链路
PYTHONPATH=. python3 tests/integration_attribution.py    # 逐笔归因证据
PYTHONPATH=. python3 tests/integration_payments.py       # 付款登记与权利人对账
PYTHONPATH=. python3 tests/integration_instructions.py   # 付款额度预留指令（两阶段付款）
PYTHONPATH=. python3 tests/integration_refunds.py        # 银行退票登记与回单退回额冻结
PYTHONPATH=. python3 tests/integration_receipts.py       # 银行回单批次导入与付款核验
PYTHONPATH=. python3 tests/integration_amendments.py     # 协议修订（预览/确认/版本追溯/追补）
```

## 目录结构

```
app/
  main.py                 # FastAPI 入口、生命周期、异常映射
  config.py               # 环境配置
  db.py                   # 连接池与幂等建表脚本（全部约束在此，含协议版本表/触发器与存量回填）
  allocation.py           # 逐笔按比例分摊（最大余数法 + 编号次序）
  schemas.py              # Pydantic 模型与输入校验
  amendment_tokens.py     # 协议修订预览令牌（HMAC 签名 + 所见状态指纹，乐观并发）
  amendments_service.py   # 协议修订：预览影响试算/确认追加版本/版本追溯（每作品锁、单语句快照匹配）
  statements_service.py   # 期间匹配（协议现行版本）、重算、初次/追补出具、并发互斥、逐笔归因冻结与分页对账
  payments_service.py     # 付款登记（流水号锁/余额校验/幂等冲突）、可用额（避开待确认预留）、权利人对账单、出单侧权利人行锁
  instructions_service.py # 额度预留指令：预留/取消/确认（指令号锁+流水号锁+权利人行锁；负额追补为更早 pending 留足）
  refunds_service.py      # 银行退票登记（退回流水号锁 + 原付款行锁 + 权利人行锁，累计超额拒绝/幂等冲突）
  receipts_service.py     # 回单批次导入（幂等/整批拒绝/跨批次去重）、核验结论与累计退回额冻结、复核版本化
  routers/
    catalog.py            # 作品 / 权利人 / 协议（区间互斥、协议修订预览/确认、版本追溯）
    plays.py              # 播放导入、幂等、更正链
    statements.py         # 单据出具、试算、查询、逐笔归因分页查询
    payments.py           # 付款登记、按权利人对账
    instructions.py       # 付款额度预留指令：预留 / 取消 / 确认 / 单指令查询
    refunds.py            # 银行退票登记
    receipts.py           # 回单批次导入、复核、版本查询
examples/demo.sh          # 端到端 curl 示例（含银行退票登记与退回后复核）
examples/demo_instructions.sh  # 两阶段付款（预留/取消/确认）端到端 curl 示例
examples/demo_amendments.sh    # 协议修订（预览/确认/版本追溯/追补）端到端 curl 示例
tests/                    # 单元测试与七套 pgserver 端到端集成测试（含协议修订）
Dockerfile  docker-compose.yml  requirements.txt
```
