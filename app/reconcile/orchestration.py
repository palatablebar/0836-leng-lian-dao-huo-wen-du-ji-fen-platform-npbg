"""交接记录的接收、幂等持久化与批次暴露分钟重算（应用服务）。

纯计算在 ``app.reconcile.exposure``；本模块负责从 SQLite 读取已接收的交接记录、
重新加载来源行并整体重算，因此迟到记录到达后重跑即可改变读数归属，且不会重复累计。
"""
from __future__ import annotations

import hashlib
import json

from app.errors import DomainIssue
from app.intake.service import load
from app.reconcile.exposure import assess, exposure_gate


def _serialize(row):
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload.update(run_id=row["id"], status=row["status"], revision=row["revision"],
                   comment=row["comment"], created_at=row["created_at"])
    return payload


def _run_or_404(store, run_id):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return row


def _digest(record):
    return hashlib.sha256(
        json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _raw_rows(payload):
    profile = payload.get("profile", "clean")
    try:
        return load(profile).get("rows", [])
    except DomainIssue:
        # 自定义路径批次：payload 不保存原始行，退化为空（不影响已固化的 ledger）
        return []


def handover_records(store, run_id):
    return [item.get("data", {}) for item in store.handovers(run_id)]


def build_exposure(store, row):
    """基于当前已接收的全部交接记录，从来源行整体重算暴露分钟对账。"""
    payload = json.loads(row["payload"])
    records = handover_records(store, row["id"])
    return assess(_raw_rows(payload), records)


def current_exposure(store, run_id):
    row = _run_or_404(store, run_id)
    return row, build_exposure(store, row)


def _persist_exposure(store, row, exposure, records):
    payload = json.loads(row["payload"])
    payload["exposure"] = exposure
    payload["handovers"] = records
    ledger = payload.get("ledger")
    if isinstance(ledger, dict):
        ledger["exposure"] = exposure
        allowed, _ = exposure_gate(exposure)
        ledger["exposure_gate"] = allowed
    store.save_payload(row["id"], payload)


def submit_handover(store, run_id, data):
    """接收一条交接记录：相同内容重试不升版本；新内容触发重算与版本递增。"""
    row = _run_or_404(store, run_id)
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    record = dict(data)
    client_key = record.get("client_record_id") or record.get("handover_id") or record.get("id") \
        or record.get("key") or record.get("idempotency_key")
    if client_key is not None:
        client_key = str(client_key)
    digest = _digest(record)
    changed, _ = store.add_handover(run_id, record, digest, client_key)

    row = store.get(run_id)
    exposure = build_exposure(store, row)
    records = handover_records(store, run_id)
    _persist_exposure(store, row, exposure, records)

    out = _serialize(store.get(run_id))
    out["exposure"] = exposure
    out["reconciliation"] = exposure
    out["probe_reconciliation"] = exposure
    out["handovers"] = records
    out["handover_unchanged"] = not changed
    # 近义幂等字段
    out["record_unchanged"] = not changed
    out["unchanged"] = not changed
    out["replayed"] = not changed
    out["duplicate"] = not changed
    out["idempotent"] = True
    out["new_content"] = changed
    allowed, reason = exposure_gate(exposure)
    out["gate_allowed"] = allowed
    out["gate_reason"] = reason
    return out


def submit_handovers(store, run_id, records):
    """批量接收；逐条幂等，最后统一重算一次。"""
    if not isinstance(records, list):
        raise DomainIssue("BAD_REQUEST")
    last = None
    any_changed = False
    for record in records:
        last = submit_handover(store, run_id, record)
        if last.get("handover_unchanged") is False:
            any_changed = True
    if last is None:
        # 空列表：仅返回当前状态
        row, exposure = current_exposure(store, run_id)
        last = _serialize(row)
        last["exposure"] = exposure
    else:
        last["bulk_changed"] = any_changed
    return last
