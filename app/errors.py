from flask import jsonify


MESSAGES = {
    "EMPTY_FILE": "文件为空",
    "BLANK_FILE": "文件只有空白",
    "HEADER_ONLY": "文件只有表头",
    "HEADER_MISMATCH": "表头与本项目字段不一致",
    "UNCLOSED_QUOTE": "引号未闭合，文件已拒收",
    "TOO_MANY_ROWS": "数据行超过上限",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "KEY_MISSING": "主键为空",
    "CONFLICT_DUPLICATE": "同一键存在冲突内容",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "阶段不存在",
    "RUN_NOT_FOUND": "批次不存在",
    "STALE_REVIEW": "批次已被更新，请刷新后复核",
    "GATE_BLOCKED": "存在阻断项，不能签发",
    "NOT_APPROVED": "批次尚未签发",
    "BAD_REQUEST": "请求内容不完整",
    "EXPOSURE_MISSING_MINUTES": "存在无随货读数的缺测分钟，暴露分钟未达合格口径",
    "MISSING_MINUTES": "存在缺测分钟，不算合格",
    "EXPOSURE_INCOMPLETE": "探头覆盖不完整，存在缺测分钟",
    "EXPOSURE_LEDGER_UNBALANCED": "暴露分钟对账不平衡",
    "HANDOVER_BAD_RECORD": "交接记录无法解析",
    "HANDOVER_UNKNOWN_BATCH": "交接记录中的批次或探头不属于本次运行",
}


class DomainIssue(Exception):
    def __init__(self, code: str, status: int = 400, detail: str | None = None):
        self.code = code
        self.status = status
        self.detail = detail
        self.codes = [code]
        super().__init__(code)


def error_response(exc: DomainIssue):
    body = {"error": exc.detail or MESSAGES.get(exc.code, "请求无法处理"), "code": exc.code}
    codes = getattr(exc, "codes", None)
    if codes:
        body["codes"] = list(dict.fromkeys([exc.code, *codes]))
    return jsonify(body), exc.status
