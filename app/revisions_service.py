"""协议改版：运营按"协议编号 + 所见版本"提交带时区的新适用区间与完整权利人比例。

两阶段：
1. 预览（POST /agreements/revisions/preview）：只读、不改账。按"新版区间与比例生效后"
   重算受影响播放的待处理变化，以及已出单期间各权利人的应付差额，并签发一个绑定
   「所见版本 + 预览时协议/播放更正链/期间单据指纹」的无状态防篡改令牌。
2. 确认（POST /agreements/revisions/confirm）：在"每作品事务咨询锁"下验签令牌、
   复核三项指纹——若该作品的现行协议、相关播放更正链或相关期间单据在预览后发生
   变化，则 409 拒绝且不修改任何数据；通过后冻结旧版历史、原子切换现行版本。

确认后：协议编号（agreements.id）保持稳定，旧版在 agreement_versions 中可追溯；
播放按其发生时刻使用现行版本重算（匹配逻辑不变）；失去协议匹配的播放转待处理；
已出单期间不被改写——运营随后对原期间调用 /statements/issue，仍由原追补机制
逐笔（added/removed/legacy）体现正负差额。原协议登记与查询接口的入参、响应字段
与错误约定保持兼容（仅新增 version 等字段）。
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal

from .allocation import allocate
from .config import settings
from .errors import APIError
from .locks import combined_lock_key

# 与协议登记共用同一把"每作品事务咨询锁"（同一命名空间），
# 使改版与新协议登记、改版与改版之间对同一作品完全串行。
_AGREEMENT_LOCK_NS = 0x41475231  # "AGR1"

# 链尾"有效播放"递归 CTE。tails 为链尾编号集合（不同调用方提供不同来源）；
# 沿 corrects_id 回溯出更正链原始编号（root_id）；
# LEFT JOIN 协议按播放发生时刻匹配（区间不重叠，最多命中一条）。
# override 参数给定时（预览假设重算），仅替换目标协议行的区间，其他协议不动。
_CHAIN_SQL_TEMPLATE = """
WITH RECURSIVE tails AS MATERIALIZED (__TAILS_SQL__),
chain AS (
    SELECT t.import_id AS tail_id, t.import_id AS walk_id, 0 AS depth
    FROM tails t
    UNION ALL
    SELECT ch.tail_id, p.corrects_id, ch.depth + 1
    FROM chain ch
    JOIN plays p ON p.import_id = ch.walk_id
    WHERE p.corrects_id IS NOT NULL AND ch.depth < 10000
),
eff_agreements AS (
    SELECT id, work_code,
           CASE WHEN id = %(override_id)s THEN %(override_from)s ELSE valid_from END AS valid_from,
           CASE WHEN id = %(override_id)s THEN %(override_to)s ELSE valid_to END AS valid_to
    FROM agreements
)
SELECT
    ch.tail_id AS import_id,
    ch.root_id,
    p.work_code,
    p.occurred_at,
    p.amount,
    a.id AS agreement_id
FROM (
    SELECT DISTINCT ON (tail_id) tail_id, walk_id AS root_id
    FROM chain
    ORDER BY tail_id, depth DESC
) ch
JOIN plays p ON p.import_id = ch.tail_id
LEFT JOIN eff_agreements a
    ON a.work_code = p.work_code
   AND tstzrange(a.valid_from, a.valid_to, '[)') @> p.occurred_at
ORDER BY p.occurred_at, ch.tail_id
"""

# 全部链尾（按期间）
_TAILS_RANGE = """
SELECT import_id FROM plays p
WHERE p.occurred_at >= %(start)s AND p.occurred_at < %(end)s
  AND NOT EXISTS (SELECT 1 FROM plays c WHERE c.corrects_id = p.import_id)
"""

# 某作品全部链尾
_TAILS_WORK = """
SELECT import_id FROM plays p
WHERE p.work_code = %(work_code)s
  AND NOT EXISTS (SELECT 1 FROM plays c WHERE c.corrects_id = p.import_id)
"""

# 某作品"相关播放更正链"：作品自身的全部播放，加上与这些播放同处一条更正链的播放
# （先向上找根，再从根向下取整条链）。
_RELATED_PLAYS_SQL = """
WITH RECURSIVE anc(import_id, root_id, depth) AS (
    SELECT import_id, import_id, 0 FROM plays WHERE work_code = %(work_code)s
    UNION ALL
    SELECT a.import_id, p.corrects_id, a.depth + 1
    FROM anc a JOIN plays p ON p.import_id = a.root_id
    WHERE p.corrects_id IS NOT NULL AND a.depth < 10000
),
roots AS (SELECT DISTINCT root_id FROM anc),
descendants(import_id, depth) AS (
    SELECT r.root_id, 0 FROM roots r
    UNION ALL
    SELECT c.import_id, d.depth + 1
    FROM descendants d
    JOIN plays c ON c.corrects_id = d.import_id
    WHERE d.depth < 10000
)
SELECT DISTINCT p.import_id, p.work_code,
       to_char(p.occurred_at AT TIME ZONE 'UTC',
               'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') AS occ_s,
       p.amount, p.corrects_id,
       to_char(p.created_at AT TIME ZONE 'UTC',
               'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') AS crt_s
FROM descendants d
JOIN plays p ON p.import_id = d.import_id
ORDER BY p.import_id
"""


def _chain_sql(tails_sql: str) -> str:
    return _CHAIN_SQL_TEMPLATE.replace("__TAILS_SQL__", tails_sql)


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _share_out(shares: dict[str, Decimal]) -> list[dict]:
    return [
        {"holder_code": h, "percent": str(shares[h])}
        for h in sorted(shares)
    ]


def _parts_out(parts: dict[str, int]) -> dict[str, int]:
    return {h: int(parts[h]) for h in sorted(parts) if parts[h] != 0}


# ---- 现行协议与比例读取 ----

def _get_agreement(conn, agreement_id: int, *, for_update: bool = False):
    return conn.execute(
        f"""
        SELECT id, work_code, valid_from, valid_to, note, version, created_at
        FROM agreements WHERE id = %s {'FOR UPDATE' if for_update else ''}
        """,
        (agreement_id,),
    ).fetchone()


def _load_shares(conn, agreement_ids) -> dict[int, dict[str, Decimal]]:
    ids = [i for i in agreement_ids if i is not None]
    if not ids:
        return {}
    rows = conn.execute(
        """
        SELECT agreement_id, holder_code, percent
        FROM agreement_shares
        WHERE agreement_id = ANY(%s)
        ORDER BY holder_code
        """,
        (ids,),
    ).fetchall()
    out: dict[int, dict[str, Decimal]] = {}
    for r in rows:
        out.setdefault(r["agreement_id"], {})[r["holder_code"]] = r["percent"]
    return out


# ---- 无状态防篡改预览令牌 ----

def _canonical_shares(shares: dict[str, Decimal]) -> str:
    # numeric 量化为 4 位小数字符串，避免 100 与 100.0000 的传输差异
    return json.dumps(
        [[h, str(shares[h].quantize(Decimal("0.0001")))] for h in sorted(shares)],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sign(payload_b64: str) -> str:
    mac = hmac.new(
        settings.revision_token_secret.encode(), payload_b64.encode(), hashlib.sha256
    )
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def issue_token(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{body}.{_sign(body)}"


def verify_token(token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise _bad_token("malformed")
    if not hmac.compare_digest(sig, _sign(body)):
        raise _bad_token("bad signature")
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        return json.loads(raw)
    except Exception:
        raise _bad_token("malformed payload")


def _bad_token(reason: str) -> APIError:
    return APIError(
        422, "revision_token_invalid",
        f"preview_token is invalid ({reason}); request a new preview",
    )


# ---- 预览时的状态指纹 ----

def _agreements_fingerprint(conn, work_code: str) -> str:
    """作品现行协议状态：各协议版本、区间与完整比例（任一变化即失配）。"""
    rows = conn.execute(
        """
        SELECT a.id, a.version,
               to_char(a.valid_from AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') AS from_s,
               CASE WHEN a.valid_to IS NULL THEN NULL
                    ELSE to_char(a.valid_to AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') END AS to_s,
               a.note
        FROM agreements a
        WHERE a.work_code = %s
        ORDER BY a.id
        """,
        (work_code,),
    ).fetchall()
    shares = conn.execute(
        """
        SELECT s.agreement_id, s.holder_code,
               to_char(s.percent, 'FM999990.0000') AS pct
        FROM agreement_shares s
        JOIN agreements a ON a.id = s.agreement_id
        WHERE a.work_code = %s
        ORDER BY s.agreement_id, s.holder_code
        """,
        (work_code,),
    ).fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(
            f"A|{r['id']}|{r['version']}|{r['from_s']}|{r['to_s']}|{r['note']}\n".encode()
        )
    for r in shares:
        h.update(f"S|{r['agreement_id']}|{r['holder_code']}|{r['pct']}\n".encode())
    return h.hexdigest()


def _plays_fingerprint(conn, work_code: str) -> str:
    """相关播放更正链：链上任一记录的新增/修改/更正均使指纹失配。"""
    rows = conn.execute(_RELATED_PLAYS_SQL, {"work_code": work_code}).fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(
            f"P|{r['import_id']}|{r['work_code']}|{r['occ_s']}|{r['amount']}|"
            f"{r['corrects_id']}|{r['crt_s']}\n".encode()
        )
    return h.hexdigest()


def _work_tail_occurrences(conn, work_code: str) -> list[datetime]:
    """该作品全部链尾播放的发生时刻（用于确定相关的已出单期间集合）。"""
    rows = conn.execute(
        _chain_sql(_TAILS_WORK),
        _override_params({"work_code": work_code}, None),
    ).fetchall()
    return [r["occurred_at"] for r in rows]


def _work_statement_periods(conn, occurrences: list[datetime]) -> list[tuple]:
    """覆盖（半开期间内包含）任一给定播放发生时刻的已出单期间。"""
    if not occurrences:
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT b.period_start, b.period_end
        FROM statement_batches b
        WHERE EXISTS (
            SELECT 1 FROM unnest(%s::timestamptz[]) AS t(x)
            WHERE t.x >= b.period_start AND t.x < b.period_end
        )
        ORDER BY b.period_start, b.period_end
        """,
        (occurrences,),
    ).fetchall()
    return [(r["period_start"], r["period_end"]) for r in rows]


def _statements_fingerprint(conn, periods: list[tuple]) -> str:
    """相关期间单据：批次、明细行、待处理快照（追加任何单据即失配）。"""
    h = hashlib.sha256()
    for start, end in periods:
        b_rows = conn.execute(
            """
            SELECT id, kind, supersedes_batch_id, evidence_status,
                   to_char(created_at AT TIME ZONE 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') AS created_s
            FROM statement_batches
            WHERE period_start = %s AND period_end = %s
            ORDER BY id
            """,
            (start, end),
        ).fetchall()
        for b in b_rows:
            h.update(
                f"B|{b['id']}|{b['kind']}|{b['supersedes_batch_id']}|"
                f"{b['evidence_status']}|{b['created_s']}\n".encode()
            )
            l_rows = conn.execute(
                "SELECT holder_code, amount FROM statement_lines "
                "WHERE batch_id = %s ORDER BY holder_code",
                (b["id"],),
            ).fetchall()
            for l in l_rows:
                h.update(f"L|{l['holder_code']}|{l['amount']}\n".encode())
        p_rows = conn.execute(
            """
            SELECT import_id, work_code, amount, reason,
                   to_char(occurred_at AT TIME ZONE 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') AS occ_s
            FROM statement_pending
            WHERE period_start = %s AND period_end = %s
            ORDER BY import_id
            """,
            (start, end),
        ).fetchall()
        for p in p_rows:
            h.update(
                f"D|{p['import_id']}|{p['work_code']}|{p['occ_s']}|"
                f"{p['amount']}|{p['reason']}\n".encode()
            )
    return h.hexdigest()


# ---- 假设重算（新版区间 + 新比例生效后的链尾匹配与分摊） ----

def _override_params(params: dict, override: dict | None) -> dict:
    prm = dict(params)
    if override is None:
        prm.update({"override_id": None, "override_from": None, "override_to": None})
    else:
        prm.update({
            "override_id": override["agreement_id"],
            "override_from": override["valid_from"],
            "override_to": override["valid_to"],
        })
    return prm


def _allocate_rows(conn, rows, share_overrides: dict[int, dict[str, Decimal]] | None = None):
    """对匹配到协议的链尾逐笔分摊，返回 (chains, pending_rows)。"""
    ids = {r["agreement_id"] for r in rows if r["agreement_id"] is not None}
    shares_map = _load_shares(conn, ids)
    if share_overrides:
        for aid, sh in share_overrides.items():
            shares_map[aid] = sh

    chains: list[dict] = []
    pending: list[dict] = []
    for r in rows:
        if r["agreement_id"] is None:
            pending.append(
                {
                    "import_id": r["import_id"],
                    "root_id": r["root_id"],
                    "work_code": r["work_code"],
                    "occurred_at": r["occurred_at"],
                    "amount": r["amount"],
                }
            )
            continue
        shares = shares_map[r["agreement_id"]]
        parts = allocate(r["amount"], shares)
        chains.append(
            {
                "root_id": r["root_id"],
                "tail_id": r["import_id"],
                "work_code": r["work_code"],
                "occurred_at": r["occurred_at"],
                "amount": r["amount"],
                "agreement_id": r["agreement_id"],
                "shares": shares,
                "parts": parts,
            }
        )
    return chains, pending


def _prior_payable(conn, start, end) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT l.holder_code, COALESCE(SUM(l.amount), 0) AS total
        FROM statement_lines l
        JOIN statement_batches b ON b.id = l.batch_id
        WHERE b.period_start = %s AND b.period_end = %s
        GROUP BY l.holder_code
        """,
        (start, end),
    ).fetchall()
    return {r["holder_code"]: int(r["total"]) for r in rows}


def _period_payable_from(rows, start, end) -> dict[str, int]:
    """从链尾行（含逐笔分摊）按期间汇总全部作品的应付。"""
    total: dict[str, int] = {}
    for ch in rows:
        if start <= ch["occurred_at"] < end:
            for h, a in ch["parts"].items():
                if a:
                    total[h] = total.get(h, 0) + a
    return total


def _build_impact(conn, work_code: str, agreement_id: int,
                  new_from, new_to, new_shares: dict[str, Decimal]):
    """核心只读计算：新版生效后的受影响播放与已出单期间差额。"""
    override = {"agreement_id": agreement_id, "valid_from": new_from, "valid_to": new_to}
    params = {"work_code": work_code}

    # 现行：该作品全部链尾
    cur_rows = conn.execute(
        _chain_sql(_TAILS_WORK), _override_params(params, None)
    ).fetchall()
    cur_chains, cur_pending = _allocate_rows(conn, cur_rows)
    cur_by_tail = {ch["tail_id"]: ch for ch in cur_chains}
    cur_pending_ids = {p["import_id"] for p in cur_pending}

    # 新版假设：替换目标协议的区间与比例
    new_rows = conn.execute(
        _chain_sql(_TAILS_WORK), _override_params(params, override)
    ).fetchall()
    new_chains, new_pending = _allocate_rows(
        conn, new_rows, share_overrides={agreement_id: new_shares}
    )
    new_by_tail = {ch["tail_id"]: ch for ch in new_chains}
    new_pending_ids = {p["import_id"] for p in new_pending}

    # 受影响 = 匹配协议变化，或逐笔分摊变化（新区间边界 / 新比例 / 待处理状态变化）
    affected: list[dict] = []
    for tail in sorted(set(cur_by_tail) | set(new_by_tail)
                       | cur_pending_ids | new_pending_ids):
        cur = cur_by_tail.get(tail)
        new = new_by_tail.get(tail)
        cur_aid = cur["agreement_id"] if cur else None
        new_aid = new["agreement_id"] if new else None
        cur_parts = _parts_out(cur["parts"]) if cur else {}
        new_parts = _parts_out(new["parts"]) if new else {}
        if cur_aid == new_aid and cur_parts == new_parts:
            continue
        ctx = new if new is not None else cur
        affected.append(
            {
                "import_id": tail,
                "root_import_id": ctx["root_id"],
                "work_code": ctx["work_code"],
                "occurred_at": ctx["occurred_at"],
                "amount": ctx["amount"],
                "before_agreement_id": cur_aid,
                "after_agreement_id": new_aid,
                "before_shares": cur_parts,
                "after_shares": new_parts,
                "before_pending": tail in cur_pending_ids,
                "after_pending": tail in new_pending_ids,
            }
        )
    affected.sort(key=lambda x: (x["occurred_at"], x["import_id"]))
    become_pending = [x for x in affected if not x["before_pending"] and x["after_pending"]]
    leave_pending = [x for x in affected if x["before_pending"] and not x["after_pending"]]

    # 已出单期间差额：只看覆盖该作品链尾的期间；但期间应付按全部作品重算
    # （其他作品播放不受本次改版影响，自然不产生差额）。
    occurrences = [ch["occurred_at"] for ch in cur_chains]
    occurrences += [p["occurred_at"] for p in cur_pending]
    periods = _work_statement_periods(conn, occurrences)
    issued_periods: list[dict] = []
    if periods:
        all_cur_rows = conn.execute(
            _chain_sql(_TAILS_RANGE),
            _override_params({"start": periods[0][0], "end": periods[-1][1]}, None),
        ).fetchall()
        all_cur_chains, _ = _allocate_rows(conn, all_cur_rows)
        all_new_rows = conn.execute(
            _chain_sql(_TAILS_RANGE),
            _override_params(
                {"start": periods[0][0], "end": periods[-1][1]}, override
            ),
        ).fetchall()
        all_new_chains, _ = _allocate_rows(
            conn, all_new_rows, share_overrides={agreement_id: new_shares}
        )
    else:
        all_cur_chains = all_new_chains = []

    for start, end in periods:
        billed = _prior_payable(conn, start, end)
        cur_payable = _period_payable_from(all_cur_chains, start, end)
        new_payable = _period_payable_from(all_new_chains, start, end)
        delta = {
            h: new_payable.get(h, 0) - cur_payable.get(h, 0)
            for h in set(cur_payable) | set(new_payable)
            if new_payable.get(h, 0) - cur_payable.get(h, 0) != 0
        }
        if not delta:
            continue
        issued_periods.append(
            {
                "period_start": start,
                "period_end": end,
                "billed": {
                    h: billed.get(h, 0)
                    for h in sorted(set(billed) | set(cur_payable) | set(new_payable))
                },
                "current_payable": {h: cur_payable[h] for h in sorted(cur_payable)},
                "projected_payable": {h: new_payable[h] for h in sorted(new_payable)},
                "delta": {h: delta[h] for h in sorted(delta)},
            }
        )

    return {
        "affected_plays": affected,
        "become_pending": become_pending,
        "leave_pending": leave_pending,
        "issued_periods": issued_periods,
        "related_periods": periods,
    }


def _proposal_from_body(body) -> dict:
    return {
        "agreement_id": body.agreement_id,
        "seen_version": body.seen_version,
        "valid_from": body.valid_from.astimezone(timezone.utc),
        "valid_to": body.valid_to.astimezone(timezone.utc) if body.valid_to else None,
        "note": body.note,
        "shares": {s.holder_code: s.percent for s in body.shares},
    }


def _proposal_matches_token(proposal: dict, tok: dict) -> bool:
    return (
        proposal["agreement_id"] == tok["agreement_id"]
        and proposal["seen_version"] == tok["seen_version"]
        and _utc_iso(proposal["valid_from"]) == tok["valid_from"]
        and _utc_iso(proposal["valid_to"]) == tok["valid_to"]
        and proposal["note"] == tok["note"]
        and _canonical_shares(proposal["shares"]) == tok["shares"]
    )


def _check_overlap(conn, work_code: str, agreement_id: int, valid_from, valid_to):
    overlap = conn.execute(
        """
        SELECT id FROM agreements
        WHERE work_code = %s
          AND id <> %s
          AND (valid_from, COALESCE(valid_to, 'infinity'::timestamptz))
              OVERLAPS
              (%s::timestamptz, COALESCE(%s::timestamptz, 'infinity'::timestamptz))
        LIMIT 1
        """,
        (work_code, agreement_id, valid_from, valid_to),
    ).fetchone()
    if overlap is not None:
        raise APIError(
            409, "agreement_overlap",
            f"revised range overlaps existing agreement {overlap['id']} "
            f"of work {work_code}",
        )


def preview_revision(conn, body) -> dict:
    """只读预览：校验现行协议与所见版本，签发令牌并给出受影响播放与期间差额。"""
    proposal = _proposal_from_body(body)
    with conn.transaction():
        # 一次预览内全部指纹与重算来自同一快照
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        agr = _get_agreement(conn, proposal["agreement_id"])
        if agr is None:
            raise APIError(
                404, "agreement_not_found",
                f"agreement {proposal['agreement_id']} not found",
            )
        if agr["version"] != proposal["seen_version"]:
            raise APIError(
                409, "agreement_version_stale",
                f"agreement {proposal['agreement_id']} is at version {agr['version']}, "
                f"but seen_version is {proposal['seen_version']}",
                detail={"current_version": agr["version"]},
            )
        work_code = agr["work_code"]
        _check_overlap(
            conn, work_code, proposal["agreement_id"],
            proposal["valid_from"], proposal["valid_to"],
        )

        impact = _build_impact(
            conn, work_code, proposal["agreement_id"],
            proposal["valid_from"], proposal["valid_to"], proposal["shares"],
        )
        token = issue_token(
            {
                "agreement_id": proposal["agreement_id"],
                "work_code": work_code,
                "seen_version": proposal["seen_version"],
                "new_version": agr["version"] + 1,
                "valid_from": _utc_iso(proposal["valid_from"]),
                "valid_to": _utc_iso(proposal["valid_to"]),
                "note": proposal["note"],
                "shares": _canonical_shares(proposal["shares"]),
                "fp_agreements": _agreements_fingerprint(conn, work_code),
                "fp_plays": _plays_fingerprint(conn, work_code),
                "fp_statements": _statements_fingerprint(conn, impact["related_periods"]),
                "periods": [[_utc_iso(s), _utc_iso(e)] for s, e in impact["related_periods"]],
            }
        )

    impact.pop("related_periods", None)
    return {
        "agreement_id": proposal["agreement_id"],
        "work_code": work_code,
        "seen_version": proposal["seen_version"],
        "new_version": agr["version"] + 1,
        "valid_from": proposal["valid_from"],
        "valid_to": proposal["valid_to"],
        "shares": _share_out(proposal["shares"]),
        "preview_token": token,
        **impact,
    }


def _lock_work_and_holders(conn, work_code: str, proposal_shares) -> None:
    """确认前的加锁屏障：指纹读取之后到本事务提交，相关播放导入与出单都无法插入。

    加锁次序（与全库统一，杜绝死锁）：先按编号升序锁权利人，最后锁作品行：
    - 权利人行：该作品全部协议版本出现过的权利人 + 新版权利人。
      与出单侧 lock_holders_for_billing、付款/退票/预留的行锁同命名、同次序；
      在途出单（持权利人行锁、从不等待作品行）先完成，之后的出单阻塞到本事务结束；
    - works 行 FOR UPDATE 最后获取：与播放导入持有的 works FOR SHARE 冲突，
      在途的该作品播放导入先提交（随后指纹即可读到），之后的导入等待本事务结束。
      播放导入从不锁定权利人行，故"权利人 → 作品"次序不产生锁环。
    """
    rows = conn.execute(
        """
        SELECT DISTINCT vs.holder_code
        FROM agreement_version_shares vs
        JOIN agreements a ON a.id = vs.agreement_id
        WHERE a.work_code = %s
        ORDER BY vs.holder_code
        """,
        (work_code,),
    ).fetchall()
    holders = {r["holder_code"] for r in rows} | set(proposal_shares)
    for code in sorted(holders):
        conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (code,),
        )
    conn.execute("SELECT code FROM works WHERE code = %s FOR UPDATE", (work_code,))


def _stale_error(kind: str, changed: str):
    return APIError(
        409, "revision_state_changed",
        f"{kind} changed between preview and confirm; revision rejected without "
        "modifying any data",
        detail={"changed": changed},
    )


def confirm_revision(conn, body) -> dict:
    """确认改版：验签/复核指纹通过后，原子冻结旧版并切换现行版本（不改账）。"""
    proposal = _proposal_from_body(body)
    token = verify_token(body.preview_token)
    if not _proposal_matches_token(proposal, token):
        raise APIError(
            422, "revision_request_mismatch",
            "request body does not match the previewed revision; request a new preview",
        )

    with conn.transaction():
        # 与协议登记共用每作品锁：改版、新登记、其他改版对同一作品串行
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (combined_lock_key(_AGREEMENT_LOCK_NS, token["work_code"]),),
        )
        agr = _get_agreement(conn, proposal["agreement_id"], for_update=True)
        if agr is None:
            raise APIError(
                404, "agreement_not_found",
                f"agreement {proposal['agreement_id']} not found",
            )
        if agr["work_code"] != token["work_code"]:
            raise _stale_error("the agreement's work", "agreements")
        if agr["version"] != proposal["seen_version"]:
            # 过期版本：预览后该协议已被改版
            raise APIError(
                409, "agreement_version_stale",
                f"agreement {proposal['agreement_id']} is at version {agr['version']}, "
                f"but seen_version is {proposal['seen_version']}",
                detail={"current_version": agr["version"]},
            )

        # 加锁屏障：此后相关播放导入/出单无法并发提交，指纹复核即为"所见即所改"。
        _lock_work_and_holders(conn, token["work_code"], proposal["shares"])

        # 复核三项指纹：现行协议 / 相关播放更正链 / 相关期间单据
        if _agreements_fingerprint(conn, token["work_code"]) != token["fp_agreements"]:
            raise _stale_error("the work's current agreement(s)", "agreements")
        if _plays_fingerprint(conn, token["work_code"]) != token["fp_plays"]:
            raise _stale_error("a related play correction chain", "play_chains")
        occurrences = _work_tail_occurrences(conn, token["work_code"])
        periods_now = _work_statement_periods(conn, occurrences)
        periods_now_iso = [[_utc_iso(s), _utc_iso(e)] for s, e in periods_now]
        if periods_now_iso != token["periods"]:
            raise _stale_error(
                "the set of issued periods covering related plays", "statement_periods"
            )
        if _statements_fingerprint(conn, periods_now) != token["fp_statements"]:
            raise _stale_error("a related period statement", "statements")

        # 区间在持锁状态下再验一遍（预览后可能有并发新登记/改版）
        _check_overlap(
            conn, token["work_code"], proposal["agreement_id"],
            proposal["valid_from"], proposal["valid_to"],
        )

        old_version = agr["version"]
        new_version = old_version + 1

        # 切换前按"新版生效后"计算影响（现行数据尚未变动，与预览同口径）
        impact = _build_impact(
            conn, token["work_code"], proposal["agreement_id"],
            proposal["valid_from"], proposal["valid_to"], proposal["shares"],
        )
        impact.pop("related_periods", None)

        # 1) 冻结新版历史（区间、备注、完整比例）
        conn.execute(
            """
            INSERT INTO agreement_versions
                (agreement_id, version, work_code, valid_from, valid_to, note,
                 created_at, superseded_at)
            VALUES (%s, %s, %s, %s, %s, %s, now(), NULL)
            """,
            (
                proposal["agreement_id"], new_version, token["work_code"],
                proposal["valid_from"], proposal["valid_to"], proposal["note"],
            ),
        )
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO agreement_version_shares
                    (agreement_id, version, holder_code, percent)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (proposal["agreement_id"], new_version, h, p)
                    for h, p in proposal["shares"].items()
                ],
            )

        # 2) 旧版本标记被取代（历史冻结表唯一允许的一次性回填）
        conn.execute(
            """
            UPDATE agreement_versions
            SET superseded_at = now()
            WHERE agreement_id = %s AND version = %s AND superseded_at IS NULL
            """,
            (proposal["agreement_id"], old_version),
        )

        # 3) 自动建档新权利人（与原登记接口一致），再切换现行比例（旧比例行删除，
        #    完整比例已冻结在 agreement_version_shares；历史归因不引用本表行）
        for h in proposal["shares"]:
            conn.execute(
                "INSERT INTO rightsholders(code) VALUES (%s) ON CONFLICT (code) DO NOTHING",
                (h,),
            )
        conn.execute(
            "DELETE FROM agreement_shares WHERE agreement_id = %s",
            (proposal["agreement_id"],),
        )
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO agreement_shares(agreement_id, holder_code, percent)
                VALUES (%s, %s, %s)
                """,
                [
                    (proposal["agreement_id"], h, p)
                    for h, p in proposal["shares"].items()
                ],
            )

        # 4) 切换现行协议行（触发器只允许 version +1、编号/作品/创建时间不变）
        conn.execute(
            """
            UPDATE agreements
            SET valid_from = %s, valid_to = %s, note = %s,
                version = %s, updated_at = now()
            WHERE id = %s
            """,
            (
                proposal["valid_from"], proposal["valid_to"], proposal["note"],
                new_version, proposal["agreement_id"],
            ),
        )

    return {
        "status": "revised",
        "agreement_id": proposal["agreement_id"],
        "work_code": token["work_code"],
        "version": new_version,
        "valid_from": proposal["valid_from"],
        "valid_to": proposal["valid_to"],
        "shares": _share_out(proposal["shares"]),
        **impact,
    }


# ---- 历史版本查询 ----

def list_versions(conn, agreement_id: int) -> list[dict] | None:
    agr = _get_agreement(conn, agreement_id)
    if agr is None:
        return None
    rows = conn.execute(
        """
        SELECT agreement_id, version, work_code, valid_from, valid_to,
               note, created_at, superseded_at
        FROM agreement_versions
        WHERE agreement_id = %s
        ORDER BY version
        """,
        (agreement_id,),
    ).fetchall()
    out = []
    for v in rows:
        share_rows = conn.execute(
            """
            SELECT holder_code, percent
            FROM agreement_version_shares
            WHERE agreement_id = %s AND version = %s
            ORDER BY holder_code
            """,
            (agreement_id, v["version"]),
        ).fetchall()
        out.append(
            {
                "agreement_id": v["agreement_id"],
                "version": v["version"],
                "work_code": v["work_code"],
                "valid_from": v["valid_from"],
                "valid_to": v["valid_to"],
                "note": v["note"],
                "created_at": v["created_at"],
                "superseded_at": v["superseded_at"],
                "current": v["version"] == agr["version"],
                "shares": [
                    {"holder_code": s["holder_code"], "percent": str(s["percent"])}
                    for s in share_rows
                ],
            }
        )
    return out
