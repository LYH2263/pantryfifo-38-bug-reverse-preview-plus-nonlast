"""FEFO consume: earliest expiry first among positive remaining lots."""

def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if float(l.get("qty_remain", 0)) > 0],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry <= today.

    `<=`（而非 `<`）与 /api/alerts 的到期判定保持一致：当日到期在全系统
    * 统一为 expired，不会出现"顶条报过期、批次却还在架"的矛盾。
    """
    out = []
    for l in lots:
        exp = l.get("expiry")
        if exp and exp <= today and float(l.get("qty_remain", 0)) > 0:
            out.append(l["id"])
    return out


def restored_status(status_now: str, expiry: str | None, today: str) -> str:
    """冲正加回后批次应处的状态——与 alerts/sweep 同一收口口径。

    - 到期日 <= 今天（与 expire_lots / /api/alerts 同界）-> expired：
      余量补回但不复活回架，总表（仅在架）看不到、紧急条也不会再报；
    - 其余（含 expiry 为 NULL，NULL 批次全系统不可能过期）-> on_shelf。
    status_now 不参与判定：是否在架只由"加回后余量 >0 且未到期"决定，
    避免出现总表状态与紧急条/履历对不上的两个世界。
    """
    if expiry is not None and expiry <= today:
        return "expired"
    return "on_shelf"


def plan_restore(deductions: list[dict], lots_now: list[dict], today: str) -> dict:
    """纯函数：根据一笔消费的 deductions 与批次*当前*快照计算冲正加回计划。

    lots_now 元素需含 id/qty_remain/status/expiry。
    返回 {ok, reason, restorations:[{lot_id, expiry, status_before,
    qty_before, take, qty_after, status_after}]}。
    """
    by_id = {l["id"]: l for l in lots_now}
    out = []
    for d in deductions:
        lot = by_id.get(d["lot_id"])
        if lot is None:
            return {"ok": False, "reason": "lot_missing", "restorations": []}
        qty_before = float(lot["qty_remain"])
        take = float(d["take"])
        qty_after = round(qty_before + take, 3)
        if qty_after <= 1e-9:
            return {"ok": False, "reason": "inconsistent_qty", "restorations": []}
        out.append({
            "lot_id": lot["id"],
            "expiry": lot.get("expiry"),
            "status_before": lot["status"],
            "qty_before": qty_before,
            "take": take,
            "qty_after": qty_after,
            "status_after": restored_status(lot["status"], lot.get("expiry"), today),
        })
    return {"ok": True, "reason": "", "restorations": out}
