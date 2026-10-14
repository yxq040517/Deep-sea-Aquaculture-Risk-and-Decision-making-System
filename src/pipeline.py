"""Validated ocean observations and a reloadable, CPU-only LSTM forecaster."""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Callable

import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
import numpy as np
import pandas as pd


FEATURES = ("temperature_c", "dissolved_oxygen_mg_l")
COLUMNS = ("timestamp", "site", "depth_m", *FEATURES)
_HIDDEN_SIZE = 32
_BATCH_SIZE = 64


class _ForecastLSTM(nn.Module):
    def __init__(self, horizon: int, hidden_size: int = _HIDDEN_SIZE) -> None:
        super().__init__()
        self.lstm = nn.LSTM(input_size=len(FEATURES), hidden_size=hidden_size, batch_first=True)
        self.output = nn.Linear(hidden_size, horizon * len(FEATURES))
        self.horizon = horizon

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        _, (hidden, _) = self.lstm(inputs)
        return self.output(hidden[-1]).reshape(-1, self.horizon, len(FEATURES))


def _prepare_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Observations must be a pandas DataFrame.")
    data = frame.copy()
    data.columns = [str(column).strip() for column in data.columns]
    if data.columns.has_duplicates:
        raise ValueError("Observation column names must be unique (also after removing whitespace).")
    missing = [column for column in COLUMNS if column not in data.columns]
    if missing:
        raise ValueError(f"Missing required observation columns: {', '.join(missing)}.")
    if data.empty:
        raise ValueError("The observation file contains no rows.")
    data = data.loc[:, list(COLUMNS)].copy()

    try:
        parsed = pd.to_datetime(data["timestamp"], errors="coerce", format="mixed", utc=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Cannot parse observation timestamps: {exc}") from exc
    invalid = parsed.isna().to_numpy()
    if invalid.any():
        raise ValueError(f"Invalid or missing timestamp at data row {int(np.flatnonzero(invalid)[0]) + 2}.")
    # UTC-normalized naive timestamps make timezone-aware and timezone-naive files comparable.
    data["timestamp"] = parsed.dt.tz_localize(None)

    sites = data["site"].astype("string").str.strip()
    invalid = (sites.isna() | sites.eq("")).fillna(True).to_numpy(dtype=bool)
    if invalid.any():
        raise ValueError(f"Missing or blank site at data row {int(np.flatnonzero(invalid)[0]) + 2}.")
    data["site"] = sites.astype(str)

    for column in ("depth_m", *FEATURES):
        numeric = pd.to_numeric(data[column], errors="coerce")
        try:
            values = numeric.to_numpy(dtype=np.float64, na_value=np.nan)
            finite = np.isfinite(values)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"Column {column} must contain finite numeric values: {exc}") from exc
        if not finite.all():
            raise ValueError(
                f"Column {column} has a missing, nonnumeric, or infinite value "
                f"at data row {int(np.flatnonzero(~finite)[0]) + 2}."
            )
        data[column] = values

    data.sort_values(["site", "depth_m", "timestamp"], kind="mergesort", inplace=True)
    data.reset_index(drop=True, inplace=True)
    cadence_ns: int | None = None
    for (site, depth), group in data.groupby(["site", "depth_m"], sort=False):
        if len(group) < 2:
            raise ValueError(f"Site {site!r} at depth {depth:g} m needs at least two observations to establish cadence.")
        steps = group["timestamp"].diff().iloc[1:]
        if (steps <= pd.Timedelta(0)).any():
            raise ValueError(f"Duplicate or out-of-order timestamps at site {site!r}, depth {depth:g} m.")
        series_cadence = int(steps.iloc[0].value)
        if (steps != steps.iloc[0]).any():
            raise ValueError(f"Irregular observation cadence at site {site!r}, depth {depth:g} m.")
        if cadence_ns is None:
            cadence_ns = series_cadence
        elif series_cadence != cadence_ns:
            raise ValueError(
                f"Inconsistent cadence: site {site!r}, depth {depth:g} m uses "
                f"{pd.Timedelta(series_cadence, unit='ns')}; expected {pd.Timedelta(cadence_ns, unit='ns')}."
            )
    assert cadence_ns is not None
    return data, cadence_ns


def read_observations(path: str | Path) -> pd.DataFrame:
    """Read a CSV (UTF-8/GB18030) or XLSX and validate independent regular series."""
    source = Path(path)
    try:
        if source.suffix.lower() == ".csv":
            try:
                frame = pd.read_csv(source, encoding="utf-8-sig")
            except UnicodeDecodeError:
                frame = pd.read_csv(source, encoding="gb18030")
        elif source.suffix.lower() == ".xlsx":
            frame = pd.read_excel(source)
        else:
            raise ValueError("Only .csv (UTF-8 or GB18030) and .xlsx observation files are supported.")
    except (OSError, UnicodeError, pd.errors.ParserError, ValueError, ImportError) as exc:
        if isinstance(exc, ValueError) and "observation files are supported" in str(exc):
            raise
        raise ValueError(f"Cannot read observations from {source}: {exc}") from exc
    try:
        return _prepare_frame(frame)[0]
    except ValueError as exc:
        raise ValueError(f"Invalid observations in {source}: {exc}") from exc


def series_keys(frame: pd.DataFrame) -> list[tuple[str, float]]:
    """List validated site/depth series in deterministic order."""
    data, _ = _prepare_frame(frame)
    return [(str(site), float(depth)) for site, depth in data.groupby(["site", "depth_m"], sort=True).groups]


def _require_positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def _partition_bounds(length: int) -> dict[str, tuple[int, int]]:
    train_end = int(length * 0.70)
    validation_end = int(length * 0.85)
    return {
        "train": (0, train_end),
        "validation": (train_end, validation_end),
        "test": (validation_end, length),
    }


def _window_indices(start: int, stop: int, lookback: int, horizon: int) -> range:
    # The entire target block stays in its partition; input is strictly earlier.
    return range(max(lookback, start), stop - horizon + 1)


def _calculate_metrics(
    model: _ForecastLSTM, dataset: TensorDataset, std: np.ndarray, horizon: int
) -> dict[str, dict[str, float]]:
    errors = np.zeros(len(FEATURES), dtype=np.float64)
    naive_errors = np.zeros(len(FEATURES), dtype=np.float64)
    std_tensor = torch.from_numpy(std)
    model.eval()
    with torch.no_grad():
        for history, targets in DataLoader(dataset, batch_size=_BATCH_SIZE):
            prediction = model(history)
            errors += (torch.abs(prediction - targets) * std_tensor).sum(dim=(0, 1)).numpy()
            persistence = history[:, -1:, :].expand(-1, horizon, -1)
            naive_errors += (torch.abs(persistence - targets) * std_tensor).sum(dim=(0, 1)).numpy()
    denominator = len(dataset) * horizon
    return {
        "mae": {feature: float(error / denominator) for feature, error in zip(FEATURES, errors)},
        "naive_mae": {
            feature: float(error / denominator) for feature, error in zip(FEATURES, naive_errors)
        },
    }


def train_lstm(
    frame: pd.DataFrame,
    output_dir: str | Path,
    lookback: int = 24,
    horizon: int = 6,
    epochs: int = 20,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Train on chronological series, retain best validation loss, and report held-out MAEs."""
    for name, value in (("lookback", lookback), ("horizon", horizon), ("epochs", epochs)):
        _require_positive_integer(value, name)
    data, cadence_ns = _prepare_frame(frame)
    groups: list[tuple[str, float, np.ndarray, dict[str, tuple[int, int]]]] = []
    site_details: list[dict] = []
    for (site, depth), group in data.groupby(["site", "depth_m"], sort=True):
        bounds = _partition_bounds(len(group))
        counts = {
            partition: len(_window_indices(*limits, lookback, horizon))
            for partition, limits in bounds.items()
        }
        if not all(counts.values()):
            raise ValueError(
                f"Insufficient observations for site {site!r}, depth {depth:g} m: "
                f"{len(group)} rows with lookback={lookback} and horizon={horizon} yield "
                f"train={counts['train']}, validation={counts['validation']}, test={counts['test']} "
                "complete target windows; each split requires at least one."
            )
        groups.append((str(site), float(depth), group.loc[:, list(FEATURES)].to_numpy(dtype=np.float64), bounds))
        site_details.append(
            {
                "site": str(site),
                "depth_m": float(depth),
                "observations": len(group),
                "train_samples": counts["train"],
                "validation_samples": counts["validation"],
                "test_samples": counts["test"],
            }
        )

    train_values = np.concatenate([values[:bounds["train"][1]] for _, _, values, bounds in groups])
    mean = train_values.mean(axis=0)
    fitted_std = train_values.std(axis=0)
    if not np.isfinite(mean).all() or not np.isfinite(fitted_std).all():
        raise ValueError("Training values are too large to compute stable normalization statistics.")
    std = np.where(fitted_std == 0, 1.0, fitted_std)
    samples: dict[str, tuple[list[np.ndarray], list[np.ndarray]]] = {
        partition: ([], []) for partition in ("train", "validation", "test")
    }
    for _, _, values, bounds in groups:
        normalized = ((values - mean) / std).astype(np.float32)
        if not np.isfinite(normalized).all():
            raise ValueError("Normalization produced nonfinite values; check observation magnitudes.")
        for partition, (start, stop) in bounds.items():
            histories, targets = samples[partition]
            for index in _window_indices(start, stop, lookback, horizon):
                histories.append(normalized[index - lookback : index])
                targets.append(normalized[index : index + horizon])
    datasets = {
        partition: TensorDataset(torch.from_numpy(np.stack(histories)), torch.from_numpy(np.stack(targets)))
        for partition, (histories, targets) in samples.items()
    }

    torch.manual_seed(42)
    model = _ForecastLSTM(horizon)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    best_loss = math.inf
    best_weights = None
    train_loader = DataLoader(
        datasets["train"], batch_size=_BATCH_SIZE, shuffle=True, generator=torch.Generator().manual_seed(42)
    )
    for epoch in range(1, epochs + 1):
        model.train()
        total_train_loss = 0.0
        for history, targets in train_loader:
            optimizer.zero_grad()
            loss = nn.functional.mse_loss(model(history), targets)
            if not torch.isfinite(loss):
                raise ValueError("Training loss became nonfinite; check observation magnitudes.")
            loss.backward()
            optimizer.step()
            total_train_loss += float(loss.detach()) * len(history)
        model.eval()
        validation_loss = 0.0
        with torch.no_grad():
            for history, targets in DataLoader(datasets["validation"], batch_size=_BATCH_SIZE):
                batch_loss = nn.functional.mse_loss(model(history), targets)
                if not torch.isfinite(batch_loss):
                    raise ValueError("Validation loss became nonfinite; check observation magnitudes.")
                validation_loss += float(batch_loss) * len(history)
        validation_loss /= len(datasets["validation"])
        if validation_loss < best_loss:
            best_loss = validation_loss
            best_weights = copy.deepcopy(model.state_dict())
        if progress is not None:
            progress(
                f"Epoch {epoch}/{epochs}: train MSE={total_train_loss / len(datasets['train']):.6g}, "
                f"validation MSE={validation_loss:.6g} (normalized)"
            )
    assert best_weights is not None
    model.load_state_dict(best_weights)
    summary = {
        "schema_version": 1,
        "model": {"type": "LSTM", "hidden_size": _HIDDEN_SIZE, "input_size": len(FEATURES)},
        "features": list(FEATURES),
        "lookback": lookback,
        "horizon": horizon,
        "epochs": epochs,
        "cadence": str(pd.Timedelta(cadence_ns, unit="ns")),
        "cadence_ns": cadence_ns,
        "sites": site_details,
        "sample_counts": {partition: len(dataset) for partition, dataset in datasets.items()},
        "normalization": {"mean": mean.tolist(), "std": std.tolist()},
        "metrics": {
            partition: _calculate_metrics(model, datasets[partition], std, horizon)
            for partition in ("validation", "test")
        },
    }
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    # Avoid publishing incomplete individual files if serialization is interrupted.
    weights_path = destination / "model.pt"
    metadata_path = destination / "metadata.json"
    with tempfile.NamedTemporaryFile(dir=destination, suffix=".pt", delete=False) as temp:
        temp_weights = Path(temp.name)
    temp_metadata: Path | None = None
    try:
        torch.save(model.state_dict(), temp_weights)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination, suffix=".json", delete=False) as temp:
            temp_metadata = Path(temp.name)
            json.dump(summary, temp, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp_weights, weights_path)
        os.replace(temp_metadata, metadata_path)
    finally:
        temp_weights.unlink(missing_ok=True)
        if temp_metadata is not None:
            temp_metadata.unlink(missing_ok=True)
    return summary


def load_training_summary(model_dir: str | Path) -> dict:
    """Load the human-readable model metadata without deserializing Python objects."""
    path = Path(model_dir) / "metadata.json"
    try:
        with path.open("r", encoding="utf-8") as stream:
            summary = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read training metadata from {path}: {exc}") from exc
    if not isinstance(summary, dict) or summary.get("schema_version") != 1:
        raise ValueError(f"Unsupported training metadata format in {path}.")
    return summary


def _load_saved_model(metadata: dict, model_dir: str | Path) -> _ForecastLSTM:
    model = _ForecastLSTM(metadata["horizon"], hidden_size=metadata["model"]["hidden_size"])
    try:
        weights = torch.load(Path(model_dir) / "model.pt", map_location="cpu", weights_only=True)
        model.load_state_dict(weights, strict=True)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise ValueError(f"Cannot load trained model from {model_dir}: {exc}") from exc
    model.eval()
    return model


def forecast(frame: pd.DataFrame, model_dir: str | Path, site: str, depth: float) -> pd.DataFrame:
    """Forecast the trained horizon from the latest real history of a known series."""
    metadata = load_training_summary(model_dir)
    if metadata.get("features") != list(FEATURES):
        raise ValueError("The trained model's feature schema is incompatible with this pipeline.")
    data, cadence_ns = _prepare_frame(frame)
    if cadence_ns != metadata.get("cadence_ns"):
        raise ValueError(f"Observation cadence {pd.Timedelta(cadence_ns, unit='ns')} does not match trained cadence {metadata['cadence']}.")
    try:
        requested_depth = float(depth)
    except (ValueError, TypeError) as exc:
        raise ValueError("Depth must be a finite numeric value.") from exc
    if not math.isfinite(requested_depth):
        raise ValueError("Depth must be a finite numeric value.")
    if not any(item["site"] == site and item["depth_m"] == requested_depth for item in metadata["sites"]):
        raise ValueError(f"Site {site!r} at depth {requested_depth:g} m was not present during training.")
    series = data.loc[(data["site"] == site) & (data["depth_m"] == requested_depth)]
    if series.empty:
        raise ValueError(f"No imported observations for site {site!r}, depth {requested_depth:g} m.")
    lookback = metadata["lookback"]
    horizon = metadata["horizon"]
    if len(series) < lookback:
        raise ValueError(
            f"Site {site!r}, depth {requested_depth:g} m has {len(series)} observations; "
            f"at least {lookback} consecutive observations are required."
        )
    mean = np.asarray(metadata["normalization"]["mean"], dtype=np.float64)
    std = np.asarray(metadata["normalization"]["std"], dtype=np.float64)
    history = ((series.loc[:, list(FEATURES)].tail(lookback).to_numpy(dtype=np.float64) - mean) / std).astype(np.float32)
    if not np.isfinite(history).all():
        raise ValueError("Imported observations cannot be normalized with the saved training statistics.")
    model = _load_saved_model(metadata, model_dir)
    with torch.no_grad():
        prediction = model(torch.from_numpy(history[None, :, :])).squeeze(0).numpy().astype(np.float64)
    prediction = prediction * std + mean
    if not np.isfinite(prediction).all():
        raise ValueError("The trained model produced nonfinite forecasts.")
    last_timestamp = series["timestamp"].iloc[-1]
    timestamps = [last_timestamp + pd.Timedelta(cadence_ns * step, unit="ns") for step in range(1, horizon + 1)]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "site": site,
            "depth_m": requested_depth,
            FEATURES[0]: prediction[:, 0],
            FEATURES[1]: prediction[:, 1],
        },
        columns=list(COLUMNS),
    )


def analyze_history(frame: pd.DataFrame, model_dir: str | Path, site: str, depth: float) -> dict:
    """Replay saved-model final-horizon forecasts against actual historic targets.

    Imported history may overlap the original training set. These replay metrics
    are descriptive, never an independent out-of-sample validation claim.
    """
    metadata = load_training_summary(model_dir)
    if metadata.get("features") != list(FEATURES):
        raise ValueError("The trained model's feature schema is incompatible with this pipeline.")
    data, cadence_ns = _prepare_frame(frame)
    if cadence_ns != metadata.get("cadence_ns"):
        raise ValueError(
            f"Observation cadence {pd.Timedelta(cadence_ns, unit='ns')} "
            f"does not match trained cadence {metadata['cadence']}."
        )
    try:
        requested_depth = float(depth)
    except (TypeError, ValueError) as exc:
        raise ValueError("Depth must be a finite numeric value.") from exc
    if not math.isfinite(requested_depth):
        raise ValueError("Depth must be a finite numeric value.")
    if not any(item["site"] == site and item["depth_m"] == requested_depth for item in metadata["sites"]):
        raise ValueError(f"Site {site!r} at depth {requested_depth:g} m was not present during training.")
    series = data.loc[(data["site"] == site) & (data["depth_m"] == requested_depth)]
    lookback, horizon = metadata["lookback"], metadata["horizon"]
    if len(series) < lookback + horizon:
        raise ValueError(
            f"Site {site!r}, depth {requested_depth:g} m needs at least "
            f"{lookback + horizon} consecutive observations for a {horizon}-step replay."
        )
    mean = np.asarray(metadata["normalization"]["mean"], dtype=np.float64)
    std = np.asarray(metadata["normalization"]["std"], dtype=np.float64)
    values = series.loc[:, list(FEATURES)].to_numpy(dtype=np.float64)
    normalized = ((values - mean) / std).astype(np.float32)
    if not np.isfinite(normalized).all():
        raise ValueError("Imported observations cannot be normalized with the saved training statistics.")

    model = _load_saved_model(metadata, model_dir)
    origins = np.arange(lookback, len(series) - horizon + 1)
    predicted = np.empty((len(origins), len(FEATURES)), dtype=np.float64)
    with torch.no_grad():
        for start in range(0, len(origins), _BATCH_SIZE):
            batch = origins[start : start + _BATCH_SIZE]
            history = np.stack([normalized[index - lookback : index] for index in batch])
            output = model(torch.from_numpy(history))[:, -1, :].numpy()
            predicted[start : start + len(batch)] = output * std + mean
    if not np.isfinite(predicted).all():
        raise ValueError("The trained model produced nonfinite historical replay values.")
    target_positions = origins + horizon - 1
    actual = values[target_positions]
    naive = values[origins - 1]
    mae = np.mean(np.abs(predicted - actual), axis=0)
    naive_mae = np.mean(np.abs(naive - actual), axis=0)
    timestamps = series["timestamp"].iloc[target_positions]
    rows = [
        {
            "timestamp": pd.Timestamp(timestamp).isoformat(),
            "actual_temperature_c": float(truth[0]),
            "predicted_temperature_c": float(estimate[0]),
            "actual_dissolved_oxygen_mg_l": float(truth[1]),
            "predicted_dissolved_oxygen_mg_l": float(estimate[1]),
        }
        for timestamp, truth, estimate in zip(timestamps, actual, predicted)
    ]
    return {
        "rows": rows,
        "metrics": {
            "mae": {feature: float(error) for feature, error in zip(FEATURES, mae)},
            "naive_mae": {feature: float(error) for feature, error in zip(FEATURES, naive_mae)},
        },
        "sample_count": len(rows),
        "horizon": horizon,
        "cadence": metadata["cadence"],
        "disclaimer": "导入历史的模型回放；若与训练数据重叠，此误差并非独立验证。",
    }
