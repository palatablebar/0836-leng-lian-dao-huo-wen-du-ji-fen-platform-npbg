import json

from app.errors import DomainIssue
from app.reconcile.exposure import exposure_gate
from app.reconcile.orchestration import build_exposure
from app.review.policy import approval_allowed


def create_run(store, payload):
    row = store.create(payload["run_key"], payload["profile"], payload)
    return serialize(row)


def approve(store, run_id, decision, comment, expected_revision):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    if decision == "APPROVED" and not approval_allowed(payload):
        raise DomainIssue("GATE_BLOCKED", 409)
    exposure = None
    if decision == "APPROVED":
        # 暴露分钟闸门：存在缺测或对账不平衡时“不算合格”，不能签发
        exposure = build_exposure(store, row)
        allowed, reason = exposure_gate(exposure)
        if not allowed:
            issue = DomainIssue(reason, 409)
            issue.codes = list(exposure.get("reasons", [])) or [reason]
            raise issue
    if decision not in {"APPROVED", "HELD"}:
        raise DomainIssue("BAD_REQUEST")
    row, problem = store.review(run_id, decision, comment.strip(), expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    out = serialize(row)
    if exposure is not None:
        out["exposure"] = exposure
    return out


def serialize(row):
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload.update(run_id=row["id"], status=row["status"], revision=row["revision"],
                   comment=row["comment"], created_at=row["created_at"])
    return payload
