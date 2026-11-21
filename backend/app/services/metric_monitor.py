"""指标波动监控服务：查询指标当前值与基线对比，检测波动幅度并触发告警通知。

domain_chain: 指标波动检测 (metric_monitor)
  └─ 查询 → 质量事件 (dq_incident)
  └─ 关联 → 告警通知 (dq_alert_channel)
  └─ 校验 → 质量评分卡 (dq_scorecard)
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services import time_series
from app.store import store


class MetricMonitorService(BaseModuleService):
    MODULE = "metric_monitor"
    ENTITY = "监控指标"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["指标编号", "目标表名", "监控字段", "基线值", "当前值", "波动幅度", "告警阈值", "监控周期"]
    REQUIRED_FIELDS = ["指标编号", "目标表名", "监控字段"]
    STATUS_ORDER = ["草稿", "待校验", "监控中", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启动监控": "监控中", "停用监控": "已停用"}
    NEGATIVE_ACTIONS = ["停用监控"]

    def query_metric_values(self, table_name: str, metric_field: str,
                            baseline_value: float = 0) -> dict[str, Any]:
        """查询监控字段当前值并计算波动幅度。

        查询目标表中监控字段的当前统计值，与基线值对比计算波动幅度。
        若波动超过告警阈值，自动写入质量事件并触发告警通知。
        """
        # 查询当前值（模拟数据查询）
        current_value = baseline_value * random.uniform(0.5, 2.0) if baseline_value > 0 else random.uniform(100, 10000)
        if baseline_value == 0:
            baseline_value = current_value * 0.9

        volatility = abs(current_value - baseline_value) / baseline_value if baseline_value > 0 else 0
        volatility_pct = round(volatility * 100, 2)

        # 告警阈值判断
        thresholds = {
            "critical": 0.30,  # 30% 波动为严重
            "warning": 0.15,   # 15% 波动为告警
            "info": 0.05,      # 5% 波动为关注
        }
        alert_level = "normal"
        if volatility >= thresholds["critical"]:
            alert_level = "critical"
        elif volatility >= thresholds["warning"]:
            alert_level = "warning"
        elif volatility >= thresholds["info"]:
            alert_level = "info"

        result = {
            "目标表名": table_name,
            "监控字段": metric_field,
            "基线值": round(baseline_value, 4),
            "当前值": round(current_value, 4),
            "波动幅度": f"{volatility_pct}%",
            "告警级别": alert_level,
            "查询时间": datetime.now(timezone.utc).isoformat(),
        }

        # 若超过严重阈值：写入质量事件并触发告警
        if alert_level in ("critical", "warning"):
            self._create_quality_incident(table_name, metric_field, result)
            self._trigger_alert(table_name, metric_field, alert_level, volatility_pct)

        return result

    def _create_quality_incident(self, table_name: str, metric_field: str,
                                  metric_result: dict[str, Any]) -> None:
        """写入质量事件记录，将指标异常事件关联到质量事件管理。"""
        incident_rows = store.rows("dq_incident")
        new_iid = max((int(r.get("id", 0)) for r in incident_rows), default=0) + 1
        incident_rows.append({
            "id": new_iid,
            "事件编号": f"DQ_I-{new_iid:04d}",
            "关联规则": f"metric_monitor:{table_name}.{metric_field}",
            "触发时间": datetime.now(timezone.utc).isoformat(),
            "偏离程度": metric_result["波动幅度"],
            "影响行数": str(random.randint(1000, 500000)),
            "严重级别": metric_result.get("告警级别", "warning"),
            "处理状态": "新建",
            "处理人": "auto",
            "status": "待确认",
            "pending": True,
            "abnormal": True,
        })

    def _trigger_alert(self, table_name: str, field_name: str, level: str, volatility: float) -> None:
        """触发告警通知：将波动告警关联到告警通道配置，发起通知。"""
        alert_rows = store.rows("dq_alert_channel")
        new_aid = max((int(r.get("id", 0)) for r in alert_rows), default=0) + 1
        channel = "email" if level == "warning" else "phone+sms"
        alert_rows.append({
            "id": new_aid,
            "配置编号": f"DQ_A-{new_aid:04d}",
            "关联规则集": f"metric:{table_name}.{field_name}",
            "通知方式": channel,
            "接收人": "dq-admin",
            "告警级别": level,
            "静默时段": datetime.now(timezone.utc).isoformat(),
            "聚合策略": "立即",
            "通道状态": "已触发",
            "status": "已生效",
            "pending": False,
            "abnormal": level == "critical",
        })

    def aggregate_metric_trends(
        self,
        table_name: str,
        days: int = 7,
        *,
        metric_field: str = "",
        end_date: Any = None,
        partitions: Any = None,
    ) -> list[dict[str, Any]]:
        """聚合多日指标查询结果，生成按业务日期归组的日汇总序列。

        日期桶端点由分区日期决定（没有分区日期时才用采集完成时间兜底），
        因此月底分区即使次月在采集，也仍归属月底那天，不会串到月初。
        同一分区的并发更正按 ``(目标表名, 日期)`` 去重，只保留一条日汇总；
        已签收的历史日期不会被后续批次覆盖。输出日期一律为 ``YYYY-MM-DD``。
        """
        days = max(int(days), 1)
        anchor = time_series.parse_day(end_date)
        if anchor is None:
            anchor = max(
                (
                    time_series.partition_day(item)
                    for item in time_series.normalize_partition_input(partitions)
                    if time_series.row_table(item) in ("", table_name)
                ),
                default=None,
            )
        if anchor is None:
            anchor = datetime.now(timezone.utc).date()

        window = time_series.date_window(anchor, days)
        supplied = time_series.normalize_partition_input(partitions)
        buckets, superseded = time_series.dedupe_daily_records(
            supplied,
            table_name=table_name,
            start=window[0] if window else None,
            end=window[-1] if window else None,
        )

        # 已签收的历史日汇总优先保留（即便不在本次分区入参里）。
        existing = time_series.dedupe_daily_records(
            [
                dict(row, **{"目标表名": table_name})
                for row in store.rows("dq_trend")
                if row.get("目标表名") == table_name
                and time_series.is_signed(row)
            ],
            table_name=table_name,
            start=window[0] if window else None,
            end=window[-1] if window else None,
        )[0]
        bucket_days = {time_series.parse_day(bucket["日期"]) for bucket in buckets}
        for bucket in existing:
            bucket_day = time_series.parse_day(bucket["日期"])
            if bucket_day is not None and bucket_day not in bucket_days:
                buckets.append(bucket)
                bucket_days.add(bucket_day)
        buckets.sort(key=lambda item: item["日期"])

        # 只记录本次实际有分区的日期；空档日期用清晰的“未出数”标记占位。
        trends: list[dict[str, Any]] = []
        for day in window:
            bucket = next(
                (item for item in buckets if time_series.parse_day(item["日期"]) == day), None)
            if bucket is None:
                trends.append({
                    "日期": day.strftime(time_series.DATE_FORMAT),
                    "表名": table_name,
                    "指标字段": metric_field,
                    "指标值": None,
                    "波动率": None,
                    "趋势": "未出数",
                    "来源分区": "",
                    "采集时间": "",
                    "分组日期": day.strftime(time_series.DATE_FORMAT),
                    "已签收": False,
                    "状态": "未出数",
                })
                continue
            value = bucket.get("指标值")
            baseline = bucket.get("基线值")
            if value is None:
                volatility = bucket.get("波动率")
                direction = "未出数"
            else:
                volatility = (
                    round(abs(value - baseline) / baseline * 100, 2)
                    if baseline not in (None, 0) else bucket.get("波动率")
                )
                direction = "上升" if baseline is None or value >= baseline else "下降"
            trends.append({
                "日期": bucket["日期"],
                "表名": table_name,
                "指标字段": metric_field,
                "指标值": round(value, 2) if isinstance(value, (int, float)) else value,
                "波动率": f"{volatility:.2f}%" if isinstance(volatility, (int, float)) else None,
                "趋势": direction,
                "来源分区": bucket["来源分区"],
                "采集时间": bucket["采集时间"],
                "采集批号": bucket["采集批号"],
                "分组日期": bucket["分组日期"] or bucket["日期"],
                "已签收": bucket["已签收"],
                "去重批次数": bucket["去重批次数"],
                "状态": "已签收" if bucket["已签收"] else "正常",
            })

        self._persist_metric_buckets(table_name, metric_field, buckets)
        return trends

    def _persist_metric_buckets(
        self, table_name: str, metric_field: str, buckets: list[dict[str, Any]]
    ) -> None:
        """把日汇总写回趋势看板，监控入口与趋势详情共用同一批日期数据。"""
        trend_rows = store.rows("dq_trend")
        for bucket in buckets:
            day = time_series.parse_day(bucket["日期"])
            if day is None:
                continue
            # 监控指标没有维度的日汇总用指标本身补齐趋势字段，保证趋势详情能直接用。
            metric_value = bucket.get("指标值")
            baseline = bucket.get("基线值")
            quality = bucket.get("综合质量")
            if quality is None and metric_value is not None and baseline not in (None, 0):
                quality = round(100 - abs(metric_value - baseline) / baseline * 100, 2)
            payload = {
                "记录编号": bucket.get("记录编号", ""),
                "目标表名": table_name,
                "指标字段": metric_field,
                "完备率": f"{bucket['完备率']:.1f}%" if bucket.get("完备率") is not None else "",
                "准确率": f"{bucket['准确率']:.1f}%" if bucket.get("准确率") is not None else "",
                "及时率": f"{bucket['及时率']:.1f}%" if bucket.get("及时率") is not None else "",
                "综合质量": f"{quality:.1f}" if quality is not None else "",
                "指标值": metric_value,
                "基线值": baseline,
                "波动率": bucket.get("波动率"),
                "趋势标记": bucket.get("趋势", "正常"),
                "来源分区": bucket.get("来源分区", ""),
                "采集时间": bucket.get("采集时间", ""),
                "采集批号": bucket.get("采集批号", ""),
                "分组日期": bucket.get("分组日期") or bucket.get("日期"),
                "批次数": bucket.get("批次数", 1),
                "去重批次数": bucket.get("去重批次数", 0),
                "已签收": bucket.get("已签收", False),
                "后续修订": bucket.get("后续修订", []),
                "记录类型": "日汇总",
                "status": "正常",
                "pending": False,
                "abnormal": False,
            }
            entry = time_series.upsert_daily_bucket(trend_rows, table_name, day, payload)
            if not entry.get("记录编号"):
                entry["记录编号"] = f"DQ_T-{int(entry.get('id', 0)):04d}"

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "查询"
        snapshot["desc"] = "查询监控指标并对比基线检测波动"
        snapshot["artifact"] = "指标波动分析报告"
        snapshot["artifact_desc"] = "查询并聚合后的指标波动分析报告"
        return snapshot