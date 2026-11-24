# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

探头交接 / 暴露分钟对账（同一处理器提供 handoff、handover、probes、exposure、reconciliation 等近义路径）：

- `POST /api/reconcile/probes`（以及 `/api/exposure/reconcile`、`/api/handoff/reconcile` 等）：无状态入参 `readings` + `handovers`，返回分钟对账。
- `POST /api/runs/{id}/handovers`（单数 `handover`/`handoff`/`reconcile` 等别名同义）：登记一条交接记录并整体重算；相同内容重试返回 `handover_unchanged=true` 且不升版本。
- `GET /api/runs/{id}/handovers`：交接记录与当前对账。
- `GET /api/runs/{id}/exposure`（别名 `/reconciliation`、`/probe-reconciliation`）：暴露分钟对账。
- `POST /api/handovers`：请求体带 `run_id` 的全局登记入口。

