"""质量趋势复核清单：沉淀趋势计算结论并预留本次日汇总序列。

domain_chain: 质量趋势 (dq_trend)
  └─ 复核 → 质量趋势复核清单 (dq_trend_review)
  └─ 核对 → 质量报告 (dq_report)
"""

from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService


class DqTrendReviewService(BaseModuleService):
    MODULE = "dq_trend_review"
    ENTITY = "复核记录"
    OPERATION_TERM = "复核"
    LIST_FIELDS = [
        "复核编号", "目标表名", "复核日期", "窗口起始", "窗口结束",
        "趋势方向", "变化率", "结论",
    ]
    REQUIRED_FIELDS = ["复核编号", "目标表名", "结论"]
    STATUS_ORDER = ["已复核", "待复核", "已归档"]
    ACTION_RULES = {"标记待复核": "待复核", "确认复核": "已复核", "归档复核": "已归档"}
    NEGATIVE_ACTIONS = []

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "复核"
        snapshot["desc"] = "沉淀趋势结论并预留日汇总序列、来源分区和分组日期"
        snapshot["artifact"] = "质量趋势复核数据集"
        snapshot["artifact_desc"] = "含日汇总序列和来源分区的复核清单数据集"
        return snapshot
