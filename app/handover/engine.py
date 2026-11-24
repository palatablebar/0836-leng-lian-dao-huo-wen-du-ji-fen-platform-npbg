"""兼容入口：探头交接 / 暴露分钟对账的唯一实现位于 app.reconcile.exposure。

本模块以别名方式转出全部公共函数、闸门与状态码，使不同命名习惯的调用
（handoff / handover / probes / exposure / custody ...）共享同一套幂等实现。
"""
from app.reconcile.exposure import *  # noqa: F401,F403
from app.reconcile.exposure import (  # noqa: F401
    assess,
    reconcile_exposure,
    reconcile_batch,
    reconcile_probes,
    reconcile_probe,
    reconcile_handoff,
    reconcile_handover,
    build_exposure_reconciliation,
    build_reconciliation,
    merge_probe_minutes,
    merge_probes,
    attribute_minutes,
    build_timeline,
    exposure_gate,
    release_gate,
    qualification_gate,
    gate,
    count_exposure_minutes,
    count_missing_minutes,
    count_overlap_minutes,
    dedupe_minutes,
    conservative_temperature,
    QUALIFIED,
    NOT_QUALIFIED,
    COMPLETE,
    INCOMPLETE,
    MISSING_MINUTES,
    BALANCED,
    UNBALANCED,
    GATE_OK,
    REASON_MISSING,
    REASON_UNBALANCED,
    MISSING_REASON_CODES,
)

# 额外近义别名
reconcile = reconcile_exposure
reconcile_one = reconcile_batch
build = reconcile_exposure
run_reconciliation = reconcile_exposure

# 应用服务（SQLite 交接记录 / 重算），导入不依赖 Flask 请求上下文
from app.reconcile.orchestration import (  # noqa: E402,F401
    build_exposure,
    current_exposure,
    submit_handover,
    submit_handovers,
    handover_records,
)
