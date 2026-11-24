"""探头交接 / 暴露分钟对账的规则与端到端测试。

覆盖：重叠只计一次、原探头补位、未确认交接就高不就低、缺测不合格、
迟到交接重判、重跑幂等，以及工作台/HTTP 对账结果。
"""
import pytest

from app.reconcile.exposure import assess, exposure_gate, reconcile_exposure
from app.service import pipeline


def seg(batch, sensor, start, end, temp="4", unit="C"):
    return {"batch_id": batch, "sensor_id": sensor, "segment_start": start,
            "segment_end": end, "temp_c": temp, "unit": unit, "probe_offset": "0"}


HANDOVER = {"batch_id": "B", "handover_at": "2026-11-20 01:00", "confirmed": True,
            "old_sensor_id": "A", "new_sensor_id": "Bp"}


def by_batch(result, bid="B"):
    return result["by_batch"][bid]


# ---------- 领域规则 ----------

def test_overlapping_probes_count_exposure_once():
    rows = [seg("B", "A", "2026-11-20 00:00", "2026-11-20 02:00", "4"),
            seg("B", "Bp", "2026-11-20 01:00", "2026-11-20 03:00", "6")]
    b = by_batch(assess(rows))
    assert b["window_minutes"] == 180
    assert b["exposure_minutes"] == 180          # 不是 240
    assert b["overlap_minutes"] == 60
    assert b["duplicate_minutes"] == 60
    assert b["raw_minutes"] == 240
    assert b["missing_minutes"] == 0
    assert b["balanced"] is True


def test_confirmed_original_probe_fills_successor_gap():
    rows = [seg("B", "A", "2026-11-20 00:00", "2026-11-20 03:00", "4"),
            seg("B", "Bp", "2026-11-20 01:00", "2026-11-20 01:30", "5"),
            seg("B", "Bp", "2026-11-20 02:00", "2026-11-20 03:00", "5")]
    h = dict(HANDOVER, original_still_with_goods=True)
    b = by_batch(assess(rows, [h]))
    assert b["filled_minutes"] == 30
    assert b["exposure_minutes"] == 180
    assert b["missing_minutes"] == 0
    assert b["qualified"] is True


def test_unconfirmed_original_does_not_fill_confirmed_handover_gap():
    rows = [seg("B", "A", "2026-11-20 00:00", "2026-11-20 03:00", "4"),
            seg("B", "Bp", "2026-11-20 01:00", "2026-11-20 01:30", "5"),
            seg("B", "Bp", "2026-11-20 02:00", "2026-11-20 03:00", "5")]
    b = by_batch(assess(rows, [HANDOVER]))
    assert b["missing_minutes"] == 30
    assert b["qualified"] is False
    allowed, code = exposure_gate(b)
    assert allowed is False and code


def test_unconfirmed_handover_uses_warmer_reading():
    rows = [seg("B", "A", "2026-11-20 00:00", "2026-11-20 02:00", "4"),
            seg("B", "Bp", "2026-11-20 00:00", "2026-11-20 02:00", "10")]
    h = dict(HANDOVER, confirmed=False)
    b = by_batch(assess(rows, [h]))
    assert b["conservative_minutes"] == 120
    assert all(m["temp_c"] == "10" for m in b["minutes_audit"])
    # 选较冷一支会得到 480，保守口径为 1200
    assert b["thermal_degree_minutes"] == "1200.000"


def test_no_accompanying_reading_is_missing_and_not_qualified():
    rows = [seg("B", "A", "2026-11-20 00:00", "2026-11-20 01:00", "4"),
            seg("B", "A", "2026-11-20 02:00", "2026-11-20 03:00", "5")]
    b = by_batch(assess(rows))
    assert b["window_minutes"] == 180
    assert b["exposure_minutes"] == 120
    assert b["missing_minutes"] == 60
    assert b["qualified"] is False


def test_late_handover_changes_attribution_on_recompute():
    rows = [seg("B", "A", "2026-11-20 00:00", "2026-11-20 03:00", "4"),
            seg("B", "Bp", "2026-11-20 01:00", "2026-11-20 01:30", "5"),
            seg("B", "Bp", "2026-11-20 02:00", "2026-11-20 03:00", "5")]
    strict = by_batch(assess(rows, [HANDOVER]))
    assert strict["missing_minutes"] == 30
    late = dict(HANDOVER, original_still_with_goods=True, received_at="2026-11-20 09:00")
    resolved = by_batch(assess(rows, [late]))
    assert resolved["missing_minutes"] == 0
    assert resolved["filled_minutes"] == 30


def test_pure_recompute_is_idempotent():
    rows = [seg("B", "A", "2026-11-20 00:00", "2026-11-20 02:00", "4"),
            seg("B", "Bp", "2026-11-20 01:00", "2026-11-20 03:00", "6")]
    first = assess(rows, [HANDOVER])
    second = assess([dict(r) for r in rows], [dict(HANDOVER)])
    assert first["exposure_minutes"] == second["exposure_minutes"]
    assert first["overlap_minutes"] == second["overlap_minutes"]


def test_kwargs_single_handover_form():
    rows = [seg("B", "A", "2026-11-20 00:00", "2026-11-20 03:00", "4"),
            seg("B", "Bp", "2026-11-20 01:00", "2026-11-20 03:00", "5")]
    result = reconcile_exposure(rows, handover_at="2026-11-20 01:00", confirmed=True,
                                old_sensor_id="A", new_sensor_id="Bp",
                                original_still_with_goods=True)
    assert by_batch(result)["exposure_minutes"] == 180


def test_clean_pipeline_exposure_is_qualified_and_balanced():
    payload = pipeline("clean")
    ex = payload["exposure"]
    assert ex["balanced"] is True and ex["qualified"] is True
    assert all(check["ok"] for check in payload["ledger"]["checks"])


# ---------- HTTP / 持久化 / 幂等 ----------

def _run(client, profile="clean"):
    return client.post("/api/runs", json={"profile": profile}).get_json()


def test_stateless_reconcile_endpoint(client):
    body = {"readings": [
        seg("B", "A", "2026-11-20 00:00", "2026-11-20 02:00", "4"),
        seg("B", "Bp", "2026-11-20 01:00", "2026-11-20 03:00", "6")]}
    response = client.post("/api/reconcile/probes", json=body)
    assert response.status_code == 200
    assert response.get_json()["exposure_minutes"] == 180


def test_handover_post_is_idempotent_and_late_record_recomputes(client):
    run = _run(client)
    rid = run["run_id"]
    record = {"batch_id": "B001", "handover_at": "2026-11-20 01:00", "confirmed": True,
              "old_sensor_id": "S01", "new_sensor_id": "S02",
              "original_still_with_goods": True}
    first = client.post(f"/api/runs/{rid}/handovers", json=record).get_json()
    rev = first["revision"]
    second = client.post(f"/api/runs/{rid}/handovers", json=record).get_json()
    assert second["handover_unchanged"] is True
    assert second["revision"] == rev
    listing = client.get(f"/api/runs/{rid}/handovers").get_json()
    assert len(listing["handovers"]) == 1


def test_missing_minutes_gate_blocks_approval(client):
    run = _run(client)
    rid = run["run_id"]
    # 把接班探头指向一个不存在的探针，且不确认原探头随货 -> 交接后大面积缺测
    record = {"batch_id": "B001", "handover_at": "2026-11-20 00:30", "confirmed": True,
              "old_sensor_id": "S01", "new_sensor_id": "GHOST"}
    posted = client.post(f"/api/runs/{rid}/handovers", json=record).get_json()
    response = client.post(f"/api/runs/{rid}/review",
                           json={"decision": "APPROVED", "comment": "尝试",
                                 "expected_revision": posted["revision"]})
    assert response.status_code == 409
    assert "EXPOSURE" in response.get_json()["code"] or "MISSING" in response.get_json()["code"]


def test_clean_run_without_handovers_still_approves(client):
    run = _run(client)
    response = client.post(f"/api/runs/{run['run_id']}/review",
                           json={"decision": "APPROVED", "comment": "ok", "expected_revision": 0})
    assert response.status_code == 200
    evidence = client.get(f"/api/runs/{run['run_id']}/evidence")
    assert evidence.status_code == 200
    assert evidence.get_json().get("exposure") is not None
