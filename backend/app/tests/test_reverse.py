"""冲正全链路测试：履历标记 / 总表余量 / 批次状态 / 紧急条必须同一收口。

运行：cd backend && python -m pytest app/tests -q
（每个用例使用独立 DATA_DIR，不污染开发库。）
"""
import sqlite3
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import connect, db_path
from app.main import app

TODAY = date.today().isoformat()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _lot(client, lot_id: int) -> dict:
    for r in client.get("/api/fridge").json():
        if r["id"] == lot_id:
            return r
    # 在架查不到时直接读库（expired/consumed 批次）
    c = connect()
    row = dict(c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone())
    c.close()
    return row


def _hist(client) -> list[dict]:
    return client.get("/api/consumptions").json()


def _consume(client, item_id: int, qty: float):
    r = client.post("/api/consume", json={"item_id": item_id, "qty": qty})
    assert r.status_code == 200, r.text
    return r.json()


def _inbound_soon_eggs(client, qty=5):
    """鸡蛋新批：到期日 2 天后（warn_days=3 -> 紧急条 soon）。返回批次 id。"""
    expiry = date.fromordinal(date.today().toordinal() + 2).isoformat()
    r = client.post("/api/lots", json={"item_id": 2, "qty": qty, "expiry": expiry})
    assert r.status_code == 200, r.text
    return r.json()["id"], expiry


# ---------- 1. 预览：只读，不标字、不动余量、不留 reverse 行 ----------

def test_preview_is_read_only(client):
    _consume(client, 2, 3)  # 鸡蛋 lot3 12 -> 9
    before_qty = _lot(client, 3)["qty_remain"]
    rows = _hist(client)
    cid = rows[0]["id"]

    r = client.post("/api/reverse/preview", json={"consumption_id": cid})
    assert r.status_code == 200
    assert r.json()["valid"] is True

    # 总表余量不变
    assert _lot(client, 3)["qty_remain"] == before_qty
    rows = _hist(client)
    target = next(h for h in rows if h["id"] == cid)
    # 履历没有冲正字：无 reversed_at、无备注污染、无 reverse 行
    assert target["reversed_at"] is None
    assert "冲正" not in (target["note"] or "")
    assert not target.get("reversed_label")
    assert all(h["kind"] == "consume" for h in rows)

    # 预览多次仍然只读
    client.post("/api/reverse/preview", json={"consumption_id": cid})
    assert "冲正" not in (_hist(client)[0]["note"] or "")


# ---------- 2. 缺原因字：失败且余量停在冲正前，履历无字 ----------

def test_confirm_without_reason_leaves_everything(client):
    _consume(client, 2, 4)  # lot3 12 -> 8
    cid = _hist(client)[0]["id"]
    for bad in ("", "   "):
        r = client.post("/api/reverse", json={"consumption_id": cid, "reason": bad})
        assert r.status_code == 400
        assert _lot(client, 3)["qty_remain"] == 8
        t = next(h for h in _hist(client) if h["id"] == cid)
        assert t["reversed_at"] is None


# ---------- 3. 确认冲正：余量加回 + 履历标记 + reverse 行，一次落齐 ----------

def test_confirm_restores_qty_and_marks_history(client):
    res = _consume(client, 2, 4)
    assert _lot(client, 3)["qty_remain"] == 8
    cid = _hist(client)[0]["id"]

    r = client.post("/api/reverse", json={"consumption_id": cid, "reason": "  算错了 "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reversed_id"] == cid

    # 总表余量已加回
    assert _lot(client, 3)["qty_remain"] == 12

    rows = _hist(client)
    target = next(h for h in rows if h["id"] == cid)
    rev = next(h for h in rows if h["kind"] == "reverse")
    # 履历同一世界：冲正字、原因、reverse 行互相对得上
    assert target["reversed_at"] is not None
    assert target["reverse_reason"] == "算错了"
    assert target["reversal_id"] == rev["id"]
    assert target.get("reversed_label") is True
    assert target["reversible"] is False
    assert rev["reverses_id"] == cid
    assert rev["note"] == "算错了"
    assert {x["lot_id"] for x in rev["restorations"]} == {d["lot_id"] for d in res["deductions"]}

    # 不能重复冲正
    again = client.post("/api/reverse", json={"consumption_id": cid, "reason": "再冲"})
    assert again.status_code == 409
    assert "already_reversed" in again.text
    assert _lot(client, 3)["qty_remain"] == 12  # 没有二次加回


# ---------- 4. 非最近一笔不能冲正（预览与确认都拒绝） ----------

def test_only_latest_consumption_reversible(client):
    _consume(client, 2, 2)  # id=1: lot3 12 -> 10
    _consume(client, 2, 1)  # id=2: lot3 10 -> 9
    first, second = (h["id"] for h in [_hist(client)[1], _hist(client)[0]])

    # 预览只读：200 + valid:false；确认：409
    pv = client.post("/api/reverse/preview", json={"consumption_id": first})
    assert pv.status_code == 200 and pv.json()["valid"] is False
    assert pv.json()["error"] == "not_latest"
    r = client.post("/api/reverse", json={"consumption_id": first, "reason": "x"})
    assert r.status_code == 409
    assert "not_latest" in r.text
    assert _lot(client, 3)["qty_remain"] == 9

    # 最近一笔可冲；冲完后前一笔重新成为最近一笔可冲
    assert client.post("/api/reverse", json={"consumption_id": second, "reason": "b"}).status_code == 200
    assert _lot(client, 3)["qty_remain"] == 10
    assert client.post("/api/reverse", json={"consumption_id": first, "reason": "a"}).status_code == 200
    assert _lot(client, 3)["qty_remain"] == 12


def test_stale_preview_then_confirm_rejected(client):
    """预览后冒出新扣减，旧预览确认必须被拒，余量停在新扣减之后。"""
    _consume(client, 2, 2)  # id=1
    cid = _hist(client)[0]["id"]
    assert client.post("/api/reverse/preview", json={"consumption_id": cid}).json()["valid"]
    _consume(client, 2, 1)  # id=2，新最近一笔
    r = client.post("/api/reverse", json={"consumption_id": cid, "reason": "晚了"})
    assert r.status_code == 409 and "not_latest" in r.text
    assert _lot(client, 3)["qty_remain"] == 9
    assert next(h for h in _hist(client) if h["id"] == cid)["reversed_at"] is None


# ---------- 5. 已过期批次：加回不复活，总表与紧急条同一收口 ----------

def test_restore_expired_lot_stays_off_shelf_and_alerts(client):
    # lot2 牛奶 到期 2026-09-28（已过期），lot1 到期 2026-10-01（已过期）
    _consume(client, 1, 1)  # FEFO 打 lot2：qty 1->0，status consumed
    client.post("/api/expire-sweep")  # lot1（余量>0）下架 expired
    cid = _hist(client)[0]["id"]

    assert client.post("/api/reverse", json={"consumption_id": cid, "reason": "误扣"}).status_code == 200

    lot2 = _lot(client, 2)
    assert lot2["qty_remain"] == 1          # 余量确实补回
    assert lot2["status"] == "expired"      # 但不复活在架
    fridge_ids = {x["id"] for x in client.get("/api/fridge").json()}
    assert {1, 2}.isdisjoint(fridge_ids)    # 总表看不到任何过期牛奶批
    alert_ids = {a["id"] for a in client.get("/api/alerts").json()}
    assert 2 not in alert_ids               # 紧急条也不当它在架临期/过期


def test_restore_on_shelf_lot_returns_to_fridge_and_alert_bar(client):
    """未到期批次耗尽后冲正：复活在架，紧急条重新出现。"""
    lid, _ = _inbound_soon_eggs(client, 5)
    _consume(client, 2, 5)  # FEFO 先打新批（到期更早），耗尽 -> consumed
    assert _lot(client, lid)["status"] == "consumed"
    assert lid not in {a["id"] for a in client.get("/api/alerts").json()}

    cid = _hist(client)[0]["id"]
    assert client.post("/api/reverse", json={"consumption_id": cid, "reason": "退回"}).status_code == 200

    lot = _lot(client, lid)
    assert lot["qty_remain"] == 5 and lot["status"] == "on_shelf"
    assert lid in {x["id"] for x in client.get("/api/fridge").json()}
    alert = next(a for a in client.get("/api/alerts").json() if a["id"] == lid)
    assert alert["level"] == "soon"  # 紧急条按到期日重新收口


# ---------- 6. 冲正后再扣减：FEFO 按到期先后重新打到加回批 ----------

def test_new_consume_after_reverse_hits_restored_lot_fefo(client):
    lid, _ = _inbound_soon_eggs(client, 5)   # 早到期批
    _consume(client, 2, 5)                   # 耗尽 lid（lot3 不动，12）
    cid = _hist(client)[0]["id"]
    assert client.post("/api/reverse", json={"consumption_id": cid, "reason": "r"}).status_code == 200
    assert _lot(client, lid)["qty_remain"] == 5

    res = _consume(client, 2, 6)             # 重新 FEFO：先 lid×5 再 lot3×1
    takes = {(d["lot_id"], d["take"]) for d in res["deductions"]}
    assert takes == {(lid, 5), (3, 1)}
    assert _lot(client, lid)["status"] == "consumed"
    assert _lot(client, 3)["qty_remain"] == 11


def test_reverse_then_sweep_then_consume(client):
    """冲正加回的过期批被 sweep 后仍是 expired，新扣减不碰它。"""
    _consume(client, 1, 1)  # lot2 耗尽
    cid = _hist(client)[0]["id"]
    assert client.post("/api/reverse", json={"consumption_id": cid, "reason": "r"}).status_code == 200
    assert _lot(client, 2)["status"] == "expired"  # 加回即过期，无需 sweep 也一致
    client.post("/api/expire-sweep")
    assert _lot(client, 2)["status"] == "expired"
    # 牛奶在架无正余量批：FEFO 扣减应 short 失败，绝不打到 expired 批
    r = client.post("/api/consume", json={"item_id": 1, "qty": 1})
    assert r.status_code == 409
    assert _lot(client, 2)["qty_remain"] == 1


# ---------- 7. 确认冲正与新扣减竞争：谁先落，履历与余量只能对上留下的那一笔 ----------

def test_reverse_loses_race_to_newer_consume(client, tmp_path, monkeypatch):
    _consume(client, 2, 3)  # id=1：lot3 12 -> 9
    cid = _hist(client)[0]["id"]

    holder = connect()
    holder.execute("BEGIN IMMEDIATE")  # 占住写锁，模拟另一笔写事务先落
    result = {}

    def do_reverse():
        result["r"] = client.post("/api/reverse", json={"consumption_id": cid, "reason": "抢"})

    t = threading.Thread(target=do_reverse)
    t.start()
    time.sleep(0.5)  # 确认冲正事务已堵在 BEGIN IMMEDIATE
    # 持锁方落下一笔新扣减：lot3 9 -> 8，并写 consume 行 id=2
    holder.execute("UPDATE lots SET qty_remain=8 WHERE id=3")
    holder.execute(
        """INSERT INTO consumptions(kind,item_id,note,result_json,created_at)
           VALUES ('consume',2,'','{}',?)""",
        ("2026-10-06T00:00:00+00:00",))
    holder.commit()
    t.join(timeout=10)

    r = result["r"]
    assert r.status_code == 409 and "not_latest" in r.text  # 锁内重检拦住
    assert _lot(client, 3)["qty_remain"] == 8               # 没有半加回
    target = next(h for h in _hist(client) if h["id"] == cid)
    assert target["reversed_at"] is None                    # 履历无冲正字
    assert not target.get("reversed_label")
    holder.close()


def test_writers_are_serialized_by_immediate_lock(client, tmp_path):
    """写者串行化是上面竞争安全的底座：持 RESERVED 锁时第二写者立刻撞锁。"""
    holder = connect()
    holder.execute("BEGIN IMMEDIATE")
    rival = sqlite3.connect(db_path(), timeout=0)
    with pytest.raises(sqlite3.OperationalError):
        rival.execute("BEGIN IMMEDIATE")
    rival.close()
    holder.rollback()
    holder.close()
