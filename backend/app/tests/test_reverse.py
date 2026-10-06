"""冲正端到端：预览只读、仅最近一笔、原因必填、加回后同一世界。

"同一世界"= /fridge 总表、/alerts 紧急条、FEFO 候选三处都只认
status='on_shelf'，加回统一复活在架，过期由 expiry 落紧急条并被
FEFO 优先扣减。
"""
import json
import random
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

TODAY = date.today().isoformat()
FAR = "2099-01-01"
ITEM_EGG = 2  # 种子数据：鸡蛋，唯一在架正余量 lot 到期 2026-11-01


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import app
    with TestClient(app) as cl:
        yield cl


def lot(client, lot_id):
    for r in client.get("/api/fridge").json():
        if r["id"] == lot_id:
            return r
    return None


def add_lot(client, item_id, qty, expiry):
    return client.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": expiry}).json()["id"]


def consume(client, qty, note=""):
    return client.post("/api/consume", json={"item_id": ITEM_EGG, "qty": qty, "note": note}).json()


def history(client):
    return client.get("/api/consumptions").json()


# ---------- 预览：只读，不盖戳，余量不动 ----------

def test_preview_is_read_only_and_idempotent(client):
    lid = add_lot(client, ITEM_EGG, 4, TODAY)
    consume(client, 2, note="晚饭")
    cid = history(client)[0]["id"]

    before = lot(client, lid)["qty_remain"]
    for _ in range(3):
        r = client.post("/api/reverse/preview", json={"consumption_id": cid}).json()
        assert r["valid"] is True
        assert [(x["lot_id"], x["take"], x["qty_after"], x["status_after"])
                for x in r["restorations"]] == [(lid, 2.0, 4.0, "on_shelf")]
        # 预览多少次，总表余量都停在冲正前
        assert lot(client, lid)["qty_remain"] == before

    h = history(client)[0]
    assert h["note"] == "晚饭"          # 没有"·冲正预览"脏戳
    assert h["reversed_at"] is None
    assert h["reversible"] is True


# ---------- 原因必填：失败后余量停在冲正前 ----------

def test_reason_required_leaves_qty_untouched(client):
    lid = add_lot(client, ITEM_EGG, 4, TODAY)
    consume(client, 2)
    cid = history(client)[0]["id"]
    client.post("/api/reverse/preview", json={"consumption_id": cid})
    assert lot(client, lid)["qty_remain"] == 2

    for bad in ("", "   "):
        resp = client.post("/api/reverse", json={"consumption_id": cid, "reason": bad})
        assert resp.status_code == 400
        assert lot(client, lid)["qty_remain"] == 2          # 余量停在冲正前
        assert all(r["kind"] != "reverse" for r in history(client))
        assert history(client)[0]["reversed_at"] is None    # 履历也没冲正字

    ok = client.post("/api/reverse", json={"consumption_id": cid, "reason": "误扣"})
    assert ok.status_code == 200
    assert lot(client, lid)["qty_remain"] == 4


# ---------- 只有最近一笔成功扣减可冲 ----------

def test_only_latest_consumption_is_reversible(client):
    lid = add_lot(client, ITEM_EGG, 6, FAR)  # 到期远，种子 lot(11-01) 更早先吃
    consume(client, 1, note="第一笔")
    consume(client, 1, note="第二笔")
    hs0 = history(client)
    id2, id1 = hs0[0]["id"], hs0[1]["id"]

    hs = history(client)
    assert [h["reversible"] for h in hs if h["kind"] == "consume"] == [True, False]

    pv = client.post("/api/reverse/preview", json={"consumption_id": id1}).json()
    assert pv["valid"] is False and pv["error"] == "not_latest"
    cf = client.post("/api/reverse", json={"consumption_id": id1, "reason": "x"})
    assert cf.status_code == 409 and cf.json()["detail"] == "not_latest"

    # 冲掉第二笔后，第一笔成为最近一笔可冲（reverse 行不占位）
    assert client.post("/api/reverse", json={"consumption_id": id2, "reason": "r2"}).status_code == 200
    assert history(client)[0]["kind"] == "reverse"
    first_consume = next(h for h in history(client) if h["id"] == id1)
    assert first_consume["reversible"] is True
    assert client.post("/api/reverse", json={"consumption_id": id1, "reason": "r1"}).status_code == 200


def test_reverse_row_is_never_a_target(client):
    add_lot(client, ITEM_EGG, 3, TODAY)
    consume(client, 1)
    cid = history(client)[0]["id"]
    client.post("/api/reverse", json={"consumption_id": cid, "reason": "x"})
    rid = history(client)[0]["id"]
    resp = client.post("/api/reverse/preview", json={"consumption_id": rid})
    assert resp.status_code == 409 and resp.json()["detail"] == "not_a_consumption"


def test_double_confirm_rejected(client):
    add_lot(client, ITEM_EGG, 3, TODAY)
    consume(client, 1)
    cid = history(client)[0]["id"]
    body = {"consumption_id": cid, "reason": "误扣"}
    assert client.post("/api/reverse", json=body).status_code == 200
    second = client.post("/api/reverse", json=body)
    assert second.status_code == 409 and second.json()["detail"] == "already_reversed"


# ---------- 加回后：余量、状态、紧急条同一世界 ----------

def test_restore_revives_swept_lot_then_alerts_and_fefo_align(client):
    # lot E 今天到期：消费 1 → 收走过期 → 冲正加回 → 紧急条标过期 → 新扣减先打它
    e = add_lot(client, ITEM_EGG, 2, TODAY)
    res = consume(client, 1, note="早午")
    assert res["deductions"][0]["lot_id"] == e
    assert lot(client, e)["qty_remain"] == 1

    assert client.post("/api/expire-sweep").json()["expired_ids"].count(e) == 1
    assert lot(client, e) is None                      # 收走后总表不可见
    assert all(a["id"] != e for a in client.get("/api/alerts").json())

    cid = history(client)[0]["id"]
    pv = client.post("/api/reverse/preview", json={"consumption_id": cid}).json()
    assert pv["valid"] is True
    rst = next(x for x in pv["restorations"] if x["lot_id"] == e)
    assert (rst["qty_before"], rst["take"], rst["status_before"], rst["status_after"]) == (
        1.0, 1.0, "expired", "on_shelf")

    assert client.post("/api/reverse", json={"consumption_id": cid, "reason": "误收"}).status_code == 200

    # 同一世界：总表在架、余量 2；紧急条同时把它当过期
    on_shelf = lot(client, e)
    assert on_shelf is not None and on_shelf["qty_remain"] == 2 and on_shelf["status"] == "on_shelf"
    alert = next(a for a in client.get("/api/alerts").json() if a["id"] == e)
    assert alert["level"] == "expired"

    # 新一笔扣减按到期先后，最先打回复活的到期批
    again = consume(client, 1, note="又一笔")
    assert again["deductions"][0]["lot_id"] == e
    assert lot(client, e)["qty_remain"] == 1


def test_history_marks_match_qty_after_confirm(client):
    """履历冲正标记与总表余量必须同时落地（预览不落地，确认才一起落）。"""
    lid = add_lot(client, ITEM_EGG, 3, TODAY)
    consume(client, 2)
    cid = history(client)[0]["id"]

    # 预览后：无标记、无加回
    client.post("/api/reverse/preview", json={"consumption_id": cid})
    assert history(client)[0]["reversed_at"] is None
    assert lot(client, lid)["qty_remain"] == 1

    client.post("/api/reverse", json={"consumption_id": cid, "reason": "错扣"})
    hs = history(client)
    rev = hs[0]
    done = next(h for h in hs if h["id"] == cid)
    assert rev["kind"] == "reverse" and rev["reverses_id"] == cid and rev["note"] == "错扣"
    assert done["reversed_at"] == rev["created_at"]
    assert done["reversal_id"] == rev["id"] and done["reverse_reason"] == "错扣"
    assert lot(client, lid)["qty_remain"] == 3          # 标记与加回一起可见


# ---------- 冲正确认与新扣减并发：谁先落都自洽 ----------

def test_concurrent_consume_and_reverse_keep_ledger_consistent(client, tmp_path):
    from app.main import consume as ep_consume, reverse as ep_reverse, ConsumeIn, ReverseIn
    from app.db import connect

    # 初始库存（含全部种子 lot），用于守恒校验
    c = connect()
    initial_in = c.execute("SELECT COALESCE(SUM(qty_in),0) s FROM lots").fetchone()["s"]
    c.close()

    errors = []
    barrier = threading.Barrier(2)
    rng = random.Random(42)
    # 鸡蛋种子库存 12：消费与冲正互相喂库存，交错时双方都能成多笔。
    def consumer():
        barrier.wait()
        for i in range(30):
            time.sleep(rng.random() * 0.003)
            try:
                ep_consume(ConsumeIn(item_id=ITEM_EGG, qty=1, note=f"c{i}"))
            except Exception as e:
                if getattr(e, "detail", None) != "short" and "short" not in str(e):
                    errors.append(e)

    def reverser():
        barrier.wait()
        for i in range(30):
            time.sleep(rng.random() * 0.003)
            try:
                ep_reverse(ReverseIn(reason=f"r{i}"))
            except Exception as e:
                detail = getattr(e, "detail", str(e))
                if detail not in ("no_consumption", "not_latest", "already_reversed"):
                    errors.append(e)

    t1, t2 = threading.Thread(target=consumer), threading.Thread(target=reverser)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors

    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM consumptions ORDER BY id")]
    remain_sum = c.execute("SELECT COALESCE(SUM(qty_remain),0) s FROM lots").fetchone()["s"]
    neg = c.execute(
        "SELECT COUNT(*) n FROM lots WHERE qty_remain < 0 AND status='on_shelf' "
        "AND COALESCE(data_quality,'clean')='clean'").fetchone()["n"]
    c.close()

    taken = restored = 0.0
    rev_targets, reversed_marks = [], []
    for r in rows:
        payload = json.loads(r["result_json"] or "{}")
        if r["kind"] == "reverse":
            restored += sum(float(d["take"]) for d in payload.get("deductions") or [])
            rev_targets.append(r["reverses_id"])
        else:
            taken += sum(float(d["take"]) for d in payload.get("deductions") or [])
            if r["reversed_at"] is not None:
                reversed_marks.append(r["id"])

    # 余量守恒：库里余量 = 入库总量 - 成功扣减 + 成功冲正加回
    assert abs(remain_sum - (initial_in - taken + restored)) < 1e-6
    assert neg == 0
    # 配对收口：每条冲正笔唯一对应一个被冲消费，标记与加回永不分家
    assert sorted(rev_targets) == sorted(reversed_marks)
    assert len(rev_targets) == len(set(rev_targets))
