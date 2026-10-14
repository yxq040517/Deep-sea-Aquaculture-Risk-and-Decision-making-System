"""Exercise local HTTP import, training, historical analysis, prediction, and export."""

from __future__ import annotations

import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import server

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient
from openpyxl import load_workbook


class PlatformApiTests(unittest.TestCase):
    @staticmethod
    def observations(count: int = 112) -> pd.DataFrame:
        index = np.arange(count)
        return pd.DataFrame({
            "timestamp": pd.date_range("2025-05-01", periods=count, freq="h"),
            "site": ["sensor-one"] * count,
            "depth_m": [12.0] * count,
            "temperature_c": 12.0 + 0.02 * index + 0.15 * np.sin(index / 6),
            "dissolved_oxygen_mg_l": 8.7 - 0.006 * index + 0.08 * np.cos(index / 5),
        })

    def test_import_train_replay_predict_export_and_reload_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, "DATASETS", root / "datasets"), patch.object(server, "MODELS", root / "models"):
                with TestClient(server.app) as client:
                    self.assertEqual(client.get("/api/datasets").json()["datasets"], [])
                    self.assertEqual(client.get("/api/models").json()["models"], [])
                    source = self.observations()
                    imported = client.post("/api/datasets", files={
                        "file": ("history.csv", source.to_csv(index=False).encode(), "text/csv")
                    })
                    self.assertEqual(imported.status_code, 200, imported.text)
                    dataset = imported.json()
                    self.assertEqual(dataset["rows"], len(source))
                    self.assertEqual(dataset["series"][0]["site"], "sensor-one")
                    training = client.post("/api/train", json={
                        "dataset_id": dataset["id"], "lookback": 12, "horizon": 4, "epochs": 2,
                    })
                    self.assertEqual(training.status_code, 200, training.text)
                    job_id = training.json()["job_id"]
                    deadline = time.monotonic() + 50
                    while time.monotonic() < deadline:
                        job = client.get(f"/api/train/{job_id}").json()
                        if job["status"] in ("completed", "failed"):
                            break
                        time.sleep(0.1)
                    self.assertEqual(job["status"], "completed", job)
                    model_id = job["model_id"]
                    self.assertTrue((root / "models" / model_id / "model.pt").is_file())
                    self.assertTrue((root / "models" / model_id / "metadata.json").is_file())
                    self.assertTrue(client.get("/api/models").json()["models"])

                    selection = {"dataset_id": dataset["id"], "model_id": model_id,
                                 "site": "sensor-one", "depth_m": 12.0}
                    analysis = client.post("/api/analyze", json=selection)
                    self.assertEqual(analysis.status_code, 200, analysis.text)
                    replay = analysis.json()
                    self.assertEqual(replay["sample_count"], len(source) - 12 - 4 + 1)
                    self.assertIn("非独立验证", replay["disclaimer"])
                    self.assertAlmostEqual(replay["rows"][0]["actual_temperature_c"],
                                           source["temperature_c"].iloc[15])

                    recent = source.tail(20)
                    extension = []
                    for offset in range(1, 5):
                        row = source.iloc[-1].copy()
                        row["timestamp"] += pd.Timedelta(hours=offset)
                        row["temperature_c"] += offset * 0.02
                        row["dissolved_oxygen_mg_l"] -= offset * 0.03
                        extension.append(row)
                    new_data = pd.concat([recent, pd.DataFrame(extension)], ignore_index=True)
                    future_upload = client.post("/api/datasets", files={
                        "file": ("new_observations.xlsx", self._excel_bytes(new_data),
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    })
                    self.assertEqual(future_upload.status_code, 200, future_upload.text)
                    selection["dataset_id"] = future_upload.json()["id"]
                    predicted = client.post("/api/forecast", json=selection)
                    self.assertEqual(predicted.status_code, 200, predicted.text)
                    result = predicted.json()
                    self.assertEqual(len(result["history"]), 12)
                    self.assertEqual(len(result["forecast"]), 4)
                    self.assertGreater(result["forecast"][0]["timestamp"], result["history"][-1]["timestamp"])
                    self.assertIn("非实测", result["disclaimer"])
                    for extension_name in ("csv", "xlsx"):
                        download = client.get(f"/api/forecast/export.{extension_name}", params=selection)
                        self.assertEqual(download.status_code, 200, download.text)
                        self.assertTrue(download.content)
                        if extension_name == "xlsx":
                            workbook = load_workbook(io.BytesIO(download.content), read_only=True)
                            self.assertEqual(workbook["短时预测"].max_row, 5)
                            workbook.close()
                    invalid = client.post("/api/forecast", json={**selection, "model_id": "../etc/passwd"})
                    self.assertEqual(invalid.status_code, 400)

                # A new browser/session reads the same persisted dataset and model records.
                with TestClient(server.app) as reopened:
                    self.assertEqual(len(reopened.get("/api/datasets").json()["datasets"]), 2)
                    self.assertEqual(reopened.get("/api/models").json()["models"][0]["id"], model_id)

    @staticmethod
    def _excel_bytes(frame: pd.DataFrame) -> bytes:
        stream = io.BytesIO()
        frame.to_excel(stream, index=False)
        return stream.getvalue()


if __name__ == "__main__":
    unittest.main()
