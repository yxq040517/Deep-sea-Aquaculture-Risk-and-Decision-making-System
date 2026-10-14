"""Extract a real NOAA NERRS temperature/oxygen testing slice, without imputation.

The packaged raw CDMO CSV is distributed by the SWMPr project at SOURCE_URL.
Output depth_m is a *nominal station grouping depth*, not each tidal depth.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd


SOURCE_URL = "https://s3.amazonaws.com/swmpexdata/zip_ex.zip"
SOURCE_MEMBER = "zip_ex/apadbwq2013.csv"
STATION_METADATA = "https://cdmo.baruch.sc.edu/waf/YearlyFiles/Apalachicola/water%20quality/metadata/apawq01-12.13m.pdf"
ROOT = Path(__file__).resolve().parent.parent
NOMINAL_DEPTH_M = 1.7  # Metadata: 2 m site water depth, probe 0.3 m above sediment.
INTERVAL = pd.Timedelta(minutes=15)
OUTPUT_NAMES = ("nerrs_dry_bar_2013_train.csv", "nerrs_dry_bar_2013_holdout.csv", "nerrs_dry_bar_2013_source.json")


def prepare(archive: bytes, output: Path) -> tuple[int, int]:
    """Retain only measured, flagged-pass values in one uninterrupted run."""
    if any((output / name).exists() for name in OUTPUT_NAMES):
        raise FileExistsError("Output files already exist; remove them explicitly before regenerating.")
    with zipfile.ZipFile(io.BytesIO(archive)) as container:
        with container.open(SOURCE_MEMBER) as stream:
            source = pd.read_csv(
                stream,
                usecols=["StationCode", "DateTimeStamp", "Temp", "F_Temp", "DO_mgl", "F_DO_mgl"],
                skipinitialspace=True,
            )
    source["timestamp_local"] = pd.to_datetime(source["DateTimeStamp"], format="%m/%d/%Y %H:%M", errors="coerce")
    for column in ("Temp", "DO_mgl"):
        source[column] = pd.to_numeric(source[column], errors="coerce")
    valid = (
        source["StationCode"].astype("string").str.strip().eq("apadbwq")
        & source["F_Temp"].astype("string").str.strip().eq("<0>")
        & source["F_DO_mgl"].astype("string").str.strip().eq("<0>")
        & source["timestamp_local"].notna()
        & source["Temp"].notna()
        & source["DO_mgl"].notna()
        & source["Temp"].between(-2, 40)
        & source["DO_mgl"].between(0, 30)
    ).fillna(False)
    clean = source.loc[valid].sort_values("timestamp_local").drop_duplicates("timestamp_local")
    segment = clean["timestamp_local"].diff().ne(INTERVAL).cumsum()
    spans = (part for _, part in clean.groupby(segment, sort=False))
    longest = max(spans, key=len)
    if len(longest) < 640:
        raise ValueError(f"No QA-flagged uninterrupted run long enough for independent tests: {len(longest)} rows.")
    selected = longest.iloc[:1200].copy()
    split = max(480, int(len(selected) * 0.75))
    if len(selected) - split < 160:
        raise ValueError("Holdout contains too few actual observations.")
    # SWMP timestamps are fixed local standard time without daylight savings;
    # Apalachicola uses a GMT-5 clock (e.g., SWMPr's America/Jamaica example).
    timestamps_utc = selected["timestamp_local"] + pd.Timedelta(hours=5)
    prepared = pd.DataFrame({
        "timestamp": timestamps_utc.dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": "apadbwq",
        "depth_m": NOMINAL_DEPTH_M,
        "temperature_c": selected["Temp"].to_numpy(),
        "dissolved_oxygen_mg_l": selected["DO_mgl"].to_numpy(),
        "F_Temp": selected["F_Temp"].astype("string").str.strip().to_numpy(),
        "F_DO_mgl": selected["F_DO_mgl"].astype("string").str.strip().to_numpy(),
    })
    output.mkdir(parents=True, exist_ok=True)
    prepared.iloc[:split].to_csv(output / OUTPUT_NAMES[0], index=False, encoding="utf-8-sig")
    prepared.iloc[split:].to_csv(output / OUTPUT_NAMES[1], index=False, encoding="utf-8-sig")
    provenance = {
        "source": "NOAA NERRS SWMP, Apalachicola Bay Dry Bar (apadbwq), 2013 water quality observations",
        "source_url": SOURCE_URL,
        "source_member": SOURCE_MEMBER,
        "source_sha256": hashlib.sha256(archive).hexdigest(),
        "official_station_metadata": STATION_METADATA,
        "parameters_reference": "https://cdmo.baruch.sc.edu/data/parameters.cfm",
        "time_reference": "https://cran.r-project.org/web/packages/SWMPr/refman/SWMPr.html",
        "source_flags_retained": {"F_Temp": "<0>", "F_DO_mgl": "<0>"},
        "qc_columns_in_export": ["F_Temp", "F_DO_mgl"],
        "numeric_screen": "Finite Temp in [-2, 40] °C and DO_mgl in [0, 30] mg/L; an additional plausibility screen, not a species safety threshold.",
        "sampling_interval": "15 minutes; no interpolation or filled gaps",
        "timestamp_processing": "Source fixed local standard time GMT-5 shifted +5 hours to explicit UTC; SWMPr notes no daylight savings in SWMP data.",
        "nominal_depth_m": NOMINAL_DEPTH_M,
        "nominal_depth_basis": "Station metadata reports approximately 2 m site depth and probe 0.3 m above bottom. This is a nominal below-surface grouping label; actual depth varies with tide. No per-observation depth is invented.",
        "station_notes": "Estuarine oyster-bar monitoring station, not a salmon cage and not Bohai Bay. Original 2013 sample is marked provisional-plus in source; QA flag 0 is initial-pass, not proof of final authoritative review.",
        "selection": "Longest uninterrupted 15-minute run with both Temp and DO_mgl flagged 0; first up to 1200 measured rows, chronological 75% training file and 25% independent holdout file.",
        "first_utc": prepared["timestamp"].iloc[0],
        "last_utc": prepared["timestamp"].iloc[-1],
        "rows_train": split,
        "rows_holdout": len(prepared) - split,
        "citation": "NOAA National Estuarine Research Reserve System (NERRS), System-wide Monitoring Program; Apalachicola NERR Dry Bar; distributed with the CC0-licensed SWMPr example archive. Acknowledge the reserve if used in a publication.",
    }
    with (output / OUTPUT_NAMES[2]).open("w", encoding="utf-8") as stream:
        json.dump(provenance, stream, ensure_ascii=False, indent=2)
    return split, len(prepared) - split


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare two real station observation files for local platform testing")
    parser.add_argument("--archive", type=Path, help="Previously downloaded zip_ex.zip (otherwise download from SOURCE_URL)")
    parser.add_argument("--output", type=Path, default=ROOT / "sample-data")
    arguments = parser.parse_args()
    content = arguments.archive.read_bytes() if arguments.archive else urllib.request.urlopen(SOURCE_URL, timeout=60).read()
    training, holdout = prepare(content, arguments.output)
    print(f"Prepared {training} training and {holdout} later held-out measurements in {arguments.output}")
