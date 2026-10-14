"""Source-qualified event comparison and operational lead time for temperature/oxygen.

These are user-defined management crossings, not biological tolerance limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class EventRule:
    max_temp_c: float | None = None
    min_do_mg_l: float | None = None
    source: str = ""
    action_minutes: float | None = None

    def __post_init__(self) -> None:
        if self.max_temp_c is None and self.min_do_mg_l is None:
            raise ValueError("至少设置一个温度上限或溶氧下限才能评价事件")
        for name, value in (("max_temp_c", self.max_temp_c), ("min_do_mg_l", self.min_do_mg_l)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value)):
                raise ValueError(f"{name} 必须是有限数值")
        if self.min_do_mg_l is not None and self.min_do_mg_l < 0:
            raise ValueError("溶氧浓度下限不能为负数；输入单位必须是 mg/L")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("管理限值必须注明来源及适用条件")
        if self.action_minutes is not None and (
            isinstance(self.action_minutes, bool)
            or not isinstance(self.action_minutes, Real)
            or not isfinite(self.action_minutes)
            or self.action_minutes < 0
        ):
            raise ValueError("措施所需时间须为非负的有限分钟数")


def _episodes(flags: np.ndarray) -> list[tuple[int, int]]:
    """Return half-open contiguous runs of threshold crossings."""
    starts = np.flatnonzero(flags & ~np.r_[False, flags[:-1]])
    ends = np.flatnonzero(flags & ~np.r_[flags[1:], False]) + 1
    return list(zip(starts.tolist(), ends.tolist()))


def _score(actual: np.ndarray, predicted: np.ndarray, step_minutes: float) -> dict:
    tp = int(np.count_nonzero(actual & predicted))
    fp = int(np.count_nonzero(~actual & predicted))
    fn = int(np.count_nonzero(actual & ~predicted))
    actual_events = _episodes(actual)
    predicted_events = _episodes(predicted)
    actual_duration = int(actual.sum()) * step_minutes
    predicted_duration = int(predicted.sum()) * step_minutes
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "actual_event_episodes": len(actual_events),
        "predicted_event_episodes": len(predicted_events),
        "missed_event_episodes": sum(not predicted[start:stop].any() for start, stop in actual_events),
        "false_alarm_episodes": sum(not actual[start:stop].any() for start, stop in predicted_events),
        "actual_duration_minutes": actual_duration,
        "predicted_duration_minutes": predicted_duration,
        "duration_bias_minutes": predicted_duration - actual_duration,
    }


def score_replay(rows: list[dict], rule: EventRule, cadence_ns: int) -> dict:
    """Score observed vs modeled final-horizon event states on real target times."""
    if not rows or not isinstance(rule, EventRule) or cadence_ns <= 0:
        raise ValueError("事件评价需要真实历史目标、用户规则与正采样间隔")
    flags: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for label, value, actual_key, predicted_key, comparator in (
        ("temperature", rule.max_temp_c, "actual_temperature_c", "predicted_temperature_c", np.greater),
        ("oxygen", rule.min_do_mg_l, "actual_dissolved_oxygen_mg_l", "predicted_dissolved_oxygen_mg_l", np.less),
    ):
        if value is None:
            continue
        actual_values = np.asarray([row[actual_key] for row in rows], dtype=np.float64)
        predicted_values = np.asarray([row[predicted_key] for row in rows], dtype=np.float64)
        if not np.isfinite(actual_values).all() or not np.isfinite(predicted_values).all():
            raise ValueError("事件评价不能使用缺失或无穷大的预测与实测值")
        flags[label] = (comparator(actual_values, value), comparator(predicted_values, value))
    step_minutes = cadence_ns / 60_000_000_000
    by_variable = {label: _score(truth, estimate, step_minutes) for label, (truth, estimate) in flags.items()}
    if len(flags) == 2:
        keys = list(flags)
        by_variable["combined"] = _score(
            flags[keys[0]][0] | flags[keys[1]][0],
            flags[keys[0]][1] | flags[keys[1]][1],
            step_minutes,
        )
    warnings = []
    for label, metrics in by_variable.items():
        if metrics["recall"] is None:
            warnings.append(f"{label}：此回放时段没有实测超限点，召回率无法计算。")
        if metrics["precision"] is None:
            warnings.append(f"{label}：此回放时段没有预测超限点，精确率无法计算。")
    return {"by_variable": by_variable, "warnings": warnings, "source": rule.source}


def forecast_lead(
    predicted: pd.DataFrame, issued_at: pd.Timestamp, rule: EventRule, *, as_of: pd.Timestamp | None = None
) -> dict:
    """Compare first predicted crossing with both last observation and current time."""
    if not isinstance(rule, EventRule) or not isinstance(predicted, pd.DataFrame) or predicted.empty:
        raise ValueError("提前量需要非空预测结果和有来源的管理规则")
    issued = pd.Timestamp(issued_at)
    issued = issued.tz_localize("UTC") if issued.tzinfo is None else issued.tz_convert("UTC")
    now = pd.Timestamp.now(tz="UTC") if as_of is None else pd.Timestamp(as_of)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    for column in ("temperature_c", "dissolved_oxygen_mg_l"):
        if column not in predicted or not np.isfinite(predicted[column].to_numpy(dtype=float)).all():
            raise ValueError("预测结果中温度和溶氧必须是有限数值")
    temp_flags = predicted["temperature_c"].to_numpy(dtype=float) > rule.max_temp_c if rule.max_temp_c is not None else np.zeros(len(predicted), dtype=bool)
    oxygen_flags = predicted["dissolved_oxygen_mg_l"].to_numpy(dtype=float) < rule.min_do_mg_l if rule.min_do_mg_l is not None else np.zeros(len(predicted), dtype=bool)
    disclaimer = (
        "仅比较模型预测与用户提供的管理限值；限值并非通用鱼类生理安全线。"
        "提前量先按最后实测时刻计算，可执行时间还需扣除数据滞后；过期的历史预测不能作为当下预警。"
        "未见预测超限不代表安全。"
    )
    first = np.flatnonzero(temp_flags | oxygen_flags)
    if not len(first):
        return {
            "status": "no_crossing",
            "first_crossing_timestamp": None,
            "minutes_until": None,
            "remaining_minutes_at_generation": None,
            "generated_at": now.isoformat(),
            "expired": None,
            "triggered_variables": [],
            "action_minutes": rule.action_minutes,
            "action_feasible": None,
            "source": rule.source,
            "disclaimer": disclaimer,
        }
    index = int(first[0])
    crossing = pd.Timestamp(predicted["timestamp"].iloc[index])
    crossing = crossing.tz_localize("UTC") if crossing.tzinfo is None else crossing.tz_convert("UTC")
    minutes = (crossing - issued).total_seconds() / 60
    remaining = (crossing - now).total_seconds() / 60
    if not isfinite(minutes) or minutes <= 0:
        raise ValueError("预测时间应晚于最后实测时刻")
    expired = remaining <= 0
    triggered = []
    if temp_flags[index]:
        triggered.append("temperature_c")
    if oxygen_flags[index]:
        triggered.append("dissolved_oxygen_mg_l")
    return {
        "status": "crossing",
        "first_crossing_timestamp": crossing.isoformat(),
        "minutes_until": minutes,
        "remaining_minutes_at_generation": remaining,
        "generated_at": now.isoformat(),
        "expired": expired,
        "triggered_variables": triggered,
        "action_minutes": rule.action_minutes,
        "action_feasible": (not expired and remaining >= rule.action_minutes) if rule.action_minutes is not None else None,
        "source": rule.source,
        "disclaimer": disclaimer,
    }
