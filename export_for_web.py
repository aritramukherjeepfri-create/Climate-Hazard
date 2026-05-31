"""
export_for_web.py  —  Progyan data pipeline
============================================
Converts raw CMIP6 outputs (chi_tiles_*.csv, chi_trend_*.npy) into the
three JSON files that main.py's DataStore expects at startup:

  data/block_summary.json   — per-block CHI + hazard stats across all scenarios
  data/trend_data.json      — 26-year district-level CHI trend
  data/metadata.json        — provenance / run info

Spatial join: each ~1 km pixel is assigned to its nearest named admin block
centroid (South 24 Parganas, West Bengal) using a KD-tree on lat/lon.
Pixels more than 0.4° from any centroid are dropped (ocean / bay tiles).

Usage:
    python export_for_web.py [--data-dir ./data] [--tiles-dir .]
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

SCENARIOS = ["ssp126", "ssp245", "ssp585"]
HAZARD_COLS = [
    "heatwave_prob", "drought_prob", "flood_prob",
    "extreme_precip_prob", "cyclone_prob", "landslide_prob",
]
MAX_DIST_DEG = 0.4   # ~44 km — tiles beyond this are ocean/outside district

# Approximate centroids for all 29 CD Blocks of South 24 Parganas
# Source: Census 2011 / OpenStreetMap admin boundaries
BLOCK_CENTROIDS = {
    "Baruipur":              (22.3605, 88.4356),
    "Budge Budge-I":         (22.4677, 88.1722),
    "Budge Budge-II":        (22.4300, 88.1900),
    "Diamond Harbour-I":     (22.1819, 88.1922),
    "Diamond Harbour-II":    (22.1500, 88.2000),
    "Falta":                 (22.1533, 88.0831),
    "Jaynagar-I":            (22.1756, 88.4264),
    "Jaynagar-II":           (22.1300, 88.4700),
    "Sonarpur":              (22.4320, 88.4130),
    "Thakurpukur Mahestola": (22.4560, 88.2800),
    "Bhangar-I":             (22.5528, 88.5031),
    "Bhangar-II":            (22.5100, 88.5400),
    "Bishnupur-I":           (22.3400, 88.3600),
    "Bishnupur-II":          (22.3000, 88.3800),
    "Kulpi":                 (22.0500, 88.2100),
    "Magra Hat-I":           (22.2900, 88.2300),
    "Magra Hat-II":          (22.2500, 88.2600),
    "Mandirbazar":           (22.1700, 88.3100),
    "Mathurapur I":          (22.1000, 88.3500),
    "Mathurapur-II":         (22.0600, 88.4000),
    "Basanti":               (22.0800, 88.7200),
    "Canning-I":             (22.3100, 88.6700),
    "Canning-II":            (22.2600, 88.7100),
    "Gosaba":                (22.1600, 88.8000),
    "Kak Dwip":              (21.8900, 88.1900),
    "Kultali":               (22.0200, 88.5000),
    "Namkhana":              (21.7600, 88.2100),
    "Pathar Pratima":        (21.8800, 88.3700),
    "Sagar":                 (21.6500, 88.0700),
}

TREND_START_YEAR = 2005


def assign_real_blocks(df: pd.DataFrame) -> pd.DataFrame:
    """Spatially assign each tile to its nearest named admin block."""
    real_names = list(BLOCK_CENTROIDS.keys())
    real_coords = [BLOCK_CENTROIDS[n] for n in real_names]
    tree = cKDTree(real_coords)
    dists, idxs = tree.query(list(zip(df["lat"], df["lon"])))
    df = df.copy()
    df["real_block"] = [real_names[i] for i in idxs]
    df["dist_deg"] = dists
    return df[df["dist_deg"] < MAX_DIST_DEG].copy()


def load_tiles(tiles_dir: Path) -> dict[str, pd.DataFrame]:
    dfs = {}
    for s in SCENARIOS:
        path = tiles_dir / f"chi_tiles_2030_{s}.csv"
        if not path.exists():
            raise FileNotFoundError(f"Missing: {path}")
        df = pd.read_csv(path)
        df = df[df["block_name"] != "Unknown"].copy()
        df = assign_real_blocks(df)
        dfs[s] = df
        print(f"  ✅ {path.name}: {len(df):,} tiles → {df['real_block'].nunique()} blocks")
    return dfs


def build_block_summary(dfs: dict[str, pd.DataFrame]) -> list[dict]:
    records = []
    ref_df = dfs["ssp245"]
    for block in BLOCK_CENTROIDS:
        rec: dict = {"block_name": block}
        rec["n_pixels"] = int((ref_df["real_block"] == block).sum())
        for s, df in dfs.items():
            sub = df[df["real_block"] == block]["chi"].dropna()
            rec[f"chi_{s}_mean"] = round(float(sub.mean()), 6) if len(sub) else None
            rec[f"chi_{s}_std"]  = round(float(sub.std()),  6) if len(sub) > 1 else 0.0
            rec[f"chi_{s}_p10"]  = round(float(sub.quantile(0.10)), 6) if len(sub) else None
            rec[f"chi_{s}_p90"]  = round(float(sub.quantile(0.90)), 6) if len(sub) else None
            for haz in HAZARD_COLS:
                if haz in df.columns:
                    hvals = df[df["real_block"] == block][haz].dropna()
                    rec[f"{haz}_{s}"] = round(float(hvals.mean()), 6) if len(hvals) else None
            if "elevation_m" in df.columns:
                ev = df[df["real_block"] == block]["elevation_m"].dropna()
                rec["elevation_m_mean"] = round(float(ev.mean()), 1) if len(ev) else None
        records.append(rec)
    print(f"\n  📊 block_summary: {len(records)} real blocks")
    return records


def build_trend_data(tiles_dir: Path) -> dict:
    scenarios_out = {}
    base_chi = None
    for s in SCENARIOS:
        path = tiles_dir / f"chi_trend_{s}.npy"
        if not path.exists():
            print(f"  ⚠️  {path.name} missing — skipping")
            continue
        arr = np.load(path).astype(float)
        n = len(arr)
        years = list(range(TREND_START_YEAR, TREND_START_YEAR + n))
        x = np.arange(n)
        slope, _ = np.polyfit(x, arr, 1)
        r2 = float(np.corrcoef(x, arr)[0, 1] ** 2)
        scenarios_out[s] = {
            "years":      years,
            "chi_values": [round(float(v), 6) for v in arr],
            "trend_slope_per_year": round(float(slope), 8),
            "trend_r2":   round(r2, 4),
            "chi_2005":   round(float(arr[0]), 6),
            "chi_2030":   round(float(arr[-1]), 6),
            "chi_change": round(float(arr[-1] - arr[0]), 6),
        }
        if s == "ssp126":
            base_chi = arr.copy()
    if base_chi is not None:
        for s in ["ssp245", "ssp585"]:
            if s in scenarios_out:
                delta = np.array(scenarios_out[s]["chi_values"]) - base_chi
                scenarios_out[s]["delta_vs_ssp126"] = [round(float(v), 6) for v in delta]
    return {
        "description": (
            "District-level mean CHI trend 2005-2030. "
            "NEX-GDDP-CMIP6 ensemble median, bias-corrected vs IMD 1985-2014 baseline."
        ),
        "scenarios": scenarios_out,
        "scenario_labels": {
            "ssp126": "SSP1-2.6 (Sustainability pathway)",
            "ssp245": "SSP2-4.5 (Middle-of-the-road)",
            "ssp585": "SSP5-8.5 (Fossil-fuelled development)",
        },
    }


def build_metadata(dfs: dict[str, pd.DataFrame]) -> dict:
    df_ref = dfs["ssp245"]
    return {
        "district":      "South 24 Parganas",
        "state":         "West Bengal",
        "country":       "India",
        "data_year":     "2030 representative (2028-2032 mean)",
        "baseline":      "IMD 1985-2014",
        "climate_model": "NEX-GDDP-CMIP6 ensemble",
        "scenarios":     SCENARIOS,
        "n_blocks":      len(BLOCK_CENTROIDS),
        "spatial_resolution_deg": 0.009,
        "bounding_box": {
            "lat_min": round(float(df_ref["lat"].min()), 4),
            "lat_max": round(float(df_ref["lat"].max()), 4),
            "lon_min": round(float(df_ref["lon"].min()), 4),
            "lon_max": round(float(df_ref["lon"].max()), 4),
        },
        "chi_formula": (
            "CHI = weighted combination of normalised hazard probabilities "
            "(heatwave, drought, flood, extreme_precip, cyclone, landslide); "
            "weights calibrated to South 24 Parganas agro-climatic profile."
        ),
        "spatial_join_method": f"KD-tree nearest-centroid, max_dist={MAX_DIST_DEG}°",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": "1.2.0",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir",  default="./data")
    parser.add_argument("--tiles-dir", default=".")
    args = parser.parse_args()

    data_dir  = Path(args.data_dir)
    tiles_dir = Path(args.tiles_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*55}")
    print("  Progyan export_for_web.py  v1.2.0")
    print(f"  tiles_dir → {tiles_dir.resolve()}")
    print(f"  data_dir  → {data_dir.resolve()}")
    print(f"{'='*55}\n")

    print("📥 Loading + spatially joining tile CSVs …")
    dfs = load_tiles(tiles_dir)

    print("\n🔢 Aggregating to real admin blocks …")
    summary = build_block_summary(dfs)
    out = data_dir / "block_summary.json"
    with open(out, "w") as f:
        json.dump(summary, f, separators=(",", ":"))
    print(f"  💾 {out}  ({out.stat().st_size/1024:.1f} KB)")

    print("\n📈 Building trend data …")
    trend = build_trend_data(tiles_dir)
    out = data_dir / "trend_data.json"
    with open(out, "w") as f:
        json.dump(trend, f, indent=2)
    print(f"  💾 {out}")

    print("\n🗂  Writing metadata …")
    meta = build_metadata(dfs)
    out = data_dir / "metadata.json"
    with open(out, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  💾 {out}")

    print(f"\n✅  Done. {len(summary)} real blocks exported.\n")


if __name__ == "__main__":
    main()
