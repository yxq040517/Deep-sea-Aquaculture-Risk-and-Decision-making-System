"""Export raw short-horizon forecasts and recorded model evaluation results."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


_COLUMNS = {
    "timestamp": "预测时间",
    "site": "观测序列 / 场址",
    "depth_m": "观测水深 (m)",
    "temperature_c": "预测水温 (°C)",
    "dissolved_oxygen_mg_l": "预测溶氧 (mg/L)",
}


def _require_forecast(frame: pd.DataFrame) -> None:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("请先生成非空的预测结果")
    if not set(_COLUMNS).issubset(frame.columns):
        raise ValueError("预测结果字段不完整，无法导出")


def _csv_site(value: str) -> str:
    """Stop a user-supplied site identifier becoming a spreadsheet formula."""
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def export_csv(path: str | Path, forecast: pd.DataFrame) -> Path:
    """Write only the numeric forecast and its series identifiers, never a risk claim."""
    _require_forecast(forecast)
    destination = Path(path)
    if destination.suffix.lower() != ".csv":
        raise ValueError("导出路径须为 .csv 文件")
    data = forecast.loc[:, list(_COLUMNS)].copy()
    data["site"] = data["site"].map(_csv_site)
    data.to_csv(destination, index=False, encoding="utf-8-sig")
    return destination


def _cell(sheet, row: int, column: int, value) -> None:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime() if value.tzinfo is None else value.isoformat()
    elif isinstance(value, datetime) and value.tzinfo is not None:
        value = value.isoformat()
    item = sheet.cell(row, column, value)
    # Excel must treat imported labels as literal text even when they start with '='.
    if isinstance(value, str):
        item.data_type = "s"
    item.alignment = Alignment(vertical="center", wrap_text=True)


def _header(sheet, labels: list[str]) -> None:
    for column, label in enumerate(labels, 1):
        _cell(sheet, 1, column, label)
        item = sheet.cell(1, column)
        item.fill = PatternFill("solid", fgColor="17365D")
        item.font = Font(color="FFFFFF", bold=True)
    sheet.row_dimensions[1].height = 27
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False


def _widths(sheet, widths: list[int]) -> None:
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def export_excel(path: str | Path, forecast: pd.DataFrame, summary: dict,
                 rule: dict | None = None, lead_time: dict | None = None,
                 issued_at: pd.Timestamp | None = None, dataset: dict | None = None) -> Path:
    """Save forecasts, trained evaluation, dataset provenance, and sourced rule/lead."""
    _require_forecast(forecast)
    if not isinstance(summary, dict) or summary.get("schema_version") != 1:
        raise ValueError("模型评估记录不存在或格式不兼容")
    destination = Path(path)
    if destination.suffix.lower() != ".xlsx":
        raise ValueError("导出路径须为 .xlsx 文件")
    book = Workbook()
    sheet = book.active
    sheet.title = "短时预测"
    fields = list(_COLUMNS)
    _header(sheet, [*(_COLUMNS[key] for key in fields), "数据类型"])
    for index, record in enumerate(forecast.loc[:, fields].itertuples(index=False, name=None), 2):
        for column, value in enumerate(record, 1):
            _cell(sheet, index, column, value)
        _cell(sheet, index, len(fields) + 1, "模型预测，非实测")
    sheet.auto_filter.ref = sheet.dimensions
    _widths(sheet, [23, 27, 18, 22, 24, 24])

    metrics = book.create_sheet("模型评估")
    _header(metrics, ["数据分段", "变量与单位", "LSTM MAE", "末值基线 MAE"])
    row = 2
    for split, title in (("validation", "验证段"), ("test", "独立测试段")):
        measured = summary.get("metrics", {}).get(split, {})
        for feature, label in (("temperature_c", "水温 / ℃"),
                               ("dissolved_oxygen_mg_l", "溶氧 / mg/L")):
            for column, value in enumerate((title, label, measured.get("mae", {}).get(feature),
                                             measured.get("naive_mae", {}).get(feature)), 1):
                _cell(metrics, row, column, value)
            row += 1
    _widths(metrics, [22, 24, 20, 24])

    info = book.create_sheet("模型信息")
    _header(info, ["记录项", "训练时的值"])
    values = [
        ("模型", "LSTM，多步水温与溶氧预测"),
        ("历史输入 / 采样步", summary["lookback"]),
        ("预测时距 / 采样步", summary["horizon"]),
        ("采样间隔", summary["cadence"]),
        ("验证分段策略", summary.get("split_mode", "未记录（旧模型）")),
        ("训练样本数", summary["sample_counts"]["train"]),
        ("验证样本数", summary["sample_counts"]["validation"]),
        ("测试样本数", summary["sample_counts"]["test"]),
        ("适用范围", "本次短时水温与溶氧预测；非全年适养判断"),
    ]
    for index, (label, value) in enumerate(values, 2):
        _cell(info, index, 1, label)
        _cell(info, index, 2, value)
    _widths(info, [34, 73])
    if dataset is not None:
        source = book.create_sheet("观测数据来源")
        _header(source, ["证据项", "原始来源记录 / 自动解析"])
        provenance = dataset.get("provenance") or {}
        fields = [
            ("来源名称", provenance.get("source_name", "未提供")),
            ("来源出处", provenance.get("source_reference", "未提供")),
            ("来源时区备注（未自动转换）", provenance.get("timezone_note", "未提供")),
            ("导入时自动时间解释", (dataset.get("quality") or {}).get("timezone_interpretation", "未记录；不可核实")),
            ("深度依据", provenance.get("depth_basis", "unknown")),
            ("原始 QC 说明", provenance.get("qc_note", "未提供")),
            ("原始水温单位声明", provenance.get("temperature_unit", "未提供")),
            ("原始溶氧单位声明", provenance.get("oxygen_unit", "未提供")),
            ("文件 SHA-256", dataset.get("sha256") or "未记录"),
            ("说明", "来源与单位声明均为用户提供，不代表传感器质量或数据真实性已经核验。"),
        ]
        for index, (label, value) in enumerate(fields, 2):
            _cell(source, index, 1, label)
            _cell(source, index, 2, value)
        _widths(source, [39, 95])
    if rule is not None:
        limits = book.create_sheet("管理限值与提前量")
        _header(limits, ["记录项", "用户设置 / 模型预测"])
        details = [
            ("规则来源及适用条件", rule["source"]),
            ("水温上限 (°C)", rule.get("max_temp_c")),
            ("溶氧下限 (mg/L)", rule.get("min_do_mg_l")),
            ("措施执行所需时间 (分钟)", rule.get("action_minutes")),
            ("预测起点（最后实测时间）", issued_at),
            ("平台生成时间（UTC）", lead_time.get("generated_at") if lead_time else None),
            ("生成时剩余时间（分钟）", lead_time.get("remaining_minutes_at_generation") if lead_time else None),
            ("预测时刻已经过期", "是" if lead_time and lead_time.get("expired") else "否" if lead_time and lead_time.get("expired") is False else "未确定"),
            ("预测超限状态", lead_time["status"] if lead_time else "未计算"),
            ("预测首次超限时间", lead_time.get("first_crossing_timestamp") if lead_time else None),
            ("相对最后实测的提前量（分钟）", lead_time.get("minutes_until") if lead_time else None),
            ("预测触发变量", ", ".join(lead_time.get("triggered_variables", [])) if lead_time else None),
            ("生成时措施时间是否足够", "预测时刻已过期" if lead_time and lead_time.get("expired")
             else "是" if lead_time and lead_time.get("action_feasible") is True
             else "否" if lead_time and lead_time.get("action_feasible") is False else "未确定"),
            ("解释", (lead_time["disclaimer"] + " 提前量以最后实测时间为起点；过期历史记录不代表现在仍可执行。")
             if lead_time else "未配置管理限值；无法判断预测超限。"),
        ]
        for index, (label, value) in enumerate(details, 2):
            _cell(limits, index, 1, label)
            _cell(limits, index, 2, value)
        _widths(limits, [37, 93])
    book.save(destination)
    return destination
