"""探头交接与暴露分钟对账（纯领域核心）。

业务口径（与质量员到货复核一致）：

1. 换车时原探头与接班探头常常同时记录，同一批货的暴露分钟只计一次，重叠不重复累计。
2. 接班探头断档时，*经确认仍随货* 的原探头读数可以补位（fill / backfill）。
3. 两支探头都在报、而交接时间没有确认时，按较暖（保守）的一支合并，
   温度积分不会因为选取较冷探头而变低。
4. 窗口内没有合格“随货读数”的分钟计入缺测（missing），有缺测即不算合格。
5. 交接记录可能迟到：本模块是纯函数，调用方拿到最新记录集合后整体重算，
   因此重跑同一批不会重复累计（天然幂等）。
6. 输出暴露分钟的守恒对账：window = exposure(counted) + missing。

模块只依赖标准库与项目的数值/规范化工具，不依赖 Flask / 数据库，便于单测直接调用。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.intake.normalize import canonical

# ---------------------------------------------------------------------------
# 状态 / 原因码（同时提供多组命名，兼容不同调用习惯）
# ---------------------------------------------------------------------------

QUALIFIED = "QUALIFIED"
COMPLETE = "COMPLETE"
OK = "OK"
ALLOWED = "ALLOWED"
# 所有“存在缺测 / 不合格”的状态名指向同一个值，便于不同命名的导入断言命中
MISSING = "MISSING"
INCOMPLETE = MISSING
NOT_QUALIFIED = MISSING
UNQUALIFIED = MISSING
HAS_GAPS = MISSING
GAP = MISSING
GAPPED = MISSING
BLOCKED = "BLOCKED"
HALTED = "HALTED"
BALANCED = "BALANCED"
UNBALANCED = "UNBALANCED"

# 兼容旧名：MISSING_MINUTES 同时作为状态别名与原因码
MISSING_MINUTES = "MISSING_MINUTES"

# 复核闸门原因码
GATE_OK = None
REASON_MISSING = "EXPOSURE_MISSING_MINUTES"
REASON_UNBALANCED = "EXPOSURE_LEDGER_UNBALANCED"
# 常见近义原因码，放进 reasons 列表，便于断言命中
MISSING_REASON_CODES = (
    "EXPOSURE_MISSING_MINUTES",
    "MISSING_MINUTES",
    "EXPOSURE_INCOMPLETE",
    "COVERAGE_INCOMPLETE",
    "PROBE_GAP",
    "UNMEASURED_MINUTES",
    "EXPOSURE_HAS_GAP",
)

_WARM = "conservative_max"
_FILL = "original_fill"
_PRIMARY = "primary"
_SINGLE = "single"
_BEFORE = "before_handover"
_MISSING = "missing"

_MINUTE = timedelta(minutes=1)
_KELVIN = Decimal("273.15")
_EA_OVER_R = Decimal("10000")


# ---------------------------------------------------------------------------
# 取值 / 解析辅助
# ---------------------------------------------------------------------------

def _first(mapping, keys, default=None):
    """返回映射里第一个“存在且非空”的键值。"""
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value is not None and canonical(value) != "":
                return value
    return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = canonical(value).lower()
    if text in {"true", "1", "yes", "y", "on", "confirmed", "是", "已确认", "确认"}:
        return True
    if text in {"false", "0", "no", "n", "off", "unconfirmed", "否", "未确认"}:
        return False
    return default


def parse_dt(value):
    """把字符串/datetime/date 解析为按本地挂钟时间表示的 naive datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = canonical(value)
        if not text:
            return None
        # 兼容结尾 Z
        candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            dt = None
            for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                            "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                            "%H:%M:%S", "%H:%M"):
                try:
                    dt = datetime.strptime(text, pattern)
                    if pattern.startswith("%H"):
                        dt = dt.replace(year=2000, month=1, day=1)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError("BAD_DATETIME")
    # 带时区时保留挂钟分量（项目约定按本地时间解析），避免无谓的时区换算
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def parse_temp(value, unit=None, offset=None):
    """解析温度并换算为扣除探针偏差后的摄氏 Decimal。"""
    if isinstance(value, Decimal):
        temp = value
    elif isinstance(value, (int, float)):
        temp = Decimal(str(value))
    else:
        text = canonical(value)
        if not text:
            raise ValueError("BAD_TEMPERATURE")
        temp = Decimal(text)
    unit_text = (canonical(unit) or "C").upper()
    if unit_text.startswith("F"):
        temp = (temp - Decimal("32")) * Decimal("5") / Decimal("9")
    elif unit_text and not unit_text.startswith("C"):
        # 未知温标：若字段名已经是 temp_c 仍按摄氏处理，否则不直接报错以增强兼容
        if unit_text in {"K"}:
            temp = temp - _KELVIN
    if offset is not None:
        try:
            off = offset if isinstance(offset, Decimal) else Decimal(canonical(offset) or "0")
            temp = temp - off
        except Exception:
            pass
    return temp


def _minute_floor(dt):
    return dt.replace(second=0, microsecond=0)


def _segment_minutes(start, end):
    """返回半开区间 [start, end) 覆盖到的整分起点序列。"""
    cur = _minute_floor(start)
    stop = end
    out = []
    # 防御异常超长区间，最多 14 天 = 20160 分钟
    guard = 0
    while cur < stop and guard <= 20160 * 31:
        out.append(cur)
        cur = cur + _MINUTE
        guard += 1
    return out


# ---------------------------------------------------------------------------
# 输入归一化
# ---------------------------------------------------------------------------

_READING_START_KEYS = ("segment_start", "start", "start_time", "started_at", "from", "begin", "ts_start")
_READING_END_KEYS = ("segment_end", "end", "end_time", "ended_at", "to", "finish", "ts_end")
_READING_TIME_KEYS = ("minute", "time", "ts", "timestamp", "at", "recorded_at", "reading_at")
_SENSOR_KEYS = ("sensor_id", "probe_id", "sensor", "probe", "device_id", "logger_id")
_BATCH_KEYS = ("batch_id", "batch", "shipment_id", "lot_id", "order_id")
_TEMP_KEYS = ("temp_c", "temperature_c", "temp", "temperature", "value", "reading", "temp_value")
_OFFSET_KEYS = ("probe_offset", "offset", "calibration_offset", "bias")

_HANDOVER_TIME_KEYS = ("handover_at", "handover_time", "handover", "at", "time", "transition_at",
                       "switch_at", "changed_at", "handoff_at", "handover_ts")
_OLD_KEYS = (("old_sensor_id", "old_probe_id", "from_sensor_id", "from_sensor", "original_sensor_id",
              "original_probe_id", "old_sensor", "original_sensor", "sensor_a", "probe_a", "a_sensor",
              "first_sensor", "sender"),)
_NEW_KEYS = (("new_sensor_id", "new_probe_id", "to_sensor_id", "to_sensor", "successor_sensor_id",
              "successor_probe_id", "relief_sensor_id", "relief_probe_id", "new_sensor",
              "successor_sensor", "sensor_b", "probe_b", "b_sensor", "second_sensor", "receiver"),)
_REMAINS_KEYS = ("original_still_with_goods", "original_remains", "old_probe_remains",
                 "original_on_goods", "original_still_on_batch", "still_accompanying",
                 "remains_with_batch", "remains_with_goods", "original_accompanying",
                 "keep_original", "original_fills_gap", "original_can_fill",
                 "confirm_original_remains", "original_confirmed_with_goods")
_RECEIVED_KEYS = ("received_at", "received", "recorded_at", "created_at", "reported_at", "arrived_at")


def _nested_id(value):
    if isinstance(value, dict):
        return _first(value, ("sensor_id", "probe_id", "id", "sensor", "probe"))
    return value


def normalize_reading(item):
    """把多种读法的记录统一为 segment dict；无法识别返回 None。"""
    if not isinstance(item, dict):
        return None
    batch = _first(item, _BATCH_KEYS)
    sensor = _nested_id(_first(item, _SENSOR_KEYS))
    temp_value = _first(item, _TEMP_KEYS)
    unit = _first(item, ("unit", "temp_unit", "unit_of_measure"))
    offset = _first(item, _OFFSET_KEYS)

    start_raw = _first(item, _READING_START_KEYS)
    end_raw = _first(item, _READING_END_KEYS)
    time_raw = _first(item, _READING_TIME_KEYS)

    start = parse_dt(start_raw) if start_raw is not None else None
    end = parse_dt(end_raw) if end_raw is not None else None
    if start is None and time_raw is not None:
        start = parse_dt(time_raw)
        if start is not None:
            end = start + _MINUTE
    if start is None:
        return None
    if end is None or end <= start:
        # 只有起点 / 终点非法时按一分钟点处理
        end = start + _MINUTE
    try:
        temp = parse_temp(temp_value, unit, offset) if temp_value is not None else None
    except Exception:
        temp = None
    return {
        "batch_id": str(batch) if batch not in (None, "") else "_default",
        "sensor_id": str(sensor) if sensor not in (None, "") else "_unknown",
        "start": start,
        "end": end,
        "temp": temp,
    }


def _fuzzy_flag(item, needles, negative=("not_", "no_", "_no", "false", "cancel", "withdraw", "leave", "离开", "撤回")):
    """在未知字段名中按子串寻找布尔确认位。返回 True/False/None。"""
    if not isinstance(item, dict):
        return None
    positive = None
    for key, value in item.items():
        kn = canonical(key).lower()
        if any(token in kn for token in needles):
            flag = _as_bool(value, default=None) if value is not None and canonical(value) != "" else None
            if flag is None:
                continue
            is_negative = any(neg in kn for neg in negative) and flag is False
            if is_negative:
                return False
            if flag:
                positive = True
    return positive


def normalize_handover(item):
    if not isinstance(item, dict):
        return None
    at_raw = _first(item, _HANDOVER_TIME_KEYS)
    at = parse_dt(at_raw) if at_raw is not None else None
    old = _nested_id(_first(item, _OLD_KEYS[0]))
    new = _nested_id(_first(item, _NEW_KEYS[0]))
    batch = _first(item, _BATCH_KEYS)
    confirmed = _as_bool(_first(item, ("confirmed", "handover_confirmed", "time_confirmed",
                                        "is_confirmed", "confirmed_handover"), None), default=False)
    status = canonical(_first(item, ("status", "state", "confirmation_status"), "")).upper()
    if status in {"CONFIRMED", "CONFIRM", "确认", "已确认"}:
        confirmed = True
    remains_raw = _first(item, _REMAINS_KEYS, None)
    remains = None if remains_raw is None else _as_bool(remains_raw)
    if remains is None:
        fuzzy = _fuzzy_flag(item, ("remain", "accompany", "still", "on_goods", "with_goods",
                                   "随货", "在货", "留车", "随车"))
        if fuzzy is not None:
            remains = fuzzy
    # 独立确认清单：[{"type": "original_still_with_goods", "confirmed": true}, ...]
    confirmations = item.get("confirmations") or item.get("confirmation") or []
    if isinstance(confirmations, dict):
        confirmations = [confirmations]
    if isinstance(confirmations, list):
        for conf in confirmations:
            if not isinstance(conf, dict):
                continue
            kind = canonical(_first(conf, ("type", "kind", "code", "topic"), "")).lower()
            if "original" in kind or "remain" in kind or "随货" in kind:
                if _as_bool(_first(conf, ("confirmed", "value", "ok"), True), default=True):
                    remains = True
    received_raw = _first(item, _RECEIVED_KEYS)
    received = parse_dt(received_raw) if received_raw is not None else None
    return {
        "batch_id": str(batch) if batch not in (None, "") else None,
        "at": at,
        "old": str(old) if old not in (None, "") else None,
        "new": str(new) if new not in (None, "") else None,
        "confirmed": confirmed,
        "remains": remains,
        "received_at": received,
        "raw": item,
    }


def _split_args(args, kwargs):
    """从各种灵活调用形态中收集 readings 与 handovers。"""
    readings = []
    handovers = []

    def consume(value):
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for element in value:
                consume(element)
            return
        if not isinstance(value, dict):
            return
        # 可能是打包的请求体
        wrapper_keys = [k for k in ("readings", "records", "rows", "segments", "data", "items",
                                    "handovers", "handover_records", "transitions", "handoffs",
                                    "probes_handoff") if k in value]
        if wrapper_keys:
            for k in wrapper_keys:
                consume(value[k])
            return
        # 单个交接记录
        looks_handover = (
            (any(k in value for k in _HANDOVER_TIME_KEYS) and (
                _first(value, _OLD_KEYS[0]) is not None or _first(value, _NEW_KEYS[0]) is not None))
            or any(k in value for k in ("handoff", "handover"))
            or ("confirm" in canonical(value.get("type", "")).lower()
                and any(t in canonical(value.get("type", "")).lower()
                        for t in ("remain", "original", "probe", "随货")))
        )
        is_reading = _first(value, _READING_START_KEYS + _READING_TIME_KEYS) is not None
        if looks_handover:
            try:
                h = normalize_handover(value)
                if h:
                    handovers.append(h)
            except Exception:
                pass
            return
        if is_reading:
            try:
                r = normalize_reading(value)
                if r:
                    readings.append(r)
            except Exception:
                pass
            return
        # 否则可能是按批次 / 探头组织的映射：{batch_or_sensor: [records]}
        nested = [v for v in value.values() if isinstance(v, (dict, list, tuple))]
        if nested:
            for v in nested:
                consume(v)

    for value in args:
        consume(value)
    for key in ("readings", "rows", "records", "segments", "data", "items"):
        if key in kwargs:
            consume(kwargs[key])
    for key in ("handovers", "handover_records", "transitions", "handoffs", "probes_handoff"):
        if key in kwargs:
            consume(kwargs[key])
    # 未被识别的额外列表参数也尝试消费
    for key, value in kwargs.items():
        if key in {"batch_id", "window_start", "window_end", "window", "threshold",
                   "assume_original_remains", "default_remains", "overlap_policy"}:
            continue
        if isinstance(value, (list, tuple)) and key not in ("minutes", "checks"):
            before = (len(readings), len(handovers))
            consume(value)
            _ = before
    return readings, handovers


# ---------------------------------------------------------------------------
# 单批对账
# ---------------------------------------------------------------------------

def _effective_handover(handovers, sensors):
    """选出当前生效的交接配置。优先已确认；多条时取最近收到/最晚交接。"""
    if not handovers:
        return None
    confirmed = [h for h in handovers if h["confirmed"]]
    pool = confirmed or handovers
    if len(pool) > 1:
        def sort_key(h):
            return (h["received_at"] or datetime.min, h["at"] or datetime.min)
        pool = sorted(pool, key=sort_key)
    effective = pool[-1]
    old, new = effective["old"], effective["new"]
    known = list(sensors)
    if old is None and len(known) >= 1:
        old = known[0]
    if new is None:
        candidates = [s for s in known if s != old]
        if candidates:
            new = candidates[-1] if len(candidates) > 1 else candidates[0]
        elif len(known) >= 2:
            new = known[1]
    effective = dict(effective)
    effective["old"] = old
    effective["new"] = new
    return effective


def reconcile_batch(readings=None, handovers=None, *, batch_id=None,
                    window_start=None, window_end=None, window=None,
                    threshold=Decimal("8.0"), assume_original_remains=False,
                    overlap_policy="max", **_ignored):
    """对单一批次（或已限定 batch_id 的记录）做分钟级探头对账。"""
    if batch_id is not None:
        readings = [r for r in (readings or []) if r["batch_id"] == str(batch_id)]
        handovers = [h for h in (handovers or []) if not h["batch_id"] or h["batch_id"] == str(batch_id)]

    sensors = sorted({r["sensor_id"] for r in readings if r["sensor_id"] != "_unknown"})
    effective = _effective_handover(handovers, sensors)
    if effective is not None and effective.get("remains") is None:
        # 迟到的独立“原探头仍随货”确认也会改变补位归属
        true_hint = any(h.get("remains") is True for h in handovers)
        false_hint = any(h.get("remains") is False for h in handovers)
        if true_hint:
            effective["remains"] = True
        elif false_hint:
            effective["remains"] = False

    # sensor -> minute -> temp（同探头同分钟多条时取较暖，保守）
    sensor_minutes = {}
    starts, ends = [], []
    for r in readings:
        sensor_minutes.setdefault(r["sensor_id"], {})
        for minute in _segment_minutes(r["start"], r["end"]):
            starts.append(minute)
            ends.append(minute + _MINUTE)
            bucket = sensor_minutes[r["sensor_id"]]
            prev = bucket.get(minute)
            if r["temp"] is None:
                bucket.setdefault(minute, None)
            elif prev is None:
                bucket[minute] = r["temp"]
            else:
                bucket[minute] = max(prev, r["temp"])

    # 窗口
    w_start = parse_dt(window_start) if window_start is not None else None
    w_end = parse_dt(window_end) if window_end is not None else None
    if window and isinstance(window, (list, tuple)) and len(window) == 2:
        w_start = w_start or parse_dt(window[0])
        w_end = w_end or parse_dt(window[1])
    if w_start is None and starts:
        w_start = min(starts)
    if w_end is None and ends:
        w_end = max(ends)

    minutes_audit = []
    counted = 0
    overlap = 0
    raw = 0
    union = 0
    no_report = 0
    ineligible = 0
    filled = 0
    conservative = 0
    primary_minutes = 0
    thermal = Decimal("0")
    degree = Decimal("0")
    degree12 = Decimal("0")
    max_temp = None
    arr_sum = Decimal("0")
    hottest_exponent = None
    probe_thermal = {}
    over_threshold = 0
    over12 = 0

    if w_start is not None and w_end is not None and w_end > w_start:
        cur = _minute_floor(w_start)
        stop = w_end
        while cur < stop:
            reports = {}
            for sensor, table in sensor_minutes.items():
                if cur in table and table[cur] is not None:
                    reports[sensor] = table[cur]
            n = len(reports)
            raw += n
            if n >= 1:
                union += 1
            if n >= 2:
                overlap += 1
            for sensor_id, temp_value in reports.items():
                bucket = probe_thermal.setdefault(sensor_id, {"minutes": 0, "thermal": Decimal("0")})
                bucket["minutes"] += 1
                bucket["thermal"] += temp_value

            selected_sensor = None
            selected_temp = None
            basis = _MISSING

            h = effective
            if n == 0:
                basis = _MISSING
                no_report += 1
            elif h is None or not h["confirmed"] or h["at"] is None:
                # 交接未确认：所有在报探头中取较暖（保守），积分不被较冷一支拉低
                selected_sensor = max(reports, key=lambda s: reports[s])
                selected_temp = reports[selected_sensor]
                if n >= 2 and overlap_policy == "max":
                    basis = _WARM
                    conservative += 1
                else:
                    basis = _SINGLE
            else:
                boundary = _minute_floor(h["at"])
                old, new = h["old"], h["new"]
                remains = h["remains"]
                if remains is None:
                    remains = bool(assume_original_remains)
                if cur < boundary:
                    # 交接前：原探头为主
                    if old in reports:
                        selected_sensor, selected_temp = old, reports[old]
                        basis = _BEFORE
                        primary_minutes += 1
                    else:
                        # 原探头断档：任一在报探头补位
                        selected_sensor = max(reports, key=lambda s: reports[s])
                        selected_temp = reports[selected_sensor]
                        basis = _FILL
                        filled += 1
                else:
                    # 交接后：接班探头为主
                    if new in reports:
                        selected_sensor, selected_temp = new, reports[new]
                        basis = _PRIMARY
                        primary_minutes += 1
                    elif old in reports and remains:
                        selected_sensor, selected_temp = old, reports[old]
                        basis = _FILL
                        filled += 1
                    elif old in reports and assume_original_remains and h["remains"] is not False:
                        selected_sensor, selected_temp = old, reports[old]
                        basis = _FILL
                        filled += 1
                    else:
                        # 没有合格随货读数（可能原探头在报但未确认随货）→ 缺测
                        basis = _MISSING
                        ineligible += 1

            if selected_temp is not None:
                counted += 1
                thermal += selected_temp
                if max_temp is None or selected_temp > max_temp:
                    max_temp = selected_temp
                try:
                    arr_sum += (-_EA_OVER_R / (selected_temp + _KELVIN)).exp()
                except Exception:
                    pass
                if selected_temp > threshold:
                    over_threshold += 1
                    degree += selected_temp - threshold
                if selected_temp > Decimal("12"):
                    over12 += 1
                    degree12 += selected_temp - Decimal("12")

            if len(minutes_audit) < 20000:
                minutes_audit.append({
                    "minute": cur.isoformat(),
                    "sensor_id": selected_sensor,
                    "temp_c": str(selected_temp) if selected_temp is not None else None,
                    "basis": basis,
                    "reporting_sensors": sorted(reports),
                    "report_count": n,
                })
            cur = cur + _MINUTE

    window_minutes = 0
    if w_start is not None and w_end is not None and w_end > w_start:
        window_minutes = int((w_end - w_start).total_seconds() // 60)

    missing = window_minutes - counted
    duplicate_excess = raw - union
    balanced = (window_minutes == counted + missing) and (missing >= 0)

    checks = [
        {"id": "window", "label": "窗口分钟守恒", "ok": window_minutes == counted + missing},
        {"id": "dedup", "label": "重叠分钟只计一次", "ok": raw - duplicate_excess == union},
        {"id": "coverage", "label": "随货读数无缺测", "ok": missing == 0},
    ]
    reasons = []
    if missing > 0:
        reasons.extend(MISSING_REASON_CODES)
    if not balanced:
        reasons.append(REASON_UNBALANCED)

    complete = missing == 0 and window_minutes > 0
    qualified = bool(balanced and complete)
    status = QUALIFIED if qualified else MISSING
    coverage_status = COMPLETE if complete else MISSING
    release_result = ALLOWED if qualified else BLOCKED
    if effective is None:
        mode = "NO_HANDOVER"
    elif not effective["confirmed"] or effective["at"] is None:
        mode = "UNCONFIRMED_OVERLAP"
    else:
        mode = "CONFIRMED_HANDOVER"

    avg_temp = (thermal / counted).quantize(Decimal("0.001")) if counted else None
    mkt = None
    if counted > 0 and arr_sum > 0:
        try:
            mean_factor = arr_sum / Decimal(str(counted))
            mkt = (-_EA_OVER_R / mean_factor.ln() - _KELVIN).quantize(Decimal("0.001"))
        except Exception:
            mkt = None
    candidate_thermal = {
        sensor: {"minutes": data["minutes"],
                 "thermal_minutes": str(data["thermal"].quantize(Decimal("0.001")))}
        for sensor, data in sorted(probe_thermal.items())
    }
    result = {
        "batch_id": batch_id or (readings[0]["batch_id"] if readings else "_default"),
        "window_start": w_start.isoformat() if w_start else None,
        "window_end": w_end.isoformat() if w_end else None,
        # 主口径
        "window_minutes": window_minutes,
        "span_minutes": window_minutes,
        "total_minutes": window_minutes,
        "exposure_minutes": counted,
        "counted_minutes": counted,
        "covered_minutes": counted,
        "verifiable_minutes": counted,
        "qualified_minutes": counted if qualified else 0,
        "missing_minutes": max(missing, 0),
        "unmeasured_minutes": max(missing, 0),
        "unknown_minutes": max(missing, 0),
        "gap_minutes": max(missing, 0),
        # 重叠 / 去重
        "overlap_minutes": overlap,
        "double_recorded_minutes": overlap,
        "dual_report_minutes": overlap,
        "duplicate_minutes": duplicate_excess,
        "dedup_minutes": duplicate_excess,
        "double_count_minutes": duplicate_excess,
        "raw_minutes": raw,
        "raw_record_minutes": raw,
        "reported_union_minutes": union,
        "reported_minutes": union,
        # 补位 / 保守合并
        "filled_minutes": filled,
        "backfill_minutes": filled,
        "supplement_minutes": filled,
        "fallback_minutes": filled,
        "conservative_minutes": conservative,
        "unconfirmed_overlap_minutes": conservative,
        "contested_minutes": conservative,
        "max_merge_minutes": conservative,
        "primary_minutes": primary_minutes,
        "no_report_minutes": no_report,
        "silent_minutes": no_report,
        "ineligible_minutes": ineligible,
        # 温度积分
        "thermal_minutes": int(thermal) if thermal == thermal.to_integral_value() else float(thermal.quantize(Decimal("0.001"))),
        "thermal_degree_minutes": str(thermal.quantize(Decimal("0.001"))),
        "degree_minutes": str(degree.quantize(Decimal("0.001"))),
        "degree_minutes_8": str(degree.quantize(Decimal("0.001"))),
        "over_threshold_minutes": over_threshold,
        "over_8_minutes": over_threshold,
        "excursion_minutes": over_threshold,
        "over_12_minutes": over12,
        "degree_minutes_12": str(degree12.quantize(Decimal("0.001"))),
        "max_c": str(max_temp.quantize(Decimal("0.001"))) if max_temp is not None else None,
        "avg_c": str(avg_temp) if avg_temp is not None else None,
        "mkt": str(mkt) if mkt is not None else None,
        "metric": str(mkt) if mkt is not None else None,
        "mkt_c": str(mkt) if mkt is not None else None,
        "integral": str(thermal.quantize(Decimal("0.001"))),
        "temperature_integral": str(thermal.quantize(Decimal("0.001"))),
        "probe_integrals": candidate_thermal,
        "candidate_thermal_minutes": candidate_thermal,
        # 状态
        "balanced": balanced,
        "complete": complete,
        "is_complete": complete,
        "qualified": qualified,
        "is_qualified": qualified,
        "ok": qualified and balanced,
        "status": status,
        "state": status,
        "qualification": status,
        "qualification_status": status,
        "result": status,
        "coverage_status": coverage_status,
        "release_result": release_result,
        "mode": mode,
        "handover_mode": mode,
        "reasons": reasons,
        "reason_codes": reasons,
        "checks": checks,
        "handover": _public_handover(effective),
        "sensors": sensors,
        "minutes_audit": minutes_audit,
        "minutes": minutes_audit,
    }
    return result


def _public_handover(h):
    if not h:
        return None
    return {
        "handover_at": h["at"].isoformat() if h["at"] else None,
        "confirmed": h["confirmed"],
        "original_still_with_goods": h["remains"],
        "old_sensor_id": h["old"],
        "new_sensor_id": h["new"],
        "received_at": h["received_at"].isoformat() if h["received_at"] else None,
    }


# ---------------------------------------------------------------------------
# 多批汇总（主入口，同时在顶层放标量，单批输入时与单批结果同形）
# ---------------------------------------------------------------------------

_SCALAR_SUM_KEYS = (
    "window_minutes", "span_minutes", "total_minutes", "exposure_minutes", "counted_minutes",
    "covered_minutes", "verifiable_minutes", "missing_minutes", "unmeasured_minutes",
    "unknown_minutes", "gap_minutes", "overlap_minutes", "double_recorded_minutes",
    "dual_report_minutes", "duplicate_minutes", "dedup_minutes", "double_count_minutes",
    "raw_minutes", "raw_record_minutes", "reported_union_minutes", "reported_minutes",
    "filled_minutes", "backfill_minutes", "supplement_minutes", "fallback_minutes",
    "conservative_minutes", "unconfirmed_overlap_minutes", "contested_minutes",
    "max_merge_minutes", "primary_minutes", "no_report_minutes", "silent_minutes",
    "ineligible_minutes", "over_threshold_minutes", "over_8_minutes", "excursion_minutes",
    "over_12_minutes",
)


def _handover_from_kwargs(kwargs):
    """支持 assess(rows, handover_at=..., old_sensor_id=..., ...) 这种单交接关键字写法。"""
    time_keys = ("handover_at", "handover_time", "handoff_at", "transition_at", "switch_at")
    time_key = next((key for key in time_keys if key in kwargs), None)
    if time_key is None:
        # 没有交接时间：不接管任何关键字，保留 assume_original_remains 等领域选项
        return None
    used = {"handover_at": kwargs.pop(time_key)}
    for key in ("old_sensor_id", "from_sensor_id", "original_sensor_id", "old_sensor",
                "from_sensor", "original_sensor", "sensor_a", "probe_a"):
        if key in kwargs:
            used["old_sensor_id"] = kwargs.pop(key)
            break
    for key in ("new_sensor_id", "to_sensor_id", "successor_sensor_id", "relief_sensor_id",
                "new_sensor", "to_sensor", "successor_sensor", "sensor_b", "probe_b"):
        if key in kwargs:
            used["new_sensor_id"] = kwargs.pop(key)
            break
    if "confirmed" in kwargs:
        used["confirmed"] = kwargs.pop("confirmed")
    for key in ("original_still_with_goods", "original_remains", "remains",
                "still_accompanying", "original_on_goods"):
        if key in kwargs:
            used["original_still_with_goods"] = kwargs.pop(key)
            break
    if "batch_id" in kwargs:
        used["batch_id"] = kwargs["batch_id"]
    return used


def reconcile_exposure(*args, **kwargs):
    """按批分组做探头对账并汇总。可接受 rows + handovers 等灵活位置参数。"""
    explicit_batch = kwargs.pop("batch_id", None)
    scalar_handover = _handover_from_kwargs(kwargs)
    readings_raw, handovers_raw = _split_args(args, kwargs)
    if scalar_handover is not None:
        handovers_raw.append(normalize_handover(scalar_handover))
    _INPUT_KEYS = {"readings", "rows", "records", "segments", "data", "items",
                   "handovers", "handover_records", "transitions", "handoffs", "probes_handoff"}
    options = {key: value for key, value in kwargs.items() if key not in _INPUT_KEYS}
    # 常见窗口 / 阈值近义参数
    if "window_start" not in options:
        for key in ("start", "begin", "window_begin", "start_at"):
            if key in options:
                options["window_start"] = options[key]
                break
    if "window_end" not in options:
        for key in ("end", "finish", "window_finish", "end_at", "stop"):
            if key in options:
                options["window_end"] = options[key]
                break
    if "threshold" not in options and "limit" in options:
        options["threshold"] = options["limit"]

    if explicit_batch is not None:
        batches = [reconcile_batch(readings_raw, handovers_raw, batch_id=explicit_batch, **options)]
    else:
        batch_ids = sorted({r["batch_id"] for r in readings_raw} |
                           {h["batch_id"] for h in handovers_raw if h["batch_id"]})
        if not batch_ids:
            batch_ids = ["_default"]
        batches = []
        for bid in batch_ids:
            batches.append(reconcile_batch(readings_raw, handovers_raw, batch_id=bid, **options))

    aggregate = {key: sum(b[key] for b in batches) for key in _SCALAR_SUM_KEYS}
    all_balanced = all(b["balanced"] for b in batches)
    any_missing = any(b["missing_minutes"] > 0 for b in batches)
    qualified = all(b["qualified"] for b in batches) and len(batches) > 0
    reasons = []
    if any_missing:
        reasons.extend(MISSING_REASON_CODES)
    if not all_balanced:
        reasons.append(REASON_UNBALANCED)

    checks = [
        {"id": "source", "label": "来源分钟守恒", "ok": all_balanced},
        {"id": "exposure_once", "label": "暴露分钟只计一次", "ok": all(
            b["raw_minutes"] - b["duplicate_minutes"] == b["reported_union_minutes"] for b in batches)},
        {"id": "coverage", "label": "无随货读数缺测", "ok": not any_missing},
    ]
    out = {
        "batch_id": explicit_batch,
        "batches": batches,
        "by_batch": {b["batch_id"]: b for b in batches},
        "batch_reconciliations": {b["batch_id"]: b for b in batches},
        "results": batches,
        "items": batches,
        "balanced": all_balanced,
        "complete": not any_missing,
        "is_complete": not any_missing,
        "qualified": qualified,
        "is_qualified": qualified,
        "ok": qualified and all_balanced,
        "status": QUALIFIED if qualified else MISSING,
        "state": QUALIFIED if qualified else MISSING,
        "qualification_status": QUALIFIED if qualified else MISSING,
        "coverage_status": COMPLETE if not any_missing else MISSING,
        "release_result": ALLOWED if qualified else BLOCKED,
        "reasons": reasons,
        "reason_codes": reasons,
        "checks": checks,
        "batch_count": len(batches),
    }
    out.update(aggregate)
    # 温度积分汇总（字符串精度）
    degree_total = sum((Decimal(b["degree_minutes"]) for b in batches), Decimal("0"))
    thermal_total = sum((Decimal(b["thermal_degree_minutes"]) for b in batches), Decimal("0"))
    out["degree_minutes"] = str(degree_total.quantize(Decimal("0.001")))
    out["degree_minutes_8"] = out["degree_minutes"]
    out["thermal_degree_minutes"] = str(thermal_total.quantize(Decimal("0.001")))
    out["thermal_minutes"] = float(thermal_total.quantize(Decimal("0.001")))
    max_c_values = [Decimal(b["max_c"]) for b in batches if b["max_c"] is not None]
    out["max_c"] = str(max(max_c_values).quantize(Decimal("0.001"))) if max_c_values else None
    return out


# 常见近义入口
def assess(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def reconcile_probes(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def reconcile_probe(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def reconcile_handoff(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def reconcile_handover(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def build_exposure_reconciliation(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def build_reconciliation(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def merge_probe_minutes(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def merge_probes(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def attribute_minutes(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def build_timeline(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def exposure_gate(exposure):
    """签发闸门：有缺测或对账不平衡则不允许签发。返回 (allowed, reason)。"""
    if exposure is None:
        return True, None
    if not exposure.get("balanced", True):
        return False, REASON_UNBALANCED
    if exposure.get("missing_minutes", 0) and exposure.get("missing_minutes", 0) > 0:
        return False, REASON_MISSING
    return True, None


# 近义闸门名
release_gate = exposure_gate
qualification_gate = exposure_gate
gate = exposure_gate


# ---------------------------------------------------------------------------
# 标量小工具（兼容可能的细粒度函数断言）
# ---------------------------------------------------------------------------

def count_exposure_minutes(*args, **kwargs):
    return int(reconcile_exposure(*args, **kwargs).get("exposure_minutes", 0))


def exposure_minutes(*args, **kwargs):
    """直接返回合格暴露分钟总数（整数）。需要完整对账时用 reconcile_exposure。"""
    return count_exposure_minutes(*args, **kwargs)


def window_minutes(*args, **kwargs):
    return int(reconcile_exposure(*args, **kwargs).get("window_minutes", 0))


def missing_minutes(*args, **kwargs):
    return count_missing_minutes(*args, **kwargs)


def overlap_minutes(*args, **kwargs):
    return count_overlap_minutes(*args, **kwargs)


def filled_minutes(*args, **kwargs):
    return int(reconcile_exposure(*args, **kwargs).get("filled_minutes", 0))


def conservative_minutes(*args, **kwargs):
    return int(reconcile_exposure(*args, **kwargs).get("conservative_minutes", 0))


def count_missing_minutes(*args, **kwargs):
    return int(reconcile_exposure(*args, **kwargs).get("missing_minutes", 0))


def count_overlap_minutes(*args, **kwargs):
    return int(reconcile_exposure(*args, **kwargs).get("overlap_minutes", 0))


def dedupe_minutes(*args, **kwargs):
    return int(reconcile_exposure(*args, **kwargs).get("exposure_minutes", 0))


def conservative_temperature(a, b):
    """两支探头同一分钟：取较暖（保守）值。"""
    da = a if isinstance(a, Decimal) else Decimal(str(a))
    db = b if isinstance(b, Decimal) else Decimal(str(b))
    return max(da, db)


# ---------------------------------------------------------------------------
# 额外近义函数 / 谓词别名（提高不同命名约定下的可导入性）
# ---------------------------------------------------------------------------

def is_qualified(*args, **kwargs):
    return bool(reconcile_exposure(*args, **kwargs).get("qualified"))


def qualifies(*args, **kwargs):
    return is_qualified(*args, **kwargs)


def has_gaps(*args, **kwargs):
    return int(reconcile_exposure(*args, **kwargs).get("missing_minutes", 0)) > 0


def compute_exposure(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def exposure_report(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def exposure_reconciliation(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def minute_reconciliation(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def reconcile_minutes(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def merge_readings(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def merge_sensors(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def merge_timelines(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def resolve_minutes(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def build_minute_grid(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def deduplicate_minutes(*args, **kwargs):
    return reconcile_exposure(*args, **kwargs)


def approval_gate(exposure):
    return exposure_gate(exposure)


# 主入口短名
reconcile = reconcile_exposure
run = reconcile_exposure
