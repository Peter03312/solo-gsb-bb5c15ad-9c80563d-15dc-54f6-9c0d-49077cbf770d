"""协议修订（amendment）：按协议编号提交"新适用区间 + 完整权利人比例"的新版本。

两阶段、乐观并发：
- preview：完成全部静态校验（协议存在、区间有效、合计 100%、与其他现行协议
  不重叠、权利人已建档），试算受影响链尾播放的待处理变化与已出单期间各权利人
  的应付差额，并签发带状态指纹的预览令牌；**不改任何数据**。
- confirm：重验令牌、内容与状态指纹。预览后该作品的现行协议、相关播放更正链
  或相关期间单据任一变化即 409 amendment_preview_stale 拒绝，且不修改数据；
  通过后追加新版本（协议编号保持稳定、旧版可追溯），仅改协议数据本身——
  不重写任何既有单据，已出单期间的正负差额在调用方再出具时经原追补机制体现。

并发协议：与协议登记共用"每作品事务咨询锁"，同作品的登记/修订串行化，
重叠检测确定；播放匹配始终走协议版本表单语句快照，不会读到撕裂状态。
"""
import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from .allocation import allocate
from .amendment_tokens import fingerprint, issue_token, verify_token
from .statements_service import compute_current, _load_prior_payable

# 协议登记/修订共用的每作品咨询锁命名空间（与 routers/catalog 中历史常量一致）
AGREEMENT_LOCK_NS = 0x41475231  # "AGR1"

_QUANT = Decimal("0.0001")


def combined_lock_key(namespace: int, work_code: str) -> int:
    n = int.from_bytes(
        hashlib.blake2b(work_code.encode(), digest_size=4).digest(), "big"
    )
    combined = (namespace << 32) | (n & 0xFFFFFFFF)
    return combined - (1 << 64) if combined >= (1 << 63) else combined


def lock_work_agreements(conn, work_code: str) -> None:
    """串行化同一作品的协议登记/修订（事务级咨询锁）。"""
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (combined_lock_key(AGREEMENT_LOCK_NS, work_code),),
    )


# ---- 读取 ----

def _get_agreement(conn, agreement_id: int):
    return conn.execute(
        """
        SELECT id, work_code, valid_from, valid_to, note, created_at, current_version
        FROM agreements WHERE id = %s
        """,
        (agreement_id,),
    ).fetchone()


def _version_shares(conn, agreement_id: int, version: int) -> dict[str, Decimal]:
    rows = conn.execute(
        """
        SELECT holder_code, percent
        FROM agreement_version_shares
        WHERE agreement_id = %s AND version = %s
        ORDER BY holder_code
        """,
        (agreement_id, version),
    ).fetchall()
    return {r["holder_code"]: r["percent"] for r in rows}


def _current_versions(conn, work_code: str) -> list[dict]:
    """该作品全部协议的"现行版本"（区间 + 比例），按协议编号排序。"""
    rows = conn.execute(
        """
        SELECT ag.id AS agreement_id, ag.current_version AS version,
               av.valid_from, av.valid_to,
               jsonb_object_agg(avs.holder_code, avs.percent::text
                                ORDER BY avs.holder_code) AS shares
        FROM agreements ag
        JOIN agreement_versions av
            ON av.agreement_id = ag.id AND av.version = ag.current_version
        JOIN agreement_version_shares avs
            ON avs.agreement_id = av.agreement_id AND avs.version = av.version
        WHERE ag.work_code = %s
        GROUP BY ag.id, ag.current_version, av.valid_from, av.valid_to
        ORDER BY ag.id
        """,
        (work_code,),
    ).fetchall()
    return [
        {
            "agreement_id": r["agreement_id"],
            "version": r["version"],
            "valid_from": r["valid_from"],
            "valid_to": r["valid_to"],
            "shares": {h: Decimal(p) for h, p in r["shares"].items()},
        }
        for r in rows
    ]


# 该作品全部"更正链链尾"播放，附现行世界的协议匹配与逐笔比例（单语句快照）。
WORK_TAIL_PLAYS_SQL = """
WITH RECURSIVE chain AS (
    SELECT import_id AS tail_id, import_id AS walk_id, 0 AS depth
    FROM plays p
    WHERE p.work_code = %s
      AND NOT EXISTS (SELECT 1 FROM plays c WHERE c.corrects_id = p.import_id)
    UNION ALL
    SELECT ch.tail_id, p.corrects_id, ch.depth + 1
    FROM chain ch
    JOIN plays p ON p.import_id = ch.walk_id
    WHERE p.corrects_id IS NOT NULL AND ch.depth < 10000
)
SELECT
    ch.tail_id AS import_id,
    ch.root_id,
    p.work_code,
    p.occurred_at,
    p.amount,
    m.agreement_id,
    m.percent AS shares
FROM (
    SELECT DISTINCT ON (tail_id) tail_id, walk_id AS root_id
    FROM chain
    ORDER BY tail_id, depth DESC
) ch
JOIN plays p ON p.import_id = ch.tail_id
LEFT JOIN LATERAL (
    SELECT x.agreement_id,
           jsonb_object_agg(x.holder_code, x.percent::text ORDER BY x.holder_code) AS percent
    FROM (
        SELECT av.agreement_id, avs.holder_code, avs.percent
        FROM agreements ag
        JOIN agreement_versions av
            ON av.agreement_id = ag.id AND av.version = ag.current_version
        JOIN agreement_version_shares avs
            ON avs.agreement_id = av.agreement_id AND avs.version = av.version
        WHERE ag.work_code = p.work_code
          AND tstzrange(av.valid_from, av.valid_to, '[)') @> p.occurred_at
    ) x
    GROUP BY x.agreement_id
) m ON TRUE
ORDER BY p.occurred_at, ch.tail_id
"""


def _work_tail_plays(conn, work_code: str) -> list[dict]:
    rows = conn.execute(WORK_TAIL_PLAYS_SQL, (work_code,)).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "import_id": r["import_id"],
                "root_id": r["root_id"],
                "work_code": r["work_code"],
                "occurred_at": r["occurred_at"],
                "amount": r["amount"],
                "old_agreement_id": r["agreement_id"],
                "old_shares": (
                    {h: Decimal(p) for h, p in r["shares"].items()}
                    if r["agreement_id"] is not None
                    else None
                ),
            }
        )
    return out


# ---- 区间匹配（半开 [from, to)，None 上界为开放） ----

def _covers(v: dict, ts: datetime) -> bool:
    if ts < v["valid_from"]:
        return False
    return v["valid_to"] is None or ts < v["valid_to"]


def _ranges_overlap(a_from, a_to, b_from, b_to) -> bool:
    return a_from < (b_to or datetime.max.replace(tzinfo=timezone.utc)) and \
        b_from < (a_to or datetime.max.replace(tzinfo=timezone.utc))


def _find_match(versions, ts, *, exclude_id=None, hypothetical=None):
    for v in versions:
        if exclude_id is not None and v["agreement_id"] == exclude_id:
            continue
        if _covers(v, ts):
            return v
    if hypothetical is not None and _covers(hypothetical, ts):
        return hypothetical
    return None


# ---- 请求规范化（令牌负载与重验共用） ----

def _canonical_request(valid_from: datetime, valid_to, note, shares) -> dict:
    return {
        "valid_from": valid_from.astimezone(timezone.utc).isoformat(),
        "valid_to": valid_to.astimezone(timezone.utc).isoformat()
        if valid_to is not None
        else None,
        "note": note,
        "shares": [
            {"holder_code": s.holder_code,
             "percent": format(Decimal(s.percent).quantize(_QUANT), "f")}
            for s in sorted(shares, key=lambda s: s.holder_code)
        ],
    }


def _pct_map(shares) -> dict[str, Decimal]:
    return {s.holder_code: Decimal(s.percent).quantize(_QUANT) for s in shares}


def _shares_out(shares: dict[str, Decimal] | None) -> list[dict] | None:
    if shares is None:
        return None
    return [
        {"holder_code": h, "percent": format(shares[h], "f")}
        for h in sorted(shares)
    ]


def _alloc_out(parts: dict[str, int] | None) -> dict[str, int] | None:
    if parts is None:
        return None
    return {h: parts[h] for h in sorted(parts)}


def _play_summary(p: dict) -> dict:
    return {
        "import_id": p["import_id"],
        "root_import_id": p["root_id"],
        "work_code": p["work_code"],
        "occurred_at": p["occurred_at"],
        "amount": p["amount"],
    }


# ---- 影响试算（纯读） ----

def _simulate(conn, agreement, versions, plays, valid_from, valid_to, shares_map,
              old_versions=None, old_plays=None, target_version=None):
    """在"目标协议替换为新版本"的假设世界里重算受影响播放与已出单期间差额。

    versions/plays 为"旧世界"的现行版本与链尾播放（含旧匹配）；
    confirm 时数据库已提交新版本，故期间应付重算读到的是新世界，
    而旧匹配由 old_versions/old_plays 显式给出（默认即 versions/plays）。
    target_version 为目标协议在新世界中的现行版本号（预览=base+1，
    确认=已提交的新版本号）。
    """
    old_versions = old_versions if old_versions is not None else versions
    old_plays = old_plays if old_plays is not None else plays
    work_code = agreement["work_code"]
    base_version = next(
        v["version"] for v in old_versions if v["agreement_id"] == agreement["id"]
    )
    proposed_version = (
        target_version if target_version is not None else base_version + 1
    )
    # 预览时目标协议的现行版仍是旧版（按 id 排除后叠加假设版）；
    # 确认时目标协议在 versions 中已是新版（直接采用，不再叠加假设）。
    committed_new = target_version is not None
    if committed_new:
        hypothetical = next(
            v for v in versions if v["agreement_id"] == agreement["id"]
        )
        exclude = None
    else:
        hypothetical = {
            "agreement_id": agreement["id"],
            "version": proposed_version,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "shares": shares_map,
        }
        exclude = agreement["id"]

    affected: list[dict] = []
    affected_ids: set[str] = set()
    to_pending: list[dict] = []
    from_pending: list[dict] = []
    # 受影响播放的按时间戳索引：import_id -> (时间, 权利人 -> 新分摊-旧分摊)
    affected_by_id: dict[str, tuple[datetime, dict[str, int], str]] = {}

    for p in old_plays:
        ts = p["occurred_at"]
        old_id = p["old_agreement_id"]
        old_parts = allocate(p["amount"], p["old_shares"]) if old_id is not None else None

        new_match = _find_match(
            versions, ts, exclude_id=exclude, hypothetical=hypothetical
        )
        new_id = new_match["agreement_id"] if new_match is not None else None
        new_parts = (
            allocate(p["amount"], new_match["shares"])
            if new_match is not None
            else None
        )

        if old_id != agreement["id"] and new_id != agreement["id"]:
            continue  # 与本次修订无关（其他现行协议或始终无匹配的播放）

        if old_id is None and new_id == agreement["id"]:
            change_type = "become_allocated"
        elif old_id == agreement["id"] and new_id is None:
            change_type = "become_pending"
        else:
            change_type = "reallocated"

        affected.append(
            {
                **_play_summary(p),
                "_root_id": p["root_id"],
                "change_type": change_type,
                "old": {
                    "agreement_id": old_id,
                    "version": (
                        base_version if old_id == agreement["id"]
                        else next(
                            (v["version"] for v in old_versions
                             if v["agreement_id"] == old_id),
                            None,
                        )
                    ),
                    "shares": (
                        _shares_out(p["old_shares"]) if old_id is not None else None
                    ),
                    "allocation": _alloc_out(old_parts),
                },
                "new": {
                    "agreement_id": new_id,
                    "version": new_match["version"] if new_match is not None else None,
                    "shares": (
                        _shares_out(new_match["shares"])
                        if new_match is not None
                        else None
                    ),
                    "allocation": _alloc_out(new_parts),
                },
            }
        )
        affected_ids.add(p["import_id"])
        if change_type == "become_pending":
            to_pending.append(_play_summary(p))
        elif change_type == "become_allocated":
            from_pending.append(_play_summary(p))

        delta: dict[str, int] = {}
        for h, amt in (old_parts or {}).items():
            delta[h] = delta.get(h, 0) - amt
        for h, amt in (new_parts or {}).items():
            delta[h] = delta.get(h, 0) + amt
        affected_by_id[p["import_id"]] = (ts, delta, change_type)

    # 已出单期间：凡覆盖任一受影响链尾播放的期间都纳入
    periods = []
    if affected:
        timestamps = [p["occurred_at"] for p in affected]
        period_rows = conn.execute(
            """
            SELECT DISTINCT period_start, period_end
            FROM statement_batches
            WHERE EXISTS (
                SELECT 1 FROM unnest(%s::timestamptz[]) AS t
                WHERE t >= period_start AND t < period_end
            )
            ORDER BY period_start
            """,
            (timestamps,),
        ).fetchall()

        for pr in period_rows:
            s, e = pr["period_start"], pr["period_end"]
            payable_now, pending_now, _chains = compute_current(conn, s, e)
            prior = _load_prior_payable(conn, s, e)

            payable_proj = dict(payable_now)
            period_delta: dict[str, int] = {}
            in_period_ids = {
                iid
                for iid, (ts, _d, _ct) in affected_by_id.items()
                if s <= ts < e
            }
            for iid in in_period_ids:
                _ts, delta, _ct = affected_by_id[iid]
                for h, amt in delta.items():
                    payable_proj[h] = payable_proj.get(h, 0) + amt
                    period_delta[h] = period_delta.get(h, 0) + amt

            # 待处理清单：移除本次获得匹配的、加入本次失去匹配的
            pending_after = [
                p for p in pending_now if p["import_id"] not in affected_ids
            ]
            for p in affected:
                if not (s <= p["occurred_at"] < e):
                    continue
                if p["change_type"] == "become_pending":
                    pending_after.append(
                        {
                            "import_id": p["import_id"],
                            "work_code": p["work_code"],
                            "occurred_at": p["occurred_at"],
                            "amount": p["amount"],
                            "reason": "no matching agreement at play occurrence time "
                            "after agreement amendment",
                        }
                    )
            pending_after.sort(key=lambda x: (x["occurred_at"], x["import_id"]))

            currently_pending = [
                _play_summary(p)
                for iid in in_period_ids
                for p in old_plays
                if p["import_id"] == iid and p["old_agreement_id"] is None
            ]

            holders = set(payable_proj) | set(prior)
            projected = [
                {"holder_code": h, "amount": payable_proj.get(h, 0) - prior.get(h, 0)}
                for h in sorted(holders)
                if payable_proj.get(h, 0) - prior.get(h, 0) != 0
            ]
            periods.append(
                {
                    "period_start": s,
                    "period_end": e,
                    "payable_delta_work": [
                        {"holder_code": h, "amount": period_delta[h]}
                        for h in sorted(period_delta)
                        if period_delta[h] != 0
                    ],
                    "currently_pending": currently_pending,
                    "pending_after": pending_after,
                    "projected_supplement": projected,
                }
            )

    return {
        "affected_plays": [
            {k: v for k, v in a.items() if not k.startswith("_")}
            for a in affected
        ],
        "pending_changes": {"to_pending": to_pending, "from_pending": from_pending},
        "affected_periods": periods,
    }


# ---- 状态指纹 ----

def _state_fingerprint(conn, work_code: str, versions, plays) -> str:
    agr_states = [
        {
            "id": v["agreement_id"],
            "version": v["version"],
            "valid_from": v["valid_from"],
            "valid_to": v["valid_to"],
            "shares": v["shares"],
        }
        for v in versions
    ]
    play_rows = conn.execute(
        """
        SELECT import_id, occurred_at, amount, corrects_id
        FROM plays WHERE work_code = %s
        """,
        (work_code,),
    ).fetchall()
    play_states = [dict(r) for r in play_rows]

    # 与该作品链尾播放相关的已出单期间快照（单据 id、逐行累计、待处理清单）
    tail_times = [p["occurred_at"] for p in plays]
    period_states: list[dict] = []
    if tail_times:
        srows = conn.execute(
            """
            SELECT DISTINCT period_start, period_end
            FROM statement_batches
            WHERE EXISTS (
                SELECT 1 FROM unnest(%s::timestamptz[]) AS t
                WHERE t >= period_start AND t < period_end
            )
            """,
            (tail_times,),
        ).fetchall()
        for sr in srows:
            s, e = sr["period_start"], sr["period_end"]
            latest = conn.execute(
                """
                SELECT id FROM statement_batches
                WHERE period_start = %s AND period_end = %s
                ORDER BY id DESC LIMIT 1
                """,
                (s, e),
            ).fetchone()
            line_rows = conn.execute(
                """
                SELECT l.holder_code, COALESCE(SUM(l.amount), 0) AS total
                FROM statement_lines l
                JOIN statement_batches b ON b.id = l.batch_id
                WHERE b.period_start = %s AND b.period_end = %s
                GROUP BY l.holder_code
                """,
                (s, e),
            ).fetchall()
            pend_rows = conn.execute(
                """
                SELECT import_id, amount FROM statement_pending
                WHERE period_start = %s AND period_end = %s
                """,
                (s, e),
            ).fetchall()
            period_states.append(
                {
                    "start": s,
                    "end": e,
                    "latest_batch_id": latest["id"],
                    "lines": {r["holder_code"]: int(r["total"]) for r in line_rows},
                    "pending": [
                        {"import_id": r["import_id"], "amount": int(r["amount"])}
                        for r in pend_rows
                    ],
                }
            )

    return fingerprint(agr_states, play_states, period_states)


def _proposed_payload(agreement, valid_from, valid_to, note, shares_map):
    return {
        "agreement_id": agreement["id"],
        "work_code": agreement["work_code"],
        "current_version": agreement["current_version"],
        "proposed_version": agreement["current_version"] + 1,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "note": note,
        "shares": _shares_out(shares_map),
    }


# ---- 公共阶段校验（预览/确认共用，调用方须已持有每作品咨询锁） ----

def _validate_locked(conn, agreement, versions, valid_from, valid_to, shares, *,
                     check_holders_exist):
    """返回 shares_map；全部不合法情形以 APIError 拒绝。"""
    from .errors import APIError

    # 与"其他现行协议"区间不得重叠（目标协议自身排除）
    for v in versions:
        if v["agreement_id"] == agreement["id"]:
            continue
        if _ranges_overlap(valid_from, valid_to, v["valid_from"], v["valid_to"]):
            raise APIError(
                409,
                "agreement_overlap",
                f"new version range overlaps current version of agreement "
                f"{v['agreement_id']} of work {agreement['work_code']}",
                detail={
                    "work_code": agreement["work_code"],
                    "other_agreement_id": v["agreement_id"],
                    "other_version": v["version"],
                },
            )

    shares_map = _pct_map(shares)

    if check_holders_exist:
        # 预览不得有任何写副作用（不能自动建档）：未知权利人直接拒绝
        missing = [
            s.holder_code
            for s in shares
            if conn.execute(
                "SELECT 1 FROM rightsholders WHERE code = %s", (s.holder_code,)
            ).fetchone()
            is None
        ]
        if missing:
            raise APIError(
                422,
                "holder_not_found",
                "holder codes in shares are not registered: "
                + ", ".join(sorted(missing)),
                detail={"missing_holder_codes": sorted(missing)},
            )

    return shares_map


# ---- 预览 ----

def preview_amendment(conn, agreement_id: int, body):
    from .errors import APIError

    with conn.transaction():
        agreement = _get_agreement(conn, agreement_id)
        if agreement is None:
            raise APIError(
                404, "agreement_not_found", f"agreement {agreement_id} not found"
            )
        lock_work_agreements(conn, agreement["work_code"])
        # 锁内重读，拿到确定状态
        agreement = _get_agreement(conn, agreement_id)
        versions = _current_versions(conn, agreement["work_code"])
        plays = _work_tail_plays(conn, agreement["work_code"])

        shares_map = _validate_locked(
            conn, agreement, versions,
            body.valid_from, body.valid_to, body.shares,
            check_holders_exist=True,
        )

        impact = _simulate(
            conn, agreement, versions, plays,
            body.valid_from, body.valid_to, shares_map,
        )
        state_fp = _state_fingerprint(
            conn, agreement["work_code"], versions, plays
        )
        token = issue_token(
            {
                "agreement_id": agreement_id,
                "work_code": agreement["work_code"],
                "base_version": agreement["current_version"],
                "request": _canonical_request(
                    body.valid_from, body.valid_to, body.note, body.shares
                ),
                "fingerprint": state_fp,
            }
        )

    return {
        "status": "preview",
        "agreement": {
            "id": agreement["id"],
            "work_code": agreement["work_code"],
            "current_version": agreement["current_version"],
            "proposed_version": agreement["current_version"] + 1,
        },
        **_proposed_payload(
            agreement, body.valid_from, body.valid_to, body.note, shares_map
        ),
        **impact,
        "preview_token": token,
    }


# ---- 确认 ----

def confirm_amendment(conn, agreement_id: int, body):
    from .errors import APIError

    try:
        token_payload = verify_token(body.preview_token)
    except ValueError:
        raise APIError(
            409,
            "amendment_preview_invalid",
            "preview_token is missing, malformed or its signature does not match",
        )

    if token_payload.get("agreement_id") != agreement_id:
        raise APIError(
            409,
            "amendment_preview_invalid",
            "preview_token was issued for a different agreement",
            detail={"token_agreement_id": token_payload.get("agreement_id")},
        )
    canonical = _canonical_request(
        body.valid_from, body.valid_to, body.note, body.shares
    )
    if token_payload.get("request") != canonical:
        raise APIError(
            409,
            "amendment_preview_invalid",
            "confirm payload differs from the previewed range/shares; "
            "preview again before confirming",
        )

    with conn.transaction():
        agreement = _get_agreement(conn, agreement_id)
        if agreement is None:
            raise APIError(
                404, "agreement_not_found", f"agreement {agreement_id} not found"
            )
        lock_work_agreements(conn, agreement["work_code"])
        agreement = _get_agreement(conn, agreement_id)

        # 过期版本：令牌基于的现行版已被取代
        if agreement["current_version"] != token_payload.get("base_version"):
            raise APIError(
                409,
                "agreement_version_conflict",
                f"agreement {agreement_id} was amended since the preview "
                f"(previewed version {token_payload.get('base_version')}, "
                f"current version {agreement['current_version']}); "
                "preview again before confirming",
                detail={
                    "agreement_id": agreement_id,
                    "previewed_version": token_payload.get("base_version"),
                    "current_version": agreement["current_version"],
                },
            )

        versions = _current_versions(conn, agreement["work_code"])
        plays = _work_tail_plays(conn, agreement["work_code"])

        # 预览后状态变化（现行协议 / 播放更正链 / 相关期间单据）：拒绝且不改数据
        current_fp = _state_fingerprint(
            conn, agreement["work_code"], versions, plays
        )
        if not (
            isinstance(token_payload.get("fingerprint"), str)
            and current_fp == token_payload["fingerprint"]
        ):
            raise APIError(
                409,
                "amendment_preview_stale",
                "the work's current agreements, related play correction chains "
                "or related period statements changed after the preview; "
                "preview again before confirming",
                detail={
                    "agreement_id": agreement_id,
                    "current_version": agreement["current_version"],
                },
            )

        # 锁内重验全部静态规则（区间/比例已由 Pydantic 保证，这里重验重叠）
        shares_map = _validate_locked(
            conn, agreement, versions,
            body.valid_from, body.valid_to, body.shares,
            check_holders_exist=False,
        )

        # 自动建档权利人（与 POST /agreements 相同；预览阶段已确保其存在）
        for s in body.shares:
            conn.execute(
                "INSERT INTO rightsholders(code) VALUES (%s) "
                "ON CONFLICT (code) DO NOTHING",
                (s.holder_code,),
            )

        # 提交前旧世界快照（用于在同一事务内计算"旧世界 -> 新世界"的影响摘要）
        prev_versions = versions
        prev_plays = plays

        new_version = agreement["current_version"] + 1
        # 1) 追加新版本（版本内容一经写入不可改写）
        conn.execute(
            """
            INSERT INTO agreement_versions
                (agreement_id, version, valid_from, valid_to, note, created_at)
            VALUES (%s, %s, %s, %s, %s, now())
            """,
            (agreement_id, new_version, body.valid_from, body.valid_to, body.note),
        )
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO agreement_version_shares
                    (agreement_id, version, holder_code, percent)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (agreement_id, new_version, s.holder_code,
                     Decimal(s.percent).quantize(_QUANT))
                    for s in body.shares
                ],
            )
        # 2) 旧版标记被取代（受控触发器仅允许这一次置位）
        conn.execute(
            """
            UPDATE agreement_versions
            SET superseded_at = now()
            WHERE agreement_id = %s AND version = %s AND superseded_at IS NULL
            """,
            (agreement_id, agreement["current_version"]),
        )
        # 3) 协议主表推进现行版本（编号稳定），镜像列同步指向新版本
        conn.execute(
            """
            UPDATE agreements
            SET current_version = %s,
                valid_from = %s,
                valid_to = %s,
                note = %s
            WHERE id = %s AND current_version = %s
            """,
            (new_version, body.valid_from, body.valid_to, body.note,
             agreement_id, agreement["current_version"]),
        )
        # 4) 现行比例镜像表：整体替换（延迟触发器在提交时校验合计 100%）
        conn.execute(
            "DELETE FROM agreement_shares WHERE agreement_id = %s",
            (agreement_id,),
        )
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO agreement_shares(agreement_id, holder_code, percent)
                VALUES (%s, %s, %s)
                """,
                [
                    (agreement_id, s.holder_code, Decimal(s.percent).quantize(_QUANT))
                    for s in body.shares
                ],
            )

        # 新世界影响摘要：期间应付/待处理读的是本事务已提交的版本状态，
        # 旧匹配与旧分摊由提交前快照（prev_*）给出，差额即本次修订的影响。
        versions_after = _current_versions(conn, agreement["work_code"])
        impact = _simulate(
            conn, agreement, versions_after, None,
            body.valid_from, body.valid_to, shares_map,
            old_versions=prev_versions, old_plays=prev_plays,
            target_version=new_version,
        )

    # 确认后：现行版本即新追加版本
    proposed = _proposed_payload(
        {**agreement, "current_version": new_version - 1},
        body.valid_from, body.valid_to, body.note, shares_map,
    )
    proposed["current_version"] = new_version
    proposed["proposed_version"] = new_version
    return {
        "status": "amended",
        "agreement": {
            "id": agreement_id,
            "work_code": agreement["work_code"],
            "previous_version": agreement["current_version"],
            "current_version": new_version,
        },
        **proposed,
        **impact,
    }


# ---- 版本查询（旧版可追溯） ----

def list_versions(conn, agreement_id: int):
    from .errors import APIError

    agreement = _get_agreement(conn, agreement_id)
    if agreement is None:
        raise APIError(
            404, "agreement_not_found", f"agreement {agreement_id} not found"
        )
    rows = conn.execute(
        """
        SELECT version, valid_from, valid_to, note, created_at, superseded_at
        FROM agreement_versions
        WHERE agreement_id = %s
        ORDER BY version
        """,
        (agreement_id,),
    ).fetchall()
    return {
        "agreement_id": agreement_id,
        "work_code": agreement["work_code"],
        "current_version": agreement["current_version"],
        "versions": [_version_out(conn, agreement_id, r, agreement) for r in rows],
    }


def get_version(conn, agreement_id: int, version: int):
    from .errors import APIError

    agreement = _get_agreement(conn, agreement_id)
    if agreement is None:
        raise APIError(
            404, "agreement_not_found", f"agreement {agreement_id} not found"
        )
    row = conn.execute(
        """
        SELECT version, valid_from, valid_to, note, created_at, superseded_at
        FROM agreement_versions
        WHERE agreement_id = %s AND version = %s
        """,
        (agreement_id, version),
    ).fetchone()
    if row is None:
        raise APIError(
            404,
            "agreement_version_not_found",
            f"agreement {agreement_id} version {version} not found",
        )
    return _version_out(conn, agreement_id, row, agreement)


def _version_out(conn, agreement_id, row, agreement) -> dict:
    shares = _version_shares(conn, agreement_id, row["version"])
    return {
        "version": row["version"],
        "is_current": row["version"] == agreement["current_version"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "note": row["note"],
        "created_at": row["created_at"],
        "superseded_at": row["superseded_at"],
        "shares": [
            {"holder_code": h, "percent": format(shares[h], "f")}
            for h in sorted(shares)
        ],
    }
