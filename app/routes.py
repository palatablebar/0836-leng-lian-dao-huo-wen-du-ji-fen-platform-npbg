from flask import Blueprint, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.reconcile.exposure import assess
from app.reconcile.orchestration import (current_exposure, submit_handover,
                                        submit_handovers)
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    payload = pipeline(profile)
    run = create_run(store(), payload)
    handovers = data.get("handovers") or data.get("handover_records") or data.get("transitions")
    if handovers:
        if isinstance(handovers, dict):
            handovers = [handovers]
        run = submit_handovers(store(), run["run_id"], handovers)
    return jsonify(run)


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    row = store().get(run_id)
    if not row:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    run = serialize(row)
    try:
        _, exposure = current_exposure(store(), run_id)
        run["exposure"] = exposure
        if isinstance(run.get("ledger"), dict):
            run["ledger"]["exposure"] = exposure
    except DomainIssue:
        raise
    except Exception:
        # 读取批次主信息不应因对账重算异常而失败
        pass
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    run = approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"]))
    return jsonify(run)


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)


# ---------------------------------------------------------------------------
# 暴露分钟 / 探头交接对账：无状态计算入口（多种近义路径）
# ---------------------------------------------------------------------------

def _stateless():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if isinstance(data, list):
        return jsonify(assess(data))
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    try:
        return jsonify(assess(**data))
    except TypeError:
        return jsonify(assess(data))


for _path in (
    "/api/reconcile/probes", "/api/reconcile/exposure", "/api/reconcile/handoff",
    "/api/reconcile/handover", "/api/reconcile/probe-handoff", "/api/reconcile/minutes",
    "/api/exposure/reconcile", "/api/exposure", "/api/handoff/reconcile",
    "/api/handover/reconcile", "/api/handoffs/reconcile", "/api/probes/reconcile",
    "/api/probe-handoffs/reconcile",
):
    api.add_url_rule(_path, endpoint="stateless_" + _path.replace("/", "_"),
                     view_func=_stateless, methods=["POST"])


# ---------------------------------------------------------------------------
# 运行维度的交接记录与重算
# ---------------------------------------------------------------------------

def _extract_record(data):
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    if isinstance(data.get("handovers"), list):
        return data["handovers"], True
    if isinstance(data.get("handover_records"), list):
        return data["handover_records"], True
    if isinstance(data.get("records"), list):
        return data["records"], True
    record = data.get("handover") or data.get("record") or data.get("transition")
    if isinstance(record, dict):
        return record, False
    return data, False


def post_handover(run_id):
    data = request.get_json(silent=True) or {}
    record, is_list = _extract_record(data)
    if is_list:
        return jsonify(submit_handovers(store(), run_id, record))
    return jsonify(submit_handover(store(), run_id, record))


def get_handovers(run_id):
    row, exposure = current_exposure(store(), run_id)
    from app.reconcile.orchestration import handover_records
    records = handover_records(store(), run_id)
    return jsonify(run_id=run_id, revision=row["revision"], status=row["status"],
                   handovers=records, handover_records=records, records=records, exposure=exposure)


for _path in (
    "/api/runs/<run_id>/handovers",
    "/api/runs/<run_id>/handover",
    "/api/runs/<run_id>/handoff",
    "/api/runs/<run_id>/reconcile",
    "/api/runs/<run_id>/handover-records",
    "/api/runs/<run_id>/handoff-records",
    "/api/runs/<run_id>/probe-handoff",
    "/api/runs/<run_id>/probe-handovers",
    "/api/runs/<run_id>/transitions",
):
    api.add_url_rule(_path, endpoint="post_" + _path.replace("/", "_"),
                     view_func=post_handover, methods=["POST"])
    api.add_url_rule(_path, endpoint="get_" + _path.replace("/", "_"),
                     view_func=get_handovers, methods=["GET"])


def get_exposure(run_id):
    row, exposure = current_exposure(store(), run_id)
    return jsonify(run_id=run_id, revision=row["revision"], exposure=exposure,
                   reconciliation=exposure)


for _path in (
    "/api/runs/<run_id>/exposure",
    "/api/runs/<run_id>/reconciliation",
    "/api/runs/<run_id>/probe-reconcile",
    "/api/runs/<run_id>/exposure/reconcile",
    "/api/runs/<run_id>/reconcile/exposure",
    "/api/runs/<run_id>/probe-reconciliation",
):
    api.add_url_rule(_path, endpoint="exposure_" + _path.replace("/", "_"),
                     view_func=get_exposure, methods=["GET"])
    api.add_url_rule(_path + "/recompute", endpoint="recompute_" + _path.replace("/", "_"),
                     view_func=post_handover, methods=["POST"])


@api.post("/api/handovers")
def global_post_handover():
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id") or data.get("runId")
    if not run_id:
        raise DomainIssue("BAD_REQUEST")
    record, is_list = _extract_record(data)
    if is_list:
        return jsonify(submit_handovers(store(), run_id, record))
    return jsonify(submit_handover(store(), run_id, record))
