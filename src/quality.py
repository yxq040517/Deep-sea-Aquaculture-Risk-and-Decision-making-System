"""Evidence retained from source observations without inventing sensor QC decisions."""

from __future__ import annotations

import math
from pathlib import Path
import re

import pandas as pd


_REQUIRED = ("timestamp", "site", "depth_m", "temperature_c", "dissolved_oxygen_mg_l")
_FLAG_NAME = re.compile(r"(^f[_-]|[_-](?:flag|qc|quality)$|^(?:flag|qc)[_-])", re.I)
_PERCENT_OXYGEN = re.compile(r"(?:oxygen|do|溶氧|氧饱和).*(?:%|percent|saturation|饱和度)|(?:%|percent|saturation|饱和度).*(?:oxygen|do|溶氧|氧饱和)", re.I)
_EXPLICIT_OFFSET = re.compile(r"(?:[zZ]|[+-]\d{2}:?\d{2})$")


def read_raw(path: Path) -> pd.DataFrame:
    """Use exactly the parser/encoding selection of the model's observation reader."""
    if path.suffix.lower() == ".xlsx":
        return pd.read_excel(path)
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="gb18030")


def reject_percent_oxygen(raw: pd.DataFrame) -> None:
    """A saturation-percent column is not the required dissolved oxygen mg/L."""
    columns = [str(column).strip() for column in raw.columns]
    oxygen_column = next((column for column in raw.columns if str(column).strip() == "dissolved_oxygen_mg_l"), None)
    if oxygen_column is None:
        if any(_PERCENT_OXYGEN.search(column) for column in columns):
            raise ValueError("溶氧饱和度 (%) 不是溶解氧浓度 (mg/L)；请提供 dissolved_oxygen_mg_l 实测浓度列。")
        return
    values = raw[oxygen_column]
    if isinstance(values, pd.DataFrame):
        return  # Duplicate names will be rejected by the core parser.
    if values.astype("string").str.contains(r"%|percent|saturation|饱和度", case=False, regex=True, na=False).any():
        raise ValueError("dissolved_oxygen_mg_l 中包含百分比/饱和度，不可作为 mg/L 浓度；请提供实测 mg/L。")


def unknown_quality() -> dict:
    return {
        "duplicate_count": None,
        "duplicate_periods": [],
        "missing_count": None,
        "missing_periods": [],
        "qc_flags": {},
        "timezone_interpretation": "历史数据未记录时区解析信息；不能确认原始时间的时区。",
        "warnings": ["历史数据缺少质量记录，重复、缺测、QC 和探头深度状态未核实。"],
    }


def assess_quality(raw: pd.DataFrame, depth_basis: str = "unknown") -> dict:
    """Describe raw gaps/duplicates; uncertain cadence yields only suspected episodes."""
    quality = {
        "duplicate_count": 0,
        "duplicate_periods": [],
        "missing_count": 0,
        "missing_periods": [],
        "qc_flags": {},
        "timezone_interpretation": "带偏移时间按 UTC 归一化后去掉时区；无偏移时间按 UTC 解释（原始时区未经核实）。",
        "warnings": [],
    }
    if depth_basis != "measured":
        quality["warnings"].append("探头深度为标称值或未注明，depth_m 不代表已核实的实测探头深度。")
    columns = [str(column).strip() for column in raw.columns]
    for column in raw.columns:
        label = str(column).strip()
        if label not in _REQUIRED and _FLAG_NAME.search(label):
            frequencies = raw[column].astype("string").fillna("未提供").value_counts(dropna=False)
            quality["qc_flags"][str(column)] = {str(value): int(count) for value, count in frequencies.items()}
    if quality["qc_flags"]:
        quality["warnings"].append("原始 QC 标记仅计数展示；未指定其含义，不据此筛选、校正或判定阈值。")
    if not set(_REQUIRED[:3]).issubset(columns):
        quality["missing_count"] = None
        quality["warnings"].append("缺少时间、站点或深度列，无法评估采样间断。")
        return quality
    data = raw.copy()
    data.columns = columns
    if data.columns.has_duplicates:
        quality["missing_count"] = None
        quality["warnings"].append("列名重复，无法评估采样间断。")
        return quality
    raw_times = data["timestamp"].astype("string").str.strip()
    explicit = raw_times.str.contains(_EXPLICIT_OFFSET, na=False)
    if explicit.any() and (~explicit).any():
        quality["warnings"].append("同时存在带时区与不带时区时间；无时区时间被按 UTC 解释，须核实源记录。")
    timestamps = pd.to_datetime(data["timestamp"], errors="coerce", format="mixed", utc=True)
    if timestamps.isna().any():
        quality["missing_count"] = None
        quality["warnings"].append("时间戳存在无效值，无法完整评估采样间断。")
        return quality
    data["timestamp"] = timestamps.dt.tz_localize(None)
    sites = data["site"].astype("string").str.strip()
    depths = pd.to_numeric(data["depth_m"], errors="coerce")
    if sites.isna().any() or sites.eq("").any() or depths.isna().any() or not depths.map(math.isfinite).all():
        quality["missing_count"] = None
        quality["warnings"].append("站点或深度含空值，无法完整评估采样间断。")
        return quality
    data["site"] = sites
    data["depth_m"] = depths
    groups = []
    for (site, depth), group in data.groupby(["site", "depth_m"], sort=True):
        counts = group["timestamp"].value_counts()
        for timestamp, count in counts.items():
            if count > 1:
                quality["duplicate_periods"].append({"site": str(site), "depth_m": float(depth),
                                                     "timestamp": timestamp.isoformat(), "count": int(count)})
                quality["duplicate_count"] += int(count) - 1
        ordered = group["timestamp"].drop_duplicates().sort_values()
        if len(ordered) > 1:
            groups.append((str(site), float(depth), ordered))
    if quality["duplicate_count"]:
        quality["warnings"].append("同一站点/深度/时间出现重复观测，原始数值未被自动合并。")
    if not groups:
        quality["missing_count"] = None
        quality["warnings"].append("有效时间点不足，无法确定采样间隔和缺测次数。")
        return quality
    steps = pd.concat([ordered.diff().iloc[1:] for _, _, ordered in groups])
    frequencies = steps.value_counts()
    top = frequencies[frequencies == frequencies.max()]
    if len(top) != 1:
        quality["missing_count"] = None
        quality["warnings"].append("常见采样间隔并列；以下疑似缺测时段基于最短观测间隔，次数未核实。")
        cadence = steps.min()
    else:
        cadence = top.index[0]
    if (steps != cadence).any():
        quality["warnings"].append(f"以常见间隔 {cadence} 描述疑似缺测；原始序列不规则，上传不会自动填补。")
    else:
        return quality
    for site, depth, ordered in groups:
        previous = ordered.shift(1)
        for start, end in zip(previous.iloc[1:], ordered.iloc[1:]):
            gap = end - start
            if gap <= cadence:
                continue
            if gap.value % cadence.value:
                quality["missing_count"] = None
                quality["warnings"].append("存在非整数倍采样间隔；缺测次数无法可靠确定。")
                continue
            missing = int(gap.value // cadence.value - 1)
            quality["missing_periods"].append({"site": site, "depth_m": depth,
                                               "from": start.isoformat(), "to": end.isoformat(),
                                               "missing_steps": missing})
            if quality["missing_count"] is not None:
                quality["missing_count"] += missing
    return quality
