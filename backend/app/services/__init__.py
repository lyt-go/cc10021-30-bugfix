"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.dq_rule_def import DqRuleDefService
from app.services.dq_check_run import DqCheckRunService
from app.services.dq_incident import DqIncidentService
from app.services.metadata_crawler import MetadataCrawlerService
from app.services.table_catalog import TableCatalogService
from app.services.column_profile import ColumnProfileService
from app.services.lineage_graph import LineageGraphService
from app.services.metric_monitor import MetricMonitorService
from app.services.dq_scorecard import DqScorecardService
from app.services.tag_classify import TagClassifyService
from app.services.dq_template import DqTemplateService
from app.services.anomaly_pattern import AnomalyPatternService
from app.services.dq_report import DqReportService
from app.services.metadata_diff import MetadataDiffService
from app.services.dq_alert_channel import DqAlertChannelService
from app.services.dq_baseline import DqBaselineService
from app.services.mdm_entity import MdmEntityService
from app.services.dq_trend import DqTrendService
from app.services.dq_trend_review import DqTrendReviewService

SERVICES: dict[str, BaseModuleService] = {
    "dq_rule_def": DqRuleDefService(),
    "dq_check_run": DqCheckRunService(),
    "dq_incident": DqIncidentService(),
    "metadata_crawler": MetadataCrawlerService(),
    "table_catalog": TableCatalogService(),
    "column_profile": ColumnProfileService(),
    "lineage_graph": LineageGraphService(),
    "metric_monitor": MetricMonitorService(),
    "dq_scorecard": DqScorecardService(),
    "tag_classify": TagClassifyService(),
    "dq_template": DqTemplateService(),
    "anomaly_pattern": AnomalyPatternService(),
    "dq_report": DqReportService(),
    "metadata_diff": MetadataDiffService(),
    "dq_alert_channel": DqAlertChannelService(),
    "dq_baseline": DqBaselineService(),
    "mdm_entity": MdmEntityService(),
    "dq_trend": DqTrendService(),
    "dq_trend_review": DqTrendReviewService(),
}


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
