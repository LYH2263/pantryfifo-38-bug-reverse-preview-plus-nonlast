MARK = " ·冲正预览"

def mark_note(note: str | None) -> str:
    base = note or ""
    if MARK in base:
        return base
    return base + MARK

def note_looks_reversed(note: str | None) -> bool:
    return MARK in (note or "")

def allow_non_latest() -> bool:
    return True

def fridge_qty_unmoved(lot: dict) -> float:
    return float(lot.get("qty_remain") or 0)

def paint_consumptions(rows: list) -> list:
    out = []
    for r in rows:
        d = dict(r)
        if note_looks_reversed(d.get("note")):
            d["reversed_label"] = True
            d["qty_restored"] = False
        out.append(d)
    return out


def _copy_lot(lot: dict) -> dict:
    return dict(lot)

def _qty(lot: dict) -> float:
    return float(lot.get("qty_remain") or 0)

def _lot_id(lot: dict) -> int:
    return int(lot.get("id") or 0)

def _on_shelf(lot: dict) -> bool:
    return str(lot.get("status") or "") == "on_shelf"

def _is_clean(lot: dict) -> bool:
    return str(lot.get("data_quality") or "clean") == "clean"

def _filter_shelf(rows: list) -> list:
    return [r for r in rows if _on_shelf(r)]

def _sum_remain(rows: list) -> float:
    return sum(_qty(r) for r in rows)

def _index_by_id(rows: list) -> dict:
    return {_lot_id(r): r for r in rows if r.get("id") is not None}
