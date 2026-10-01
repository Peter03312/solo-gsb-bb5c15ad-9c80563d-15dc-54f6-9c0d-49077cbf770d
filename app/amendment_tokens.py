"""协议修订预览令牌（无状态乐观并发凭证）。

预览（preview）不改任何数据，只把"所见协议版本 + 受影响数据的状态指纹 +
拟提交内容"签名为令牌；确认（confirm）时重验签名、内容与状态指纹：
预览后该作品的现行协议、相关播放更正链或相关期间单据若已变化，
指纹不一致即拒绝（amendment_preview_stale），且不修改任何数据。

令牌 = base64url(payload_json) + "." + base64url(HMAC-SHA256(payload))
payload 明文可读，仅防篡改、不承担保密。
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal

from .config import settings

_TOKEN_VERSION = 1


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def _sign(payload_b64: str) -> str:
    mac = hmac.new(
        settings.amendment_token_secret.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    )
    return _b64e(mac.digest())


def issue_token(payload: dict) -> str:
    body = dict(payload)
    body["token_version"] = _TOKEN_VERSION
    body["previewed_at"] = datetime.now(timezone.utc).isoformat()
    payload_b64 = _b64e(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    )
    return payload_b64 + "." + _sign(payload_b64)


def verify_token(token: str) -> dict:
    """验签并返回 payload；令牌格式错误、被篡改均抛 ValueError。"""
    if not token or token.count(".") != 1:
        raise ValueError("malformed preview token")
    payload_b64, sig = token.split(".", 1)
    if not hmac.compare_digest(sig, _sign(payload_b64)):
        raise ValueError("preview token signature mismatch")
    try:
        return json.loads(_b64d(payload_b64))
    except Exception as exc:
        raise ValueError("malformed preview token payload") from exc


def fingerprint(
    agreements: list[dict],
    plays: list[dict],
    periods: list[dict],
) -> str:
    """对"预览所见状态"生成稳定指纹。

    - agreements：该作品全部协议的 (编号, 现行版本号, 现行区间, 现行比例)，
      覆盖"该作品的现行协议"变化（任何一份协议修订都会改变指纹）；
    - plays：该作品全部播放记录（import_id, 发生时间, 金额, corrects_id），
      覆盖"相关播放更正链"变化（导入新播放、任何更正都改变指纹）；
    - periods：与该作品链尾播放相关的已出单期间快照：
      (期间, 最新单据 id, 逐行已入账累计, 最新待处理清单)，
      覆盖"相关期间单据"变化（任何形式的再出具都会改变指纹）。
    百分比统一量化为 numeric(8,4) 字符串，时间统一 UTC，保证跨进程稳定。
    """

    def norm_pct(v) -> str:
        return format(Decimal(str(v)).quantize(Decimal("0.0001")), "f")

    def norm_dt(v) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        return v.astimezone(timezone.utc).isoformat()

    agr = [
        {
            "id": a["id"],
            "version": a["version"],
            "valid_from": norm_dt(a["valid_from"]),
            "valid_to": norm_dt(a["valid_to"]),
            "shares": {h: norm_pct(p) for h, p in sorted(a["shares"].items())},
        }
        for a in agreements
    ]
    pls = [
        {
            "import_id": p["import_id"],
            "occurred_at": norm_dt(p["occurred_at"]),
            "amount": int(p["amount"]),
            "corrects_id": p["corrects_id"],
        }
        for p in plays
    ]
    pers = [
        {
            "start": norm_dt(pe["start"]),
            "end": norm_dt(pe["end"]),
            "latest_batch_id": pe["latest_batch_id"],
            "lines": {h: int(a) for h, a in sorted(pe["lines"].items())},
            "pending": sorted(
                (
                    {
                        "import_id": x["import_id"],
                        "amount": int(x["amount"]),
                    }
                    for x in pe["pending"]
                ),
                key=lambda x: x["import_id"],
            ),
        }
        for pe in periods
    ]
    canonical = json.dumps(
        {"agreements": agr, "plays": pls, "periods": pers},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
