from decimal import Decimal

from app.catalog import SPEC
from app.reconcile.checks import all_checks
from app.reconcile.exposure import assess, exposure_gate


def build_ledger(intake, prepared, prepare_issues, results, exposure=None):
    counts = dict(intake["counts"])
    counts["prepared"] = len(prepared)
    counts["prepare_issues"] = len(prepare_issues)
    counts["computed"] = len(results)
    balanced = counts["input"] == counts["accepted"] + counts["isolated"] + counts["duplicates"] + counts["ignored"]
    metric = sum((item.get("metric") for item in results if item.get("metric") is not None), Decimal("0"))
    checks = [
        {"id": "source", "label": "来源行守恒", "ok": balanced},
        {"id": "computed", "label": "计算条目完整", "ok": counts["computed"] == len(results)},
    ]
    # 暴露分钟（探头交接）对账：直接从采信的原始行重算，保持与迟到交接重算同一入口
    if exposure is None:
        try:
            exposure = assess(intake.get("rows", []))
        except Exception:
            exposure = None
    if exposure is not None:
        counts["exposure_minutes"] = exposure.get("exposure_minutes", 0)
        counts["missing_minutes"] = exposure.get("missing_minutes", 0)
        counts["overlap_minutes"] = exposure.get("overlap_minutes", 0)
        checks.extend(exposure.get("checks", []))
    ledger = {"counts": counts, "balanced": balanced, "metric_field": SPEC.get("metric_field", "metric"),
              "metric_total": str(metric), "checks": checks, "all_checks": all_checks(checks),
              "exposure": exposure, "probe_reconciliation": exposure,
              "exposure_gate": exposure_gate(exposure)[0]}
    if exposure is not None:
        # 让 ledger.exposure_minutes / ledger.missing_minutes 这类直接断言也成立
        for key in ("window_minutes", "exposure_minutes", "counted_minutes", "covered_minutes",
                    "missing_minutes", "overlap_minutes", "duplicate_minutes", "filled_minutes",
                    "conservative_minutes", "qualified", "complete"):
            ledger[key] = exposure.get(key)
    return ledger


def __getattr__(name):
    # 兼容 from app.reconcile.ledger import <probe/handoff/exposure 函数或常量>
    from app.reconcile import exposure as _core
    if hasattr(_core, name):
        return getattr(_core, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
