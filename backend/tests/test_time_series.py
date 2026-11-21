"""跨月回填与日汇总链路测试：验证日期聚合、来源分区和复核清单。"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import time_series
from app.services.dq_trend import DqTrendService
from app.services.metric_monitor import MetricMonitorService
from app.store import store

VALID_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(autouse=True)
def clean_time_series_rows():
    """每个用例前清掉上一个用例写入的趋势/复核记录，避免随机数据互相影响。"""
    for module in ("dq_trend", "dq_trend_review", "dq_incident", "dq_scorecard"):
        store.rows(module).clear()
    yield


def day(entries):
    return [entry["日期"] for entry in entries]


class TestDateWindow:
    def test_window_crosses_month_without_zero_day(self):
        window = time_series.date_window("2026-11-21", 18)
        assert window[0].isoformat() == "2026-11-04"
        assert window[-1].isoformat() == "2026-11-21"
        assert len(window) == 18

    def test_parse_day_rejects_generated_invalid_dates(self):
        assert time_series.parse_day("2026-11-00") is None
        assert time_series.parse_day("2026-11--1") is None
        assert time_series.parse_day("dt=20261031") is not None


class TestMetricTrendBuckets:
    def test_long_window_has_only_valid_consecutive_dates(self):
        svc = MetricMonitorService()
        trends = svc.aggregate_metric_trends("ods_order_info", 45, end_date="2026-01-15")
        assert len(trends) == 45
        dates = day(trends)
        assert all(VALID_DATE.match(item) for item in dates), dates
        # 跨年窗口连续且有序，没有 00 日/负数日。
        from datetime import date, timedelta
        parsed = [date.fromisoformat(item) for item in dates]
        assert parsed == [date(2025, 12, 2) + timedelta(days=i) for i in range(45)]

    def test_month_end_partition_collected_next_month_keeps_business_date(self):
        svc = MetricMonitorService()
        partitions = [
            {"分区日期": "2026-10-31", "采集时间": "2026-11-02T03:00:00+00:00",
             "指标值": "1200", "基线值": "1000", "采集批号": "batch-a"},
            {"分区日期": "2026-11-01", "采集时间": "2026-11-02T04:00:00+00:00",
             "指标值": "800", "基线值": "1000", "采集批号": "batch-b"},
        ]
        trends = svc.aggregate_metric_trends(
            "ods_order_info", 2, partitions=partitions, end_date="2026-11-01")
        assert day(trends) == ["2026-10-31", "2026-11-01"]
        assert trends[0]["来源分区"] == "dt=2026-10-31"
        assert trends[0]["分组日期"] == "2026-10-31"
        # 月底分区归属 10-31，而不是采集完成的 11-02 或月初。
        assert trends[0]["指标值"] == 1200
        assert trends[1]["指标值"] == 800
        assert trends[1]["趋势"] == "下降"

    def test_concurrent_corrections_same_partition_count_once(self):
        svc = MetricMonitorService()
        partitions = [
            {"分区日期": "2026-10-31", "采集批号": "batch-1", "采集时间": "2026-11-02T01:00:00",
             "指标值": "1100", "基线值": "1000"},
            {"分区日期": "2026-10-31", "采集批号": "batch-2", "采集时间": "2026-11-02T02:00:00",
             "指标值": "1150", "基线值": "1000"},
        ]
        trends = svc.aggregate_metric_trends(
            "dwd_trade_detail", 1, partitions=partitions, end_date="2026-10-31")
        assert day(trends) == ["2026-10-31"]
        assert len([row for row in store.rows("dq_trend")
                    if row.get("目标表名") == "dwd_trade_detail"]) == 1
        assert trends[0]["去重批次数"] == 1
        assert trends[0]["指标值"] == 1150

    def test_repeated_query_is_idempotent(self):
        svc = MetricMonitorService()
        partitions = [{"分区日期": "2026-10-31", "指标值": "1000", "基线值": "1000"}]
        first = svc.aggregate_metric_trends(
            "dim_product_sku", 1, partitions=partitions, end_date="2026-10-31")
        second = svc.aggregate_metric_trends(
            "dim_product_sku", 1, partitions=partitions, end_date="2026-10-31")
        assert first == second
        assert len([row for row in store.rows("dq_trend")
                    if row.get("目标表名") == "dim_product_sku"]) == 1


class TestQualityTrendReview:
    def test_cross_month_window_produces_review_snapshot(self):
        svc = DqTrendService()
        partitions = [
            {"分区日期": "2026-10-30", "采集时间": "2026-11-01T05:00:00",
             "综合质量": "88.0", "采集批号": "batch-1"},
            {"分区日期": "2026-10-31", "采集时间": "2026-11-01T06:00:00",
             "综合质量": "76.0", "采集批号": "batch-2"},
        ]
        trend = svc.compute_quality_trend(
            "ods_order_info", 3, end_date="2026-11-01", partitions=partitions)
        assert trend["窗口起始"] == "2026-10-30"
        assert trend["窗口结束"] == "2026-11-01"
        assert all(VALID_DATE.match(item) for item in trend["分组日期"])
        # 11-01 没有实际分区，被显式标记为未出数，而不是伪造日期。
        assert trend["有效日汇总"] == 2
        assert trend["缺失日期"] == 1
        assert trend["日汇总序列"][-1]["日期"] == "2026-11-01"
        assert trend["日汇总序列"][-1]["综合质量"] is None

        reviews = svc.list_trend_reviews("ods_order_info")
        assert len(reviews) == 1
        review = reviews[0]
        assert review["复核编号"] == trend["复核编号"]
        assert review["窗口结束"] == "2026-11-01"
        assert [item["日期"] for item in review["日汇总序列"]] == trend["分组日期"]
        assert review["来源分区"][0] == "dt=2026-10-30"
        assert review["分组日期"][:2] == ["2026-10-30", "2026-10-31"]

    def test_signed_date_preserved_and_revision_recorded(self):
        svc = DqTrendService()
        trend_rows = store.rows("dq_trend")
        trend_rows.append({
            "id": 1, "记录编号": "DQ_T-0001", "目标表名": "ods_order_info",
            "日期": "2026-10-31", "综合质量": "91.0", "已签收": True,
            "来源分区": "dt=2026-10-31", "分组日期": "2026-10-31",
            "status": "正常", "pending": False, "abnormal": False,
        })
        trend = svc.compute_quality_trend(
            "ods_order_info", 2, end_date="2026-11-01",
            partitions=[{"分区日期": "2026-10-31", "综合质量": "70.0",
                         "采集时间": "2026-11-03T00:00:00"}],
        )
        signed_point = next(p for p in trend["日汇总序列"] if p["日期"] == "2026-10-31")
        assert signed_point["综合质量"] == 91.0
        assert signed_point["已签收"] is True
        assert trend["日汇总序列"][0]["日期"] == "2026-10-31"
        review = svc.list_trend_reviews("ods_order_info")[0]
        assert review["已签收日期"] == ["2026-10-31"]
        # 留存报表仍能看到原值，后续修订单独留痕。
        stored = next(row for row in store.rows("dq_trend") if row["日期"] == "2026-10-31")
        assert stored["综合质量"] == "91.0"
        assert stored["后续修订"][0]["综合质量"] == 70.0

    def test_monitor_entry_and_trend_detail_share_same_dates(self):
        monitor = MetricMonitorService()
        partitions = [
            {"分区日期": "2026-10-31", "指标值": "1000", "基线值": "900",
             "综合质量": "88.0", "采集批号": "batch-1"},
            {"分区日期": "2026-11-01", "指标值": "950", "基线值": "900",
             "综合质量": "90.0", "采集批号": "batch-2"},
        ]
        monitor_trends = monitor.aggregate_metric_trends(
            "dws_daily_summary", 2, partitions=partitions, end_date="2026-11-01")
        detail = DqTrendService().compute_quality_trend(
            "dws_daily_summary", 2, end_date="2026-11-01", partitions=partitions)
        assert day(monitor_trends) == detail["分组日期"]
        assert [item["来源分区"] for item in monitor_trends] == detail["来源分区"]
        # 监控先落地日汇总；趋势详情不传质量字段时，仍复用同一批日汇总。
        detail_again = DqTrendService().compute_quality_trend(
            "dws_daily_summary", 2, end_date="2026-11-01", partitions=[
                {"分区日期": item["日期"], "指标值": item["指标值"], "基线值": 900}
                for item in monitor_trends
            ])
        assert day(monitor_trends) == detail_again["分组日期"]
        assert detail_again["有效日汇总"] == 2


class TestReportIgnoresInvalidDates:
    def test_report_average_excludes_invalid_and_missing_dates(self):
        from app.services.dq_report import DqReportService

        trend_rows = store.rows("dq_trend")
        trend_rows.extend([
            {"id": 1, "目标表名": "a", "日期": "2026-11-00", "综合质量": "10.0",
             "status": "正常", "pending": False, "abnormal": False},
            {"id": 2, "目标表名": "a", "日期": "2026-10-31", "综合质量": "75.0",
             "status": "正常", "pending": False, "abnormal": False},
            {"id": 3, "目标表名": "a", "日期": "2026-11-01", "综合质量": "95.0",
             "status": "正常", "pending": False, "abnormal": False},
        ])
        store.rows("dq_scorecard").clear()
        report = DqReportService().aggregate_quality_report("daily")
        assert report["平均综合质量"] == 85.0
