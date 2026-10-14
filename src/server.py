"""Local HTTP API for importing observations, training models, and forecasting."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import uuid
from zipfile import BadZipFile
from datetime import datetime, timezone
from xml.etree.ElementTree import ParseError
from pathlib import Path
from typing import Literal

# Import the CPU pipeline before pandas on Windows (native-library initialization order).
from src import pipeline

import anyio
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel, Field, StrictInt
from starlette.background import BackgroundTask

from src.events import EventRule, forecast_lead, score_replay
from src.quality import assess_quality, read_raw, reject_percent_oxygen, unknown_quality
from src.reporting import export_csv, export_excel


BASE = Path(__file__).resolve().parent.parent
DATASETS = BASE / "workspace" / "datasets"
MODELS = BASE / "workspace" / "models"
WEB = BASE / "web"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_UUID_ID = re.compile(r"[0-9a-f]{32}\Z")

app = FastAPI(title="水质时序预测")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, exc: RequestValidationError) -> JSONResponse:
    issues = exc.errors()
    message = "; ".join(f"{'.'.join(map(str, issue['loc']))}: {issue['msg']}" for issue in issues)
    return JSONResponse(status_code=400, content={"detail": message or "Invalid request."})


class TrainRequest(BaseModel):
    dataset_id: str
    lookback: StrictInt = Field(gt=0, le=1000)
    horizon: StrictInt = Field(gt=0, le=1000)
    epochs: StrictInt = Field(gt=0, le=1000)
    split_mode: Literal["chronological", "year_holdout"] = "chronological"


class SeriesRequest(BaseModel):
    dataset_id: str
    model_id: str
    site: str
    depth_m: float
    rule: dict | None = None


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _UUID_ID.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid identifier.")
    return value


def _dataset_path(dataset_id: str) -> Path:
    dataset_id = _identifier(dataset_id)
    metadata_path = DATASETS / f"{dataset_id}.json"
    if not metadata_path.is_file():
        raise HTTPException(status_code=404, detail="Dataset not found.")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        suffix = metadata["suffix"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=404, detail="Dataset not found.") from None
    if suffix not in (".csv", ".xlsx"):
        raise HTTPException(status_code=404, detail="Dataset not found.")
    path = DATASETS / f"{dataset_id}{suffix}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return path


def _model_path(model_id: str) -> Path:
    model_id = _identifier(model_id)
    path = MODELS / model_id
    if not path.is_dir() or not (path / "model.pt").is_file() or not (path / "metadata.json").is_file():
        raise HTTPException(status_code=404, detail="Model not found.")
    try:
        pipeline.load_training_summary(path)
    except ValueError:
        raise HTTPException(status_code=404, detail="Model not found.") from None
    return path


def _summary(frame: pd.DataFrame, dataset_id: str, name: str) -> dict:
    groups = frame.groupby(["site", "depth_m"], sort=True)
    first = next(iter(groups))[1]
    return {
        "id": dataset_id,
        "name": name,
        "rows": len(frame),
        "series": [
            {"site": str(site), "depth_m": float(depth), "observations": len(group)}
            for (site, depth), group in groups
        ],
        "start": frame["timestamp"].min().isoformat(),
        "end": frame["timestamp"].max().isoformat(),
        "cadence": str(first["timestamp"].iloc[1] - first["timestamp"].iloc[0]),
    }

_PROVENANCE_FIELDS = ("source_name", "source_reference", "timezone_note", "depth_basis", "qc_note", "temperature_unit", "oxygen_unit")


def _provenance(text: str | None) -> dict:
    try:
        supplied = json.loads(text) if text else {}
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid provenance JSON: {exc}") from exc
    if not isinstance(supplied, dict):
        raise HTTPException(status_code=400, detail="Provenance must be a JSON object.")
    result = {field: "未提供" for field in _PROVENANCE_FIELDS}
    for field in _PROVENANCE_FIELDS:
        if field in supplied and supplied[field] is not None:
            if not isinstance(supplied[field], str):
                raise HTTPException(status_code=400, detail=f"Provenance {field} must be text.")
            result[field] = supplied[field].strip() or "未提供"
    if result["depth_basis"] not in ("measured", "nominal", "unknown", "未提供"):
        raise HTTPException(status_code=400, detail="depth_basis must be measured, nominal or unknown.")
    if result["temperature_unit"] not in ("°C", "未提供"):
        raise HTTPException(status_code=400, detail="temperature_c requires °C; no automatic conversion is available.")
    if result["oxygen_unit"] not in ("mg/L", "未提供"):
        raise HTTPException(status_code=400, detail="dissolved_oxygen_mg_l requires mg/L; percent saturation cannot be converted automatically.")
    return result


def _dataset_metadata(dataset_id: str) -> dict:
    _dataset_path(dataset_id)
    return json.loads((DATASETS / f"{dataset_id}.json").read_text(encoding="utf-8"))


def _public_dataset(metadata: dict) -> dict:
    return {
        **{key: metadata[key] for key in ("id", "name", "rows", "series", "start", "end", "cadence")},
        "provenance": metadata.get("provenance") or _provenance(None),
        "quality": metadata.get("quality") or unknown_quality(),
        "sha256": metadata.get("sha256"),
    }


def _event_rule(value: dict | None) -> EventRule | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Rule must be an object with sourced thresholds.")
    for field in ("max_temp_c", "min_do_mg_l", "action_minutes"):
        candidate = value.get(field)
        if candidate is not None and (isinstance(candidate, bool) or not isinstance(candidate, (int, float))):
            raise HTTPException(status_code=400, detail=f"{field} must be a finite number.")
    try:
        return EventRule(**value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid rule: {exc}") from exc


def _validation_context(metadata: dict, dataset: dict, selected: pd.DataFrame, site: str, depth: float) -> dict:
    source_hash = metadata.get("training_source_sha256")
    imported_hash = dataset.get("sha256")
    if source_hash and imported_hash and source_hash == imported_hash:
        return {"status": "overlap", "message": "回放文件与训练源文件 SHA-256 一致，不能当作独立验证。"}
    trained = next((item for item in metadata.get("sites", []) if item.get("site") == site and item.get("depth_m") == depth), None)
    if trained:
        start, end = trained.get("source_start"), trained.get("source_end")
        try:
            imported_start, imported_end = pd.Timestamp(selected["timestamp"].min()), pd.Timestamp(selected["timestamp"].max())
            if end and imported_start > pd.Timestamp(end):
                return {"status": "independent_time", "message": "导入观测晚于该站点/深度的训练源观测；仅表示时间不重叠，不代表独立场址。"}
            if start and imported_end < pd.Timestamp(start):
                return {"status": "independent_time", "message": "导入观测早于该站点/深度的训练源观测；仅表示时间不重叠，不代表独立场址。"}
            if start and end and imported_start <= pd.Timestamp(end) and imported_end >= pd.Timestamp(start):
                return {"status": "overlap", "message": "回放观测时间与训练源观测区间重叠，不能当作独立验证。"}
        except (TypeError, ValueError):
            pass
    return {"status": "unknown", "message": "训练源时间范围或源文件指纹不足，无法证明回放与训练数据独立。"}


def _public_error(exc: Exception, *paths: Path) -> HTTPException:
    message = str(exc)
    for path in paths:
        message = message.replace(str(path), "selected file")
    return HTTPException(status_code=400, detail=message)


def _read_dataset(dataset_id: str) -> pd.DataFrame:
    path = _dataset_path(dataset_id)
    try:
        return pipeline.read_observations(path)
    except ValueError as exc:
        raise _public_error(exc, path) from exc


def _requested_series(frame: pd.DataFrame, site: str, depth: float) -> pd.DataFrame:
    if not isinstance(site, str) or not site.strip():
        raise HTTPException(status_code=400, detail="Select a site.")
    if not math.isfinite(depth):
        raise HTTPException(status_code=400, detail="Depth must be finite.")
    selected = frame.loc[(frame["site"] == site) & (frame["depth_m"] == depth)]
    if selected.empty:
        raise HTTPException(status_code=400, detail="The selected site and depth are not in this dataset.")
    return selected


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/datasets")
async def upload_dataset(file: UploadFile = File(...), provenance: str | None = Form(default=None)) -> dict:
    name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        raise HTTPException(status_code=400, detail="Upload a CSV or XLSX observation file.")
    evidence = _provenance(provenance)
    dataset_id = uuid.uuid4().hex
    DATASETS.mkdir(parents=True, exist_ok=True)
    temporary = DATASETS / f".{dataset_id}{suffix}"
    destination = DATASETS / f"{dataset_id}{suffix}"
    metadata_path = DATASETS / f"{dataset_id}.json"
    try:
        total = 0
        digest = hashlib.sha256()
        with temporary.open("xb") as output:
            while chunk := await file.read(_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=400, detail="Observation files must not exceed 50 MiB.")
                output.write(chunk)
                digest.update(chunk)
        quality = None
        try:
            raw = await anyio.to_thread.run_sync(read_raw, temporary)
            quality = await anyio.to_thread.run_sync(assess_quality, raw, evidence["depth_basis"])
            if evidence["source_name"] == "未提供" or evidence["source_reference"] == "未提供":
                quality["warnings"].append("数据来源或出处未提供；文件格式校验不等同于实测来源核验。")
            if evidence["temperature_unit"] == "未提供" or evidence["oxygen_unit"] == "未提供":
                quality["warnings"].append("原始物理单位未提供；列名约定为 °C 和 mg/L，不证明原始数值使用这些单位。")
            if evidence["timezone_note"] != "未提供":
                quality["warnings"].append("来源时区备注仅作来源说明，不改变自动 UTC 时间解释；如不一致须在源文件中显式修正后重传。")
            reject_percent_oxygen(raw)
            frame = await anyio.to_thread.run_sync(pipeline.read_observations, temporary)
        except (ValueError, TypeError, OverflowError, UnicodeError, OSError, ImportError,
                BadZipFile, KeyError, ParseError, InvalidFileException, pd.errors.ParserError) as exc:
            message = str(exc).replace(str(temporary), "selected file")
            raise HTTPException(status_code=400, detail={"message": message, "quality": quality}) from exc
        summary = _summary(frame, dataset_id, name)
        metadata = {**summary, "suffix": suffix, "created_at": datetime.now(timezone.utc).isoformat(),
                    "provenance": evidence, "quality": quality, "sha256": digest.hexdigest()}
        os.replace(temporary, destination)
        # Publish metadata last: listings never include partially uploaded datasets.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATASETS, suffix=".json", delete=False) as stream:
            metadata_temp = Path(stream.name)
            json.dump(metadata, stream, ensure_ascii=False, allow_nan=False)
        try:
            os.replace(metadata_temp, metadata_path)
        finally:
            metadata_temp.unlink(missing_ok=True)
        return _public_dataset(metadata)
    finally:
        temporary.unlink(missing_ok=True)
        await file.close()


@app.get("/api/datasets")
def list_datasets() -> dict:
    if not DATASETS.is_dir():
        return {"datasets": []}
    entries = []
    for path in DATASETS.glob("*.json"):
        if not _UUID_ID.fullmatch(path.stem):
            continue
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if metadata.get("id") != path.stem or metadata.get("suffix") not in (".csv", ".xlsx"):
                continue
            if not (DATASETS / f"{path.stem}{metadata['suffix']}").is_file():
                continue
            entries.append({**_public_dataset(metadata), "created_at": metadata["created_at"]})
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            continue
    entries.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"datasets": [{key: value for key, value in entry.items() if key != "created_at"} for entry in entries]}


@app.get("/api/models")
def list_models() -> dict:
    if not MODELS.is_dir():
        return {"models": []}
    entries = []
    for path in MODELS.iterdir():
        if not path.is_dir() or not _UUID_ID.fullmatch(path.name):
            continue
        weights, metadata = path / "model.pt", path / "metadata.json"
        if not weights.is_file() or not metadata.is_file():
            continue
        try:
            summary = pipeline.load_training_summary(path)
            created_at = datetime.fromtimestamp(metadata.stat().st_mtime, timezone.utc).isoformat()
        except (ValueError, OSError):
            continue
        entries.append({"id": path.name, "summary": summary, "split_mode": summary.get("split_mode", "unknown"), "created_at": created_at})
    entries.sort(key=lambda item: item["created_at"], reverse=True)
    return {"models": entries}


_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_active_job: str | None = None


def _train_job(job_id: str, request: TrainRequest) -> None:
    global _active_job
    model_id = uuid.uuid4().hex
    temporary = MODELS / f".{model_id}.partial"
    destination = MODELS / model_id
    try:
        with _jobs_lock:
            _jobs[job_id].update(status="running", progress="Reading observations")
        frame = _read_dataset(request.dataset_id)

        def progress(message: str) -> None:
            with _jobs_lock:
                _jobs[job_id]["progress"] = message

        summary = pipeline.train_lstm(
            frame, temporary, lookback=request.lookback, horizon=request.horizon,
            epochs=request.epochs, progress=progress, split_mode=request.split_mode,
            source_sha256=_dataset_metadata(request.dataset_id).get("sha256"),
        )
        if not (temporary / "model.pt").is_file() or not (temporary / "metadata.json").is_file():
            raise ValueError("Training finished without complete model artifacts.")
        os.replace(temporary, destination)
        with _jobs_lock:
            _jobs[job_id].update(status="completed", progress="Training completed", model_id=model_id, summary=summary)
    except Exception as exc:
        # A background failure is reported by the job endpoint, never as a successful model.
        message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        with _jobs_lock:
            _jobs[job_id].update(status="failed", progress="Training failed", error=message.replace(str(temporary), "model") or type(exc).__name__)
    finally:
        try:
            if temporary.exists():
                shutil.rmtree(temporary)
        finally:
            with _jobs_lock:
                _active_job = None


@app.post("/api/train")
def start_training(request: TrainRequest) -> dict:
    global _active_job
    _dataset_path(request.dataset_id)
    with _jobs_lock:
        if _active_job is not None:
            raise HTTPException(status_code=409, detail="Another training job is already running.")
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"id": job_id, "status": "queued", "progress": "Waiting to start"}
        _active_job = job_id
    try:
        threading.Thread(target=_train_job, args=(job_id, request), name=f"train-{job_id[:8]}", daemon=True).start()
    except RuntimeError as exc:
        with _jobs_lock:
            _jobs[job_id].update(status="failed", progress="Training failed", error=str(exc))
            _active_job = None
        raise HTTPException(status_code=500, detail="Cannot start training thread.") from exc
    return {"job_id": job_id}


@app.get("/api/train/{job_id}")
def training_status(job_id: str) -> dict:
    _identifier(job_id)
    with _jobs_lock:
        if job_id not in _jobs:
            raise HTTPException(status_code=404, detail="Training job not found.")
        return _jobs[job_id].copy()


@app.post("/api/analyze")
async def analyze(request: SeriesRequest) -> dict:
    _identifier(request.dataset_id)
    _identifier(request.model_id)
    rule = _event_rule(request.rule)
    frame = await anyio.to_thread.run_sync(_read_dataset, request.dataset_id)
    path = _model_path(request.model_id)
    selected = _requested_series(frame, request.site, request.depth_m)
    try:
        result = await anyio.to_thread.run_sync(pipeline.analyze_history, frame, path, request.site, request.depth_m)
        metadata = pipeline.load_training_summary(path)
        result["validation_context"] = _validation_context(metadata, _dataset_metadata(request.dataset_id),
                                                            selected, request.site, request.depth_m)
        result["event_evaluation"] = score_replay(result["rows"], rule, metadata["cadence_ns"]) if rule else None
        return result
    except ValueError as exc:
        raise _public_error(exc, path) from exc


def _forecast_data(dataset_id: str, model_id: str, site: str, depth: float) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    _identifier(dataset_id)
    _identifier(model_id)
    frame = _read_dataset(dataset_id)
    path = _model_path(model_id)
    selected = _requested_series(frame, site, depth)
    try:
        predicted = pipeline.forecast(frame, path, site, depth)
        summary = pipeline.load_training_summary(path)
    except ValueError as exc:
        raise _public_error(exc, path) from exc
    return selected, predicted, summary


@app.post("/api/forecast")
async def predict(request: SeriesRequest) -> dict:
    rule = _event_rule(request.rule)
    selected, predicted, summary = await anyio.to_thread.run_sync(
        _forecast_data, request.dataset_id, request.model_id, request.site, request.depth_m
    )
    lead_time = forecast_lead(predicted, pd.Timestamp(selected["timestamp"].iloc[-1]), rule) if rule else None
    recent = selected.tail(summary["lookback"])
    return {
        "history": [
            {"timestamp": row.timestamp.isoformat(), "temperature_c": row.temperature_c,
             "dissolved_oxygen_mg_l": row.dissolved_oxygen_mg_l}
            for row in recent.itertuples(index=False)
        ],
        "forecast": [
            {"timestamp": row.timestamp.isoformat(), "site": row.site, "depth_m": row.depth_m,
             "temperature_c": row.temperature_c, "dissolved_oxygen_mg_l": row.dissolved_oxygen_mg_l}
            for row in predicted.itertuples(index=False)
        ],
        "horizon": summary["horizon"],
        "cadence": summary["cadence"],
        "disclaimer": "模型预测并非实测；仅供短时水温与溶氧趋势参考，不构成养殖适宜性或安全保证。提前量相对末次观测时刻计算，过期数据不代表现在仍可执行。",
        "lead_time": lead_time,
    }


def _export(dataset_id: str, model_id: str, site: str, depth_m: float, extension: str,
            rule: EventRule | None = None) -> FileResponse:
    selected, predicted, summary = _forecast_data(dataset_id, model_id, site, depth_m)
    with tempfile.NamedTemporaryFile(suffix=f".{extension}", delete=False) as stream:
        destination = Path(stream.name)
    try:
        if extension == "csv":
            export_csv(destination, predicted)
        else:
            details = vars(rule) if rule else None
            issued_at = pd.Timestamp(selected["timestamp"].iloc[-1]) if rule else None
            lead = forecast_lead(predicted, issued_at, rule) if rule else None
            export_excel(destination, predicted, summary, rule=details, lead_time=lead,
                         issued_at=issued_at, dataset=_public_dataset(_dataset_metadata(dataset_id)))
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return FileResponse(destination, filename=f"forecast.{extension}", background=BackgroundTask(destination.unlink, missing_ok=True))


@app.get("/api/forecast/export.csv")
async def download_csv(dataset_id: str, model_id: str, site: str, depth_m: float = Query(...)) -> FileResponse:
    if not math.isfinite(depth_m):
        raise HTTPException(status_code=400, detail="Depth must be finite.")
    try:
        return await anyio.to_thread.run_sync(_export, dataset_id, model_id, site, depth_m, "csv")
    except ValueError as exc:
        raise _public_error(exc) from exc


@app.get("/api/forecast/export.xlsx")
async def download_xlsx(dataset_id: str, model_id: str, site: str, depth_m: float = Query(...),
                        rule: str | None = Query(default=None)) -> FileResponse:
    if not math.isfinite(depth_m):
        raise HTTPException(status_code=400, detail="Depth must be finite.")
    try:
        configured_rule = _event_rule(json.loads(rule)) if rule is not None else None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid rule JSON: {exc}") from exc
    try:
        return await anyio.to_thread.run_sync(_export, dataset_id, model_id, site, depth_m, "xlsx", configured_rule)
    except ValueError as exc:
        raise _public_error(exc) from exc
