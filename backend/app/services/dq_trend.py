"""质量趋势服务：跟踪数据质量指标变化趋势，计算趋势方向和改善程度。

domain_chain: 质量趋势 (dq_trend)
  └─ 计算 → 质量评分卡 (dq_scorecard)
  └─ 聚合 → 质量报告 (dq_report)
  └─ 查询 → 质量事件 (dq_incident)

趋势不再按“今天减去偏移量”拼字符串生成日期，而是按业务日期（分区日期）
归组：先收集评分卡、趋势看板和本次分钟分区里的日汇总，再看窗口内哪些日期
真正有数据，没有数据的日期显式标记为“未出数”。日桶确定后，把结论写回
质量趋势复核清单，并预留本次用到的日汇总序列、来源分区和分组日期。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from app.services import time_series
from app.services.base import BaseModuleService
from app.store import store

REVIEW_MODULE = "dq_trend_review"


class DqTrendService(BaseModuleService):
    MODULE = "dq_trend"
    ENTITY = "趋势记录"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["记录编号", "目标表名", "日期", "完备率", "准确率", "及时率", "综合质量", "趋势标记"]
    REQUIRED_FIELDS = ["记录编号", "目标表名", "日期"]
    STATUS_ORDER = ["正常", "下降趋势", "需关注", "已改善"]
    ACTION_RULES = {"标记下降": "下降趋势", "标记需关注": "需关注", "标记已改善": "已改善"}
    NEGATIVE_ACTIONS = []

    def compute_quality_trend(
        self,
        table_name: str,
        days: int = 14,
        *,
        end_date: Any = None,
        partitions: Any = None,
        metric_field: str = "",
        window_end: Any = None,
    ) -> dict[str, Any]:
        """计算数据质量趋势：按业务日期聚合历史质量数据并写回复核清单。

        * 日期窗口以 ``end_date``（或 ``window_end``）为最后一天向前推，跨月/跨年安全；
        * 有分区入参时沿用指标波动监控的日桶，保证监控入口和趋势详情同一批日期；
        * 没有分区入参时从质量评分卡和趋势看板的现有记录取数；
        * 窗口内没有日汇总的日期标记“未出数”，不会生成无效日期或伪造指标；
        * 日桶确定后写入复核清单，保存日汇总序列、来源分区与分组日期。
        """
        days = max(int(days), 1)
        anchor = time_series.parse_day(end_date if end_date is not None else window_end)
        supplied = time_series.normalize_partition_input(partitions)
        if anchor is None:
            anchor = max(
                (
                    time_series.partition_day(item)
                    for item in supplied
                    if time_series.row_table(item) in ("", table_name)
                ),
                default=None,
            )
        if anchor is None:
            anchor = self._latest_available_day(table_name)
        if anchor is None:
            anchor = datetime.now(timezone.utc).date()

        window = time_series.date_window(anchor, days)
        if not window:
            return self._empty_trend(table_name, days)

        sources = self._collect_daily_sources(table_name, window, supplied, metric_field)
        daily_points, revisions = self._merge_daily_sources(table_name, window, sources)

        # 写入趋势存储：按 (目标表名, 日期) 幂等更新，签收日期不覆盖。
        self._persist_quality_daily(table_name, daily_points)

        trend = self._summarize(daily_points, window)
        review = self._write_trend_review(
            table_name=table_name,
            window=window,
            daily_points=daily_points,
            trend=trend,
            revisions=revisions,
            metric_field=metric_field,
            source="分区" if supplied else "评分卡/趋势看板",
        )

        if trend["趋势方向"] == "下降":
            self._alert_trend_downturn(table_name, trend["趋势方向"], trend["变化率数值"])

        latest = next(
            (point for point in reversed(daily_points) if point.get("综合质量") is not None),
            None,
        )
        lowest = min(
            (point["综合质量"] for point in daily_points if point.get("综合质量") is not None),
            default=None,
        )
        return {
            "目标表名": table_name,
            "数据天数": days,
            "窗口起始": window[0].strftime(time_series.DATE_FORMAT),
            "窗口结束": window[-1].strftime(time_series.DATE_FORMAT),
            "趋势方向": trend["趋势方向"],
            "变化率": f"{trend['变化率数值']:+.1f}%",
            "最新质量": f"{latest['综合质量']:.1f}" if latest else "N/A",
            "最低质量": f"{lowest:.1f}" if lowest is not None else "N/A",
            "有效日汇总": trend["有效日汇总"],
            "缺失日期": trend["缺失日期"],
            "日汇总序列": [time_series.trend_series_entry(point) for point in daily_points],
            "来源分区": sorted({point["来源分区"] for point in daily_points if point.get("来源分区")}),
            "分组日期": [point["日期"] for point in daily_points],
            "复核编号": review["复核编号"],
            "复核结论": review["结论"],
            "生成时间": datetime.now(timezone.utc).isoformat(),
        }

    def _collect_daily_sources(
        self,
        table_name: str,
        window: list[date],
        partitions: list[dict[str, Any]],
        metric_field: str,
    ) -> list[dict[str, Any]]:
        """收集窗口内的日汇总来源，来源之间保留分区与分组日期。"""
        records: list[dict[str, Any]] = []

        # 1. 本次传入的分区：分区日期决定归属，采集时间只作兜底。
        for item in partitions:
            if time_series.row_table(item) not in ("", table_name):
                continue
            day = time_series.partition_day(item)
            if day is None or not time_series.in_window(day, window[0], window[-1]):
                continue
            records.append({
                "目标表名": table_name,
                "日期": day.strftime(time_series.DATE_FORMAT),
                "来源分区": time_series.partition_id(item, day),
                "采集时间": item.get("采集时间") or item.get("接入时间"),
                "采集批号": time_series.batch_key(item),
                "分组日期": time_series.day_str(item.get("分组日期")) or day.strftime(time_series.DATE_FORMAT),
                "已签收": time_series.is_signed(item),
                "综合质量": time_series.to_number(item.get("综合质量")),
                "指标值": time_series.to_number(item.get("指标值")),
                "基线值": time_series.to_number(item.get("基线值")),
                "完备率": time_series.to_number(item.get("完备率")),
                "准确率": time_series.to_number(item.get("准确率")),
                "及时率": time_series.to_number(item.get("及时率")),
                "指标字段": metric_field or item.get("监控字段", ""),
                "来源": "分区",
                "_raw": item,
            })

        # 2. 趋势看板里已经落地的日汇总（含已签收的历史日期）。
        for row in store.rows(self.MODULE):
            if row.get("目标表名") != table_name:
                continue
            day = time_series.parse_day(row.get("日期"))
            if day is None or not time_series.in_window(day, window[0], window[-1]):
                continue
            records.append({
                "目标表名": table_name,
                "日期": day.strftime(time_series.DATE_FORMAT),
                "来源分区": time_series.partition_id(row, day),
                "采集时间": row.get("采集时间"),
                "采集批号": time_series.batch_key(row),
                "分组日期": time_series.day_str(row.get("分组日期")) or day.strftime(time_series.DATE_FORMAT),
                "已签收": time_series.is_signed(row),
                "综合质量": time_series.to_number(row.get("综合质量")),
                "指标值": time_series.to_number(row.get("指标值")),
                "基线值": time_series.to_number(row.get("基线值")),
                "完备率": time_series.to_number(row.get("完备率")),
                "准确率": time_series.to_number(row.get("准确率")),
                "及时率": time_series.to_number(row.get("及时率")),
                "指标字段": metric_field or row.get("指标字段", ""),
                "来源": "趋势看板",
                "_raw": row,
            })

        # 3. 质量评分卡：评分时间即业务日期，聚合为当天日汇总。
        scorecard_rows = [
            row for row in store.rows("dq_scorecard")
            if row.get("目标表名") == table_name
        ]
        for day in window:
            matches = []
            for row in scorecard_rows:
                row_day = time_series.parse_day(row.get("评分时间") or row.get("日期"))
                if row_day == day:
                    matches.append(row)
            if not matches:
                continue
            records.append({
                "目标表名": table_name,
                "日期": day.strftime(time_series.DATE_FORMAT),
                "来源分区": f"dt={day.strftime(time_series.DATE_FORMAT)}",
                "采集时间": max(
                    (str(row.get("评分时间", "")) for row in matches), default="",
                ),
                "采集批号": time_series.batch_key(matches[-1]),
                "分组日期": day.strftime(time_series.DATE_FORMAT),
                "已签收": any(time_series.is_signed(row) for row in matches),
                "综合质量": self._average(matches, "综合得分"),
                "完备率": self._average(matches, "完整性得分"),
                "准确率": self._average(matches, "准确性得分"),
                "及时率": self._average(matches, "时效性得分"),
                "指标值": None,
                "基线值": None,
                "指标字段": metric_field,
                "来源": "评分卡",
            })

        return records

    def _merge_daily_sources(
        self, table_name: str, window: list[date], records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """同一日期合并多来源记录：分区优先，签收记录优先，其余按采集时间取最新。"""
        winners: dict[date, dict[str, Any]] = {}
        revisions: dict[date, list[dict[str, Any]]] = {}
        counts: dict[date, int] = {}

        for index, record in enumerate(records):
            day = time_series.parse_day(record.get("日期"))
            if day is None:
                continue
            counts[day] = counts.get(day, 0) + 1
            rank = (
                1 if record.get("已签收") else 0,
                1 if record.get("来源") == "分区" else 0,
                time_series.parse_day(record.get("采集时间")) or date.min,
                index,
            )
            current = winners.get(day)
            if current is None:
                winners[day] = record
                continue
            current_rank = (
                1 if current.get("已签收") else 0,
                1 if current.get("来源") == "分区" else 0,
                time_series.parse_day(current.get("采集时间")) or date.min,
                -1,
            )
            if rank > current_rank:
                revisions.setdefault(day, []).append(self._snapshot(current))
                winners[day] = record
            else:
                revisions.setdefault(day, []).append(self._snapshot(record))

        points: list[dict[str, Any]] = []
        superseded: list[dict[str, Any]] = []
        for day in window:
            winner = winners.get(day)
            if winner is None:
                points.append({
                    "日期": day.strftime(time_series.DATE_FORMAT),
                    "分组日期": day.strftime(time_series.DATE_FORMAT),
                    "来源分区": "",
                    "采集时间": "",
                    "采集批号": "",
                    "已签收": False,
                    "综合质量": None,
                    "完备率": None,
                    "准确率": None,
                    "及时率": None,
                    "指标值": None,
                    "基线值": None,
                    "指标字段": "",
                    "状态": "未出数",
                    "来源": "",
                    "批次数": 0,
                    "去重批次数": 0,
                    "后续修订": [],
                })
                continue
            later = revisions.get(day, [])
            if winner.get("已签收"):
                superseded.extend(
                    {**revision, "目标表名": table_name, "业务日期": day.strftime(time_series.DATE_FORMAT)}
                    for revision in later
                )
            # 分区记录负责来源与分组日期；质量字段从同一天其它来源补齐，
            # 这样监控入口先落地的日汇总不会因为分区入参缺字段而丢掉指标值。
            filler = [winner, *[
                record for record in records
                if record is not winner
                and time_series.parse_day(record.get("日期")) == day
            ]]
            points.append({
                "日期": day.strftime(time_series.DATE_FORMAT),
                "分组日期": winner.get("分组日期") or day.strftime(time_series.DATE_FORMAT),
                "来源分区": winner.get("来源分区", ""),
                "采集时间": time_series._stringify(winner.get("采集时间")),
                "采集批号": winner.get("采集批号", ""),
                "已签收": winner.get("已签收", False),
                "综合质量": self._first_value(filler, "综合质量"),
                "完备率": self._first_value(filler, "完备率"),
                "准确率": self._first_value(filler, "准确率"),
                "及时率": self._first_value(filler, "及时率"),
                "指标值": self._first_value(filler, "指标值"),
                "基线值": self._first_value(filler, "基线值"),
                "指标字段": self._first_value(filler, "指标字段") or "",
                "状态": "已签收" if winner.get("已签收") else "正常",
                "来源": winner.get("来源", ""),
                "批次数": counts.get(day, 1),
                "去重批次数": max(counts.get(day, 1) - 1, 0),
                "后续修订": later if winner.get("已签收") else [],
            })
        return points, superseded

    @staticmethod
    def _first_value(records: list[dict[str, Any]], field: str) -> Any:
        for record in records:
            value = record.get(field)
            if value is not None and str(value).strip() != "":
                return value
        return None

    def _summarize(self, daily_points: list[dict[str, Any]], window: list[date]) -> dict[str, Any]:
        values = [point["综合质量"] for point in daily_points if point.get("综合质量") is not None]
        valid = len(values)
        missing = len(window) - valid
        direction = "平稳"
        change_rate = 0.0
        if valid >= 3:
            half = max(valid // 2, 1)
            avg_first = sum(values[:half]) / len(values[:half])
            avg_second = sum(values[half:]) / len(values[half:])
            if avg_second > avg_first + 2:
                direction = "上升"
            elif avg_second < avg_first - 2:
                direction = "下降"
            change_rate = (avg_second - avg_first) / avg_first * 100 if avg_first > 0 else 0
        return {
            "趋势方向": direction,
            "变化率数值": change_rate,
            "有效日汇总": valid,
            "缺失日期": missing,
        }

    def _persist_quality_daily(self, table_name: str, daily_points: list[dict[str, Any]]) -> None:
        trend_rows = store.rows(self.MODULE)
        for point in daily_points:
            if point.get("状态") == "未出数":
                continue
            day = time_series.parse_day(point["日期"])
            if day is None:
                continue
            payload = {
                "完备率": self._percent(point.get("完备率")),
                "准确率": self._percent(point.get("准确率")),
                "及时率": self._percent(point.get("及时率")),
                "综合质量": f"{point['综合质量']:.1f}" if point.get("综合质量") is not None else "",
                "指标值": point.get("指标值"),
                "基线值": point.get("基线值"),
                "指标字段": point.get("指标字段", ""),
                "趋势标记": "平稳",
                "来源分区": point.get("来源分区", ""),
                "采集时间": point.get("采集时间", ""),
                "采集批号": point.get("采集批号", ""),
                "分组日期": point.get("分组日期") or point["日期"],
                "已签收": point.get("已签收", False),
                "批次数": point.get("批次数", 1),
                "去重批次数": point.get("去重批次数", 0),
                "后续修订": point.get("后续修订", []),
                "status": "正常",
                "pending": False,
                "abnormal": False,
                "记录类型": "日汇总",
            }
            entry = time_series.upsert_daily_bucket(trend_rows, table_name, day, payload)
            if not entry.get("记录编号"):
                entry["记录编号"] = f"DQ_T-{int(entry.get('id', 0)):04d}"

    def _write_trend_review(
        self,
        *,
        table_name: str,
        window: list[date],
        daily_points: list[dict[str, Any]],
        trend: dict[str, Any],
        revisions: list[dict[str, Any]],
        metric_field: str,
        source: str,
    ) -> dict[str, Any]:
        """日桶确定后把结论写回质量趋势复核清单，并预留本次日汇总序列。"""
        rows = store.rows(REVIEW_MODULE)
        new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        series = [time_series.trend_series_entry(point) for point in daily_points]
        conclusion = self._review_conclusion(trend)
        entry = {
            "id": new_id,
            "复核编号": f"DQ_TR-{new_id:04d}",
            "目标表名": table_name,
            "复核日期": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "窗口起始": window[0].strftime(time_series.DATE_FORMAT),
            "窗口结束": window[-1].strftime(time_series.DATE_FORMAT),
            "日汇总日期": [point["日期"] for point in daily_points],
            "分组日期": [point["分组日期"] for point in daily_points],
            "来源分区": [point["来源分区"] for point in daily_points],
            "日汇总序列": series,
            "指标字段": metric_field,
            "取数来源": source,
            "有效日汇总": trend["有效日汇总"],
            "缺失日期": trend["缺失日期"],
            "趋势方向": trend["趋势方向"],
            "变化率": f"{trend['变化率数值']:+.1f}%",
            "结论": conclusion,
            "后续修订": revisions,
            "已签收日期": [point["日期"] for point in daily_points if point.get("已签收")],
            "status": "已复核",
            "pending": False,
            "abnormal": trend["趋势方向"] == "下降",
        }
        rows.append(entry)
        return entry

    def _review_conclusion(self, trend: dict[str, Any]) -> str:
        if trend["有效日汇总"] == 0:
            return "窗口内无可用日汇总，暂不给出趋势结论"
        if trend["缺失日期"] > trend["有效日汇总"] * 2:
            return f"日汇总覆盖不足（有效{trend['有效日汇总']}天），结论仅供参考"
        if trend["趋势方向"] == "下降":
            return "质量下降，需复核来源分区和分组日期"
        if trend["趋势方向"] == "上升":
            return "质量改善，日汇总日期与来源分区一致"
        return "质量平稳，日汇总日期与来源分区一致"

    def list_trend_reviews(self, table_name: str | None = None) -> list[dict[str, Any]]:
        """查询质量趋势复核清单；可按目标表名过滤。"""
        rows = store.rows(REVIEW_MODULE)
        if table_name:
            return [row for row in rows if row.get("目标表名") == table_name]
        return list(rows)

    def _latest_available_day(self, table_name: str) -> date | None:
        candidates = [
            time_series.parse_day(row.get("日期"))
            for row in store.rows(self.MODULE)
            if row.get("目标表名") == table_name
        ]
        candidates.extend(
            time_series.parse_day(row.get("评分时间") or row.get("日期"))
            for row in store.rows("dq_scorecard")
            if row.get("目标表名") == table_name
        )
        valid = [day for day in candidates if day is not None]
        return max(valid) if valid else None

    def _empty_trend(self, table_name: str, days: int) -> dict[str, Any]:
        return {
            "目标表名": table_name,
            "数据天数": days,
            "窗口起始": "",
            "窗口结束": "",
            "趋势方向": "平稳",
            "变化率": "+0.0%",
            "最新质量": "N/A",
            "最低质量": "N/A",
            "有效日汇总": 0,
            "缺失日期": days,
            "日汇总序列": [],
            "来源分区": [],
            "分组日期": [],
            "复核编号": "",
            "复核结论": "窗口内无可用日汇总，暂不给出趋势结论",
            "生成时间": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _average(rows: list[dict[str, Any]], field: str) -> float | None:
        values = [
            value for value in (time_series.to_number(row.get(field)) for row in rows)
            if value is not None
        ]
        return round(sum(values) / len(values), 2) if values else None

    @staticmethod
    def _percent(value: Any) -> str:
        return f"{value:.1f}%" if isinstance(value, (int, float)) else ""

    @staticmethod
    def _snapshot(point: dict[str, Any]) -> dict[str, Any]:
        return time_series.trend_series_entry(point)

    def _alert_trend_downturn(self, table_name: str, direction: str, change_rate: float) -> None:
        """检测到质量下降趋势，写入质量事件并触发告警。"""
        incident_rows = store.rows("dq_incident")
        new_iid = max((int(r.get("id", 0)) for r in incident_rows), default=0) + 1
        incident_rows.append({
            "id": new_iid,
            "事件编号": f"DQ_I-{new_iid:04d}",
            "关联规则": f"trend:{table_name}",
            "触发时间": datetime.now(timezone.utc).isoformat(),
            "偏离程度": f"{abs(change_rate):.1f}%",
            "影响行数": "全局",
            "严重级别": "warning" if abs(change_rate) > 10 else "info",
            "处理状态": "趋势告警",
            "处理人": "趋势分析引擎",
            "status": "待确认",
            "pending": True,
            "abnormal": True,
        })

    def aggregate_trend_comparison(self, table_names: list[str]) -> dict[str, Any]:
        """聚合多表质量趋势对比，生成综合排名。

        查询多张表的趋势数据，计算质量均值和排名，
        聚合生成跨表趋势对比快照。缺失或未出数的日期不参与均值。
        """
        trend_rows = store.rows(self.MODULE)
        comparison = {}

        for tbl in table_names:
            tbl_trends = [
                t for t in trend_rows
                if t.get("目标表名") == tbl
                and time_series.parse_day(t.get("日期")) is not None
                and time_series.to_number(t.get("综合质量")) is not None
            ]
            if tbl_trends:
                qualities = [time_series.to_number(t.get("综合质量")) for t in tbl_trends]
                avg_quality = sum(qualities) / len(qualities)
                latest = max(
                    tbl_trends,
                    key=lambda t: time_series.parse_day(t.get("日期")) or date.min,
                )
                comparison[tbl] = {
                    "平均质量": round(avg_quality, 1),
                    "最新质量": latest.get("综合质量", "N/A"),
                    "最新日期": time_series.day_str(latest.get("日期")),
                    "趋势": latest.get("趋势标记", "未知"),
                    "样本量": len(tbl_trends),
                }

        ranked = sorted(
            comparison.items(),
            key=lambda x: x[1]["平均质量"],
            reverse=True,
        )

        return {
            "聚合时间": datetime.now(timezone.utc).isoformat(),
            "表数": len(comparison),
            "排名": [{"表名": k, **v} for k, v in ranked],
            "最佳表": ranked[0][0] if ranked else "N/A",
            "最差表": ranked[-1][0] if ranked else "N/A",
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "计算"
        snapshot["desc"] = "按业务日期日汇总计算质量趋势并对比多表排名"
        snapshot["artifact"] = "质量趋势分析数据集"
        snapshot["artifact_desc"] = "含日汇总序列、复核结论和对比排名的质量趋势分析数据集"
        return snapshot
