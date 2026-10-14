"""Local HTTP API for importing observations, training models, and forecasting."""

from __future__ import annotations

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

# Import the CPU pipeline before pandas on Windows (native-library initialization order).
from src import pipeline

import anyio
import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel, Field, StrictInt
from starlette.background import BackgroundTask

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


class SeriesRequest(BaseModel):
    dataset_id: str
    model_id: str
    site: str
    depth_m: float


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
async def upload_dataset(file: UploadFile = File(...)) -> dict:
    name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        raise HTTPException(status_code=400, detail="Upload a CSV or XLSX observation file.")
    dataset_id = uuid.uuid4().hex
    DATASETS.mkdir(parents=True, exist_ok=True)
    temporary = DATASETS / f".{dataset_id}{suffix}"
    destination = DATASETS / f"{dataset_id}{suffix}"
    metadata_path = DATASETS / f"{dataset_id}.json"
    try:
        total = 0
        with temporary.open("xb") as output:
            while chunk := await file.read(_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=400, detail="Observation files must not exceed 50 MiB.")
                output.write(chunk)
        try:
            frame = await anyio.to_thread.run_sync(pipeline.read_observations, temporary)
        except (ValueError, BadZipFile, KeyError, ParseError, InvalidFileException) as exc:
            raise _public_error(exc, temporary) from exc
        summary = _summary(frame, dataset_id, name)
        metadata = {**summary, "suffix": suffix, "created_at": datetime.now(timezone.utc).isoformat()}
        os.replace(temporary, destination)
        # Publish metadata last: listings never include partially uploaded datasets.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATASETS, suffix=".json", delete=False) as stream:
            metadata_temp = Path(stream.name)
            json.dump(metadata, stream, ensure_ascii=False, allow_nan=False)
        try:
            os.replace(metadata_temp, metadata_path)
        finally:
            metadata_temp.unlink(missing_ok=True)
        return summary
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
            entries.append({**{key: metadata[key] for key in ("id", "name", "rows", "series", "start", "end", "cadence")}, "created_at": metadata["created_at"]})
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            continue
    entries.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"datasets": [{key: entry[key] for key in ("id", "name", "rows", "series", "start", "end", "cadence")} for entry in entries]}


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
        entries.append({"id": path.name, "summary": summary, "created_at": created_at})
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
            epochs=request.epochs, progress=progress,
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
    frame = await anyio.to_thread.run_sync(_read_dataset, request.dataset_id)
    path = _model_path(request.model_id)
    _requested_series(frame, request.site, request.depth_m)
    try:
        return await anyio.to_thread.run_sync(pipeline.analyze_history, frame, path, request.site, request.depth_m)
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
    selected, predicted, summary = await anyio.to_thread.run_sync(
        _forecast_data, request.dataset_id, request.model_id, request.site, request.depth_m
    )
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
        "disclaimer": "模型预测并非实测；仅供短时水温与溶氧趋势参考，不构成养殖适宜性或安全保证。",
    }


def _export(dataset_id: str, model_id: str, site: str, depth_m: float, extension: str) -> FileResponse:
    _, predicted, summary = _forecast_data(dataset_id, model_id, site, depth_m)
    with tempfile.NamedTemporaryFile(suffix=f".{extension}", delete=False) as stream:
        destination = Path(stream.name)
    try:
        if extension == "csv":
            export_csv(destination, predicted)
        else:
            export_excel(destination, predicted, summary)
    except (ValueError, OSError):
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
async def download_xlsx(dataset_id: str, model_id: str, site: str, depth_m: float = Query(...)) -> FileResponse:
    if not math.isfinite(depth_m):
        raise HTTPException(status_code=400, detail="Depth must be finite.")
    try:
        return await anyio.to_thread.run_sync(_export, dataset_id, model_id, site, depth_m, "xlsx")
    except ValueError as exc:
        raise _public_error(exc) from exc
