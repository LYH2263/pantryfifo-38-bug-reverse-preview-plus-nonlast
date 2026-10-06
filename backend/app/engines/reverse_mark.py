"""冲正口径：履历标记只来自确认落库的 reversed_at，预览绝不留痕。"""


def allow_non_latest() -> bool:
    """只允许冲正最近一笔未冲正的成功消费。"""
    return False


def paint_consumptions(rows: list) -> list:
    """履历展示：已确认冲正的消费带 reversed_label 标记。

    标记唯一依据 reversed_at（确认冲正时与余量加回在同一事务写入），
    不再使用备注串里的预览字样——预览只读，不会提前标字。
    """
    out = []
    for r in rows:
        d = dict(r)
        if d.get("reversed_at"):
            d["reversed_label"] = True
        out.append(d)
    return out
