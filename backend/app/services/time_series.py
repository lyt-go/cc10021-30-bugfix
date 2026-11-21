"""监控时序日桶工具：统一解析分区日期、生成跨月日期窗口并聚合日汇总。

这里刻意把“业务日期”和“采集时间”分开处理：

* 业务日期优先取分区字段（分区日期 / dt / 数据日期），它决定指标波动归属哪一天；
* 采集时间只在缺少分区字段时兜底，避免月底分区因次月才采集而被记到月初；
* 同一分区被多个采集批更正时，一个 (目标表名, 日期) 只保留一个日桶。

所有对外输出的日期都经过 ``date`` 校验，不会再出现 00 日、负数日或跨月错位。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

DATE_FORMAT = "%Y-%m-%d"

# 业务日期字段优先级：分区日期永远是第一选择，采集时间不参与业务日期判断。
PARTITION_DATE_KEYS = (
    "分区日期", "数据日期", "业务日期", "分区业务日期", "dt", "日期", "date",
)
COLLECT_TIME_KEYS = (
    "采集时间", "完成时间", "接入时间", "落库时间", "collect_time",
    "collected_at", "loaded_at",
)
BATCH_KEYS = ("采集批号", "接入批号", "批次号", "批次", "batch_id", "batch")
TABLE_KEYS = ("目标表名", "表名", "table_name", "table")
METRIC_VALUE_KEYS = ("指标值", "当前值", "metric_value", "value")
BASELINE_VALUE_KEYS = ("基线值", "基准值", "baseline_value", "baseline")
SIGNED_KEYS = ("已签收", "签收", "signed", "retained", "留存")
SIGNED_FLAG_VALUES = {"true", "1", "yes", "y", "是", "已签收", "已留存", "retained"}

_ISO_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_COMPACT_DATE_RE = re.compile(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})")


def parse_day(value: Any) -> date | None:
    """把日期/时间/字符串解析成 ``date``；无法解析或非法日期返回 ``None``。

    ``2026-11-00``、``2026-11--1`` 这类由旧拼接逻辑产生的伪日期会被
    明确拒绝，避免继续流入报表。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    match = _ISO_DATE_RE.match(text)
    if match:
        return _safe_date(match.group(1), match.group(2), match.group(3))

    if len(text) == 8 and text.isdigit():
        return _safe_date(text[:4], text[4:6], text[6:])

    # 支持 dt=20261031 / dt=2026-10-31 这类分区名。
    match = _COMPACT_DATE_RE.search(text)
    if match:
        return _safe_date(match.group(1), match.group(2), match.group(3))

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _safe_date(year: Any, month: Any, day: Any) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def day_str(value: Any) -> str:
    """日期统一输出为 ``YYYY-MM-DD``；非法日期返回空串。"""
    parsed = parse_day(value)
    return parsed.strftime(DATE_FORMAT) if parsed else ""


def date_window(end: Any, days: int) -> list[date]:
    """生成以 ``end`` 结尾、长度为 ``days`` 的连续日期，天然支持跨月/跨年。"""
    anchor = parse_day(end)
    if anchor is None or days <= 0:
        return []
    start = anchor - timedelta(days=int(days) - 1)
    return [start + timedelta(days=offset) for offset in range(int(days))]


def in_window(parsed: date | None, start: date | None, end: date | None) -> bool:
    if parsed is None:
        return False
    if start is not None and parsed < start:
        return False
    if end is not None and parsed > end:
        return False
    return True


def to_number(value: Any) -> float | None:
    """宽松解析数值，兼容 ``92.5%``、``1,234`` 这类展示格式。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"n/a", "na", "none", "null", "-", "—"}:
        return None
    text = text.rstrip("%").strip()
    try:
        return float(text)
    except ValueError:
        return None


def first_value(record: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def row_table(record: dict[str, Any]) -> str:
    value = first_value(record, TABLE_KEYS)
    return str(value).strip() if value is not None else ""


def partition_day(record: dict[str, Any]) -> date | None:
    """分区业务日期：优先分区字段，其次才用采集/落库时间兜底。"""
    for key in PARTITION_DATE_KEYS:
        parsed = parse_day(record.get(key))
        if parsed is not None:
            return parsed
    for key in COLLECT_TIME_KEYS:
        parsed = parse_day(record.get(key))
        if parsed is not None:
            return parsed
    return None


def collect_day(record: dict[str, Any]) -> date | None:
    for key in COLLECT_TIME_KEYS:
        parsed = parse_day(record.get(key))
        if parsed is not None:
            return parsed
    return None


def is_signed(record: dict[str, Any]) -> bool:
    """判断历史日汇总是否已签收/已留存，已签收日期不允许被后续批次静默覆盖。"""
    for key in SIGNED_KEYS:
        value = record.get(key)
        if isinstance(value, bool):
            if value:
                return True
        elif value is not None and str(value).strip().lower() in SIGNED_FLAG_VALUES:
            return True
    status = str(record.get("签收状态") or "").strip()
    return status in {"已签收", "已留存", "retained", "signed"}


def batch_key(record: dict[str, Any]) -> str:
    value = first_value(record, BATCH_KEYS)
    return str(value).strip() if value is not None else ""


def partition_id(record: dict[str, Any], day: date) -> str:
    """返回可核对的分区标识，例如 ``dt=2026-10-31``。"""
    for key in ("来源分区", "分区", "partition", "partition_id"):
        value = record.get(key)
        if value is not None and str(value).strip():
            text = str(value).strip()
            if text.startswith("dt="):
                return text
            return f"dt={text}"
    value = record.get("dt")
    if value is not None and str(value).strip():
        text = str(value).strip()
        return text if text.startswith("dt=") else f"dt={text}"
    return f"dt={day.strftime(DATE_FORMAT)}"


def _revision_rank(record: dict[str, Any], index: int) -> tuple:
    """同一分区多个批次时的排序：已签收优先，其次按修订/采集时间，最后按入参顺序。"""
    signed = 1 if is_signed(record) else 0
    revision = (
        _parse_optional_day(record.get("修订日期"))
        or _parse_optional_day(record.get("更新时间"))
        or _parse_optional_day(record.get("分组日期"))
        or collect_day(record)
        or date.min
    )
    sequence = to_number(record.get("批序") or record.get("revision")) or 0.0
    return (signed, revision, sequence, index)


def _parse_optional_day(value: Any) -> date | None:
    return parse_day(value)


def _metric_snapshot(record: dict[str, Any], day: date | None) -> dict[str, Any]:
    return {
        "日期": day_str(day) if day else "",
        "来源分区": partition_id(record, day) if day else "",
        "采集时间": _stringify(first_value(record, COLLECT_TIME_KEYS)),
        "采集批号": batch_key(record),
        "分组日期": day_str(record.get("分组日期")),
        "指标值": to_number(first_value(record, METRIC_VALUE_KEYS)),
        "基线值": to_number(first_value(record, BASELINE_VALUE_KEYS)),
        "综合质量": to_number(record.get("综合质量")),
    }


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    return str(value)


def dedupe_daily_records(
    records: Iterable[dict[str, Any]],
    *,
    table_name: str | None = None,
    start: Any = None,
    end: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把分区批次聚合成日桶：同一 (目标表名, 日期) 只保留一个。

    返回 ``(日桶列表, 被去重批次列表)``：
    * 普通批次的并发更正按修订/采集时间取最新一条，只算一次；
    * 已签收的历史日期优先保留，后续更正进入 ``后续修订``，不会被静默覆盖。
    """
    start_day = parse_day(start)
    end_day = parse_day(end)
    winners: dict[tuple[str, date], dict[str, Any]] = {}
    ranks: dict[tuple[str, date], tuple] = {}
    duplicate_count: dict[tuple[str, date], int] = {}
    revisions: dict[tuple[str, date], list[dict[str, Any]]] = {}

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        table = row_table(record)
        if table_name and table and table != table_name:
            continue
        if table_name and not table:
            table = table_name
        day = partition_day(record)
        if not in_window(day, start_day, end_day):
            continue
        key = (table, day)
        rank = _revision_rank(record, index)
        duplicate_count[key] = duplicate_count.get(key, 0) + 1

        current = winners.get(key)
        if current is None or rank >= ranks[key]:
            if current is not None:
                revisions.setdefault(key, []).append(_metric_snapshot(current, key[1]))
            winners[key] = record
            ranks[key] = rank
        else:
            revisions.setdefault(key, []).append(_metric_snapshot(record, key[1]))

    buckets: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for key, record in sorted(winners.items(), key=lambda item: (item[0][0], item[0][1])):
        table, day = key
        winner_signed = is_signed(record)
        later = revisions.get(key, [])
        # 只有签收日期被更正时才需要人工核对“旧汇总/后续修订”的归属日期。
        if winner_signed and later:
            superseded.extend(
                {**revision, "目标表名": table, "业务日期": day_str(day)} for revision in later
            )
        buckets.append({
            "目标表名": table,
            "日期": day_str(day),
            "分区日期": day_str(day),
            "业务日期": day_str(day),
            "来源分区": partition_id(record, day),
            "采集时间": _stringify(first_value(record, COLLECT_TIME_KEYS)),
            "采集批号": batch_key(record),
            "分组日期": day_str(record.get("分组日期")),
            "已签收": winner_signed,
            "签收状态": "已签收" if winner_signed else "未签收",
            "指标值": to_number(first_value(record, METRIC_VALUE_KEYS)),
            "基线值": to_number(first_value(record, BASELINE_VALUE_KEYS)),
            "波动率": to_number(record.get("波动率")),
            "综合质量": to_number(record.get("综合质量")),
            "完备率": to_number(record.get("完备率")),
            "准确率": to_number(record.get("准确率")),
            "及时率": to_number(record.get("及时率")),
            "批次数": duplicate_count.get(key, 1),
            "去重批次数": max(duplicate_count.get(key, 1) - 1, 0),
            "后续修订": sorted(later, key=lambda item: item.get("采集时间", "")),
        })
    return buckets, superseded


def trend_series_entry(bucket: dict[str, Any]) -> dict[str, Any]:
    """从日桶抽取复核清单要预留的日汇总序列条目（不含逐日长文本）。"""
    return {
        "日期": bucket.get("日期", ""),
        "分组日期": bucket.get("分组日期") or bucket.get("日期", ""),
        "来源分区": bucket.get("来源分区", ""),
        "采集批号": bucket.get("采集批号", ""),
        "指标值": bucket.get("指标值"),
        "波动率": bucket.get("波动率"),
        "综合质量": bucket.get("综合质量"),
        "完备率": bucket.get("完备率"),
        "准确率": bucket.get("准确率"),
        "及时率": bucket.get("及时率"),
        "已签收": bucket.get("已签收", False),
    }


def upsert_daily_bucket(
    rows: list[dict[str, Any]],
    table_name: str,
    day: date,
    payload: dict[str, Any],
    *,
    protect_signed: bool = True,
) -> dict[str, Any]:
    """按 ``(目标表名, 日期)`` 更新或新增一条日汇总，重复调用不会产生重复日桶。

    已签收的历史日汇总默认保留原值（``protect_signed``），后续修订另记在
    ``后续修订`` 字段中，保证留存报表的日期和数值仍对得上。
    """
    signed = is_signed(payload)
    for row in rows:
        if row.get("目标表名") != table_name:
            continue
        if parse_day(row.get("日期")) != day:
            continue
        if protect_signed and is_signed(row) and not signed:
            if payload.get("后续修订"):
                row["后续修订"] = payload["后续修订"]
            if payload.get("复核编号"):
                row["复核编号"] = payload["复核编号"]
            return row
        row.update(payload)
        row["目标表名"] = table_name
        row["日期"] = day.strftime(DATE_FORMAT)
        row.setdefault("id", max((int(r.get("id", 0)) for r in rows), default=0) + 1)
        return row

    new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    entry = {
        "id": new_id,
        "记录编号": f"DQ_T-{new_id:04d}",
        **payload,
    }
    entry["目标表名"] = table_name
    entry["日期"] = day.strftime(DATE_FORMAT)
    entry.setdefault("status", "正常")
    entry.setdefault("pending", False)
    entry.setdefault("abnormal", False)
    rows.append(entry)
    return entry


def normalize_partition_input(partitions: Any) -> list[dict[str, Any]]:
    """把分区入参统一成字典列表，兼容单个日期、日期列表和字典列表。"""
    if partitions is None:
        return []
    if isinstance(partitions, dict):
        return [dict(partitions)]
    if isinstance(partitions, (str, date, datetime)):
        return [{"分区日期": partitions}]
    if isinstance(partitions, Iterable):
        items: list[dict[str, Any]] = []
        for item in partitions:
            if isinstance(item, dict):
                items.append(dict(item))
            else:
                items.append({"分区日期": item})
        return items
    return []
