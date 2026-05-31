"""
Progyan Backend — FastAPI
=========================
Serve via:  uvicorn main:app --host 0.0.0.0 --port 8000
Deploy to:  Render (free tier) → render.yaml included

Data files expected in DATA_DIR (set via env var DATA_PATH):
  block_summary.json    — CHI/hazard per block per scenario (from export_for_web.py)
  trend_data.json       — 26-year trend per scenario
  block_metadata.csv    — sowing calendars, cropping patterns
  metadata.json         — run info
"""

import os
import json
import csv
import math
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────
DATA_PATH = os.getenv("DATA_PATH", str(Path(__file__).parent))
DATA_DIR  = Path(DATA_PATH)

# ─────────────────────────────────────────────────────────
# DATA STORE  (module-level — survives lifespan context)
# ─────────────────────────────────────────────────────────
class DataStore:
    def __init__(self):
        self.block_chi:   dict = {}
        self.block_meta:  dict = {}
        self.trend:       dict = {}
        self.run_meta:    dict = {}
        self.block_names: list = []

store = DataStore()

# ─────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────
def _load_json(filename: str) -> Union[dict, list]:
    p = DATA_DIR / filename
    if not p.exists():
        raise FileNotFoundError(f"Required data file missing: {p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _load_block_summary() -> None:
    data = _load_json("block_summary.json")
    # Support both list-at-root and {"blocks": [...]} shapes
    if isinstance(data, dict):
        data = data.get("blocks", [])
    for row in data:
        name = str(row.get("block_name", "")).strip()
        if name:
            store.block_chi[name] = row
    store.block_names = sorted(store.block_chi.keys())
    log.info("block_summary.json — %d blocks loaded", len(store.block_chi))


def _load_block_metadata() -> None:
    p = DATA_DIR / "block_metadata.csv"
    if not p.exists():
        log.warning("block_metadata.csv missing — advisory will use fallback defaults")
        return
    with open(p, newline="", encoding="utf-8") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        for row in reader:
            row = {k: v.strip() for k, v in row.items()}
            name = row.get("block_name", "").strip()
            if not name:
                continue
            if name not in store.block_meta:
                store.block_meta[name] = []
            store.block_meta[name].append({
                "sowing_start":     row.get("typical_sowing_start", "June"),
                "sowing_end":       row.get("typical_sowing_end",   "July"),
                "cropping_pattern": row.get("cropping_pattern",     "Paddy rice"),
                "salinity_level":   row.get("salinity_level",       "Low"),
                "livelihood_type":  row.get("livelihood_type",      "Agriculture"),
            })
    total_rows = sum(len(v) for v in store.block_meta.values())
    log.info("block_metadata.csv — %d blocks, %d season rows", len(store.block_meta), total_rows)


def _load_trend() -> None:
    try:
        store.trend = _load_json("trend_data.json")
        log.info("trend_data.json loaded")
    except FileNotFoundError:
        log.warning("trend_data.json missing — trend endpoint will return 503")


def _load_meta() -> None:
    try:
        store.run_meta = _load_json("metadata.json")
        log.info("metadata.json loaded")
    except FileNotFoundError:
        store.run_meta = {"note": "metadata.json not found"}


# ─────────────────────────────────────────────────────────
# LIFESPAN  (replaces deprecated @app.on_event("startup"))
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(application: FastAPI):
    log.info("Loading data from: %s", DATA_DIR.resolve())
    try:
        _load_block_summary()
    except FileNotFoundError as exc:
        log.error("FATAL: %s", exc)
    _load_block_metadata()
    _load_trend()
    _load_meta()
    log.info("Startup complete. %d blocks ready.", len(store.block_names))
    yield


# ─────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────
app = FastAPI(
    title="Progyan — DiCRA Climate Sensor API",
    description=(
        "Climate risk advisories for South 24 Parganas. "
        "Backed by IMD 1985-2014 baseline + NEX-GDDP-CMIP6."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://climate-hazard.vercel.app/"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "HEAD"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# Serve static frontend if present
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

@app.get("/", include_in_schema=False, response_model=None)
def serve_index() -> Union[FileResponse, dict]:
    # 1. Try static/index.html
    index = _STATIC_DIR / "index.html"
    # 2. Fallback: index.html in same folder as main.py
    if not index.exists():
        index = Path(__file__).parent / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {
        "project": "Progyan — DiCRA Climate Sensor",
        "status": "operational",
        "docs": "/docs",
        "health": "/api/v1/health",
        "blocks": "/api/v1/blocks",
    }

# ─────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────
SCENARIO_NAMES: dict = {
    "ssp126": "SSP1-2.6 (Sustainability)",
    "ssp245": "SSP2-4.5 (Middle Road)",
    "ssp585": "SSP5-8.5 (Fossil-Fueled)",
}

MONTH_MAP: dict = {
    "January": 1, "February": 2, "March": 3,  "April":    4,
    "May":     5, "June":     6, "July":  7,  "August":   8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


# ─────────────────────────────────────────────────────────
# PURE HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────
def _chi(block_data: dict, scenario: str) -> Optional[float]:
    val = block_data.get(f"chi_{scenario}_mean")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _hazards(block_data: dict, scenario: str) -> dict:
    keys = [
        "heatwave_prob", "drought_prob", "flood_prob",
        "extreme_precip_prob", "cyclone_prob", "landslide_prob",
    ]
    result = {}
    for k in keys:
        col = f"{k}_{scenario}"
        if col in block_data and block_data[col] is not None:
            try:
                result[k] = float(block_data[col])
            except (TypeError, ValueError):
                pass
    return result


def _risk_tier(chi: float) -> str:
    if chi > 0.70:
        return "critical"
    if chi > 0.55:
        return "high"
    if chi > 0.40:
        return "moderate"
    return "low"


def _racs(chi: float, block_data: dict) -> dict:
    block_name = block_data.get("block_name", "")
    meta_rows  = store.block_meta.get(block_name, [{}])
    salinity   = meta_rows[0].get("salinity_level", "Low") if meta_rows else "Low"
    adopted    = salinity == "Low"
    base       = 700
    disc       = int(base * 0.15) if adopted else 0
    racs       = base - disc
    if chi > 0.70:
        rate = 12.0
    elif chi > 0.50:
        rate = 9.5
    else:
        rate = 7.0
    if adopted:
        rate = round(rate - 1.5, 1)
    prov = "15%" if chi > 0.70 else "8%" if chi > 0.50 else "4%"
    tier = _risk_tier(chi)
    return {
        "racs":              racs,
        "base_score":        base,
        "adoption_discount": disc,
        "adopted":           adopted,
        "interest_rate":     rate,
        "provisioning":      prov,
        "risk_category":     tier.upper(),
        "recommendation": (
            "Hold — review provisioning immediately" if chi > 0.70
            else "Proceed with caution; monitor quarterly" if chi > 0.50
            else "Normal lending permitted"
        ),
    }


def _clpi(chi: float, block_data: dict) -> float:
    block_name = block_data.get("block_name", "")
    meta_rows  = store.block_meta.get(block_name, [{}])
    salinity   = meta_rows[0].get("salinity_level", "Low") if meta_rows else "Low"
    wealth     = {"High": 0.25, "Moderate": 0.45, "Low": 0.65}.get(salinity, 0.55)
    wind       = min(0.97, chi * 0.72 + 0.08)
    inund      = min(0.97, chi * 0.65)
    return round(0.4 * wind + 0.3 * chi + 0.2 * inund + 0.1 * (1.0 - wealth), 4)


def _current_season_row(block_name: str) -> Optional[dict]:
    rows = store.block_meta.get(block_name, [])
    if not rows:
        return None
    month_idx = datetime.utcnow().month
    for row in rows:
        start = MONTH_MAP.get(row.get("sowing_start", ""), 0)
        end   = MONTH_MAP.get(row.get("sowing_end",   ""), 0)
        if start and end and start <= month_idx <= end:
            return row
    def _dist(row: dict) -> int:
        s = MONTH_MAP.get(row.get("sowing_start", ""), 1)
        d = abs(month_idx - s)
        return min(d, 12 - d)
    return min(rows, key=_dist)


# ─────────────────────────────────────────────────────────
# ADVISORY ENGINE
# ─────────────────────────────────────────────────────────
def _generate_advisory(
    block_name: str,
    chi: float,
    hazards: dict,
    season_row: Optional[dict],
) -> dict:
    sr           = season_row or {}
    tier         = _risk_tier(chi)
    salinity     = sr.get("salinity_level",   "Low")
    livelihood   = sr.get("livelihood_type",  "Agriculture")
    crop_pattern = sr.get("cropping_pattern", "Paddy rice")
    sow_start    = sr.get("sowing_start",     "June")
    sow_end      = sr.get("sowing_end",       "July")

    high_sal     = salinity == "High"
    mod_sal      = salinity == "Moderate"
    pisciculture = "Pisciculture" in livelihood or "Aquaculture" in livelihood

    top_haz      = max(hazards, key=hazards.get) if hazards else "flood_prob"
    primary_crop = crop_pattern.split("+")[0].strip()

    if tier == "critical":
        salinity_note = None
        if high_sal:
            salinity_note = (
                f"URGENT — Apply gypsum 250 kg/ha before next sowing. "
                f"Current crop pattern ({crop_pattern}) must shift to salt-tolerant "
                f"varieties CSR 36 / Lunishree / CR Dhan 602. "
                f"Do not irrigate from tidal sources."
            )
        elif mod_sal:
            salinity_note = (
                "Soil EC likely elevated — test before sowing. "
                "If EC > 1.5 dS/m, leach with fresh water before transplanting."
            )
        pisciculture_note = (
            "Close all sluice gates immediately. Pre-position emergency aerators. "
            "Do not stock new fish. Protect spawn from saline surge."
        ) if pisciculture else None
        sms_raw = (
            f"CRITICAL {block_name}: DO NOT SOW. Harvest now. Bunds 60cm. "
            f"CHI={chi:.2f}. PMFBY mandatory Rs1000/bigha. "
            f"IMD: imd.gov.in. 1800-180-1551"
        )
        return {
            "tier":                tier,
            "traffic_light":       "red",
            "headline":            f"CRITICAL RISK — Do not sow in {block_name}",
            "crop_recommendation": "No sowing recommended this season",
            "varieties":           "Hold all seed stock — await conditions to improve",
            "sowing_window":       f"Defer beyond {sow_end} — monitor IMD bulletins",
            "insurance":           "MANDATORY — PMFBY (Rs 1,000 per bigha). Register immediately.",
            "actions": [
                f"Harvest any standing {primary_crop} within 48 hours",
                "Raise all field bunds to minimum 60 cm height immediately",
                "Clear drainage channels of silt and debris before next rain event",
                "Move livestock, stored grain, and farming equipment to higher ground",
                "Register for PMFBY crop insurance before the sowing notification date",
                "Secure 3-5 day freshwater supply for livestock",
                "Monitor IMD cyclone and flood warnings daily (imd.gov.in)",
            ],
            "salinity_note":     salinity_note,
            "pisciculture_note": pisciculture_note,
            "sms":               sms_raw[:160],
        }

    elif tier == "high":
        delay_days = 14 if top_haz in ("flood_prob", "cyclone_prob") else 12
        if high_sal:
            recommend_crop = "Salt-tolerant Aman rice (RS-20, Tijai) + flood-tolerant varieties"
            varieties      = "Swarna-Sub1, CR Dhan 602, Tijai, RS-20"
        elif top_haz == "drought_prob":
            recommend_crop = f"{crop_pattern} — short-duration / drought-resistant"
            varieties      = "Sahbhagi Dhan, Swarna-Sub1, MTU 1010"
        else:
            recommend_crop = crop_pattern
            varieties      = "Swarna-Sub1, CR Dhan 602, HYV flood-tolerant"
        salinity_note = None
        if high_sal:
            salinity_note = (
                f"Use salt-tolerant varieties from your existing pattern ({crop_pattern}). "
                f"Apply gypsum 200 kg/ha before transplanting. "
                f"Avoid furrow irrigation — use basin flooding."
            )
        elif mod_sal:
            salinity_note = (
                "Test soil EC before sowing. If above 1.2 dS/m, "
                "apply 150 kg/ha gypsum and leach field."
            )
        pisciculture_note = (
            "Monitor pond salinity (>5 ppt stressful for major carps). "
            "Prepare lime application 25 kg/ha against acidification risk. "
            "Keep 10-day feed stock."
        ) if pisciculture else None
        sms_raw = (
            f"HIGH RISK {block_name}: Delay {primary_crop[:20]} {delay_days}d. "
            f"Use Swarna-Sub1. PMFBY Rs500/bigha. "
            f"CHI={chi:.2f}. Window: {sow_start}-{sow_end}."
        )
        return {
            "tier":                tier,
            "traffic_light":       "amber",
            "headline":            f"HIGH RISK — Delay sowing in {block_name} by {delay_days} days",
            "crop_recommendation": recommend_crop,
            "varieties":           varieties,
            "sowing_window":       f"Delay {delay_days} days from normal: {sow_start}-{sow_end}. Watch 7-day forecast.",
            "insurance":           "RECOMMENDED — PMFBY Rs 500 per bigha. Register before sowing.",
            "actions": [
                f"Delay {primary_crop} transplanting by {delay_days} days",
                "Prepare raised seed beds 15 cm above normal field level",
                "Clear and deepen main drainage channels before monsoon onset",
                "Stockpile contingency seed covering at least 20% of your holding area",
                "Apply mulch (paddy straw 3-4 t/ha) to retain soil moisture",
                "Install shade nets over nurseries to reduce heat stress on seedlings",
                "Enrol in PMFBY before the block sowing notification date",
            ],
            "salinity_note":     salinity_note,
            "pisciculture_note": pisciculture_note,
            "sms":               sms_raw[:160],
        }

    elif tier == "moderate":
        varieties = (
            "Sahbhagi Dhan, DroughtMaster, IET-5656"
            if top_haz == "drought_prob"
            else "Swarna-Sub1, IET-5656, Samba Masuri"
        )
        salinity_note = (
            "Test soil EC before sowing. If EC > 1 dS/m, apply 100 kg/ha gypsum."
        ) if mod_sal else None
        pisciculture_note = (
            "Monitor water salinity in ponds twice weekly. "
            "Watch for pH drop after heavy rain."
        ) if pisciculture else None
        sms_raw = (
            f"MODERATE {block_name}: Delay {primary_crop[:20]} 7-10d. "
            f"CHI={chi:.2f}. Normal window {sow_start}-{sow_end}. "
            f"Insurance optional Rs200/bigha."
        )
        return {
            "tier":                tier,
            "traffic_light":       "yellow",
            "headline":            f"MODERATE RISK — Proceed with precautions in {block_name}",
            "crop_recommendation": crop_pattern,
            "varieties":           varieties,
            "sowing_window":       f"Delay 7-10 days from normal: {sow_start}-{sow_end}",
            "insurance":           "OPTIONAL — Rs 200 per bigha (recommended for small holders <2 ha)",
            "actions": [
                f"Delay {primary_crop} by 7-10 days",
                "Apply mulch (paddy straw 2 t/ha) to conserve soil moisture",
                "Use drip or sprinkler irrigation if available; minimise flood irrigation",
                "Monitor soil moisture weekly using tensiometer or feel method",
                "Keep 10% extra contingency seed in reserve",
            ],
            "salinity_note":     salinity_note,
            "pisciculture_note": pisciculture_note,
            "sms":               sms_raw[:160],
        }

    else:  # low
        sms_raw = (
            f"LOW RISK {block_name}: Proceed {primary_crop[:20]} "
            f"{sow_start}-{sow_end}. CHI={chi:.2f}. "
            f"Standard practices. Normal conditions expected."
        )
        return {
            "tier":                tier,
            "traffic_light":       "green",
            "headline":            f"LOW RISK — Normal operations in {block_name}",
            "crop_recommendation": crop_pattern,
            "varieties":           "HYVs — IET-5656, Swarna, MTU 1010, Samba Masuri",
            "sowing_window":       f"Normal window: {sow_start}-{sow_end}",
            "insurance":           "Standard crop insurance — not urgently required",
            "actions": [
                f"Proceed with {primary_crop} on normal calendar",
                "Apply recommended NPK fertiliser doses at transplanting",
                "Ensure field levelling before transplanting for even water distribution",
                "Monitor for pest/disease outbreaks (blast, BLB, brown plant hopper)",
                "Verify irrigation channel functionality before sowing",
            ],
            "salinity_note":     None,
            "pisciculture_note": None,
            "sms":               sms_raw[:160],
        }


# ─────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.get("/api/v1/info", tags=["health"])
def info() -> dict:
    return {
        "project":       "Progyan — DiCRA Climate Sensor",
        "version":       "1.0.0",
        "status":        "operational",
        "blocks_loaded": len(store.block_names),
        "docs":          "/docs",
    }


@app.get("/api/v1/health", tags=["health"])
def health() -> dict:
    return {
        "status":          "ok",
        "blocks":          len(store.block_names),
        "metadata_blocks": len(store.block_meta),
        "trend_scenarios": list(store.trend.get("scenarios", {}).keys()),
        "data_path":       str(DATA_DIR.resolve()),
        "run_info":        store.run_meta,
    }


@app.get("/api/v1/blocks", tags=["data"])
def list_blocks() -> dict:
    return {
        "district": store.run_meta.get("district", "South 24 Parganas"),
        "count":    len(store.block_names),
        "blocks":   store.block_names,
    }


@app.get("/api/v1/blocks/{block_name}", tags=["data"])
def get_block(
    block_name: str,
    scenario: str = Query("ssp245", pattern="^ssp(126|245|585)$"),
) -> dict:
    b = store.block_chi.get(block_name)
    if not b:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Block '{block_name}' not found. "
                f"Available (first 5): {store.block_names[:5]}"
            ),
        )
    chi = _chi(b, scenario)
    if chi is None:
        raise HTTPException(
            status_code=422,
            detail=f"CHI data not available for scenario '{scenario}' in block '{block_name}'.",
        )
    hazards  = _hazards(b, scenario)
    season   = _current_season_row(block_name)
    advisory = _generate_advisory(block_name, chi, hazards, season)
    racs_out = _racs(chi, b)
    clpi_out = _clpi(chi, b)
    return {
        "block_name":    block_name,
        "scenario":      scenario,
        "scenario_name": SCENARIO_NAMES[scenario],
        "data_year":     "2030 representative (2028-2032 mean)",
        "chi": {
            "mean":     chi,
            "std":      b.get(f"chi_{scenario}_std"),
            "p10":      b.get(f"chi_{scenario}_p10"),
            "p90":      b.get(f"chi_{scenario}_p90"),
            "n_pixels": b.get("n_pixels"),
        },
        "hazards":  hazards,
        "advisory": advisory,
        "racs":     racs_out,
        "clpi":     clpi_out,
        "metadata": {
            "salinity":     (season or {}).get("salinity_level"),
            "livelihood":   (season or {}).get("livelihood_type"),
            "current_crop": (season or {}).get("cropping_pattern"),
            "sowing_start": (season or {}).get("sowing_start"),
            "sowing_end":   (season or {}).get("sowing_end"),
            "all_seasons":  store.block_meta.get(block_name, []),
        },
        "scenario_comparison": {
            sc: {
                "chi_mean": _chi(b, sc),
                "chi_std":  b.get(f"chi_{sc}_std"),
            }
            for sc in ["ssp126", "ssp245", "ssp585"]
        },
    }


@app.get("/api/v1/district/summary", tags=["data"])
def district_summary(
    scenario: str = Query("ssp245", pattern="^ssp(126|245|585)$"),
) -> dict:
    rows = []
    for name, b in store.block_chi.items():
        chi = _chi(b, scenario)
        if chi is None:
            continue
        rows.append({"name": name, "chi": chi, "clpi": _clpi(chi, b)})
    chis = [r["chi"] for r in rows]
    if not chis:
        raise HTTPException(status_code=503, detail="No CHI data loaded for this scenario.")
    mean_chi = sum(chis) / len(chis)
    std_chi  = math.sqrt(sum((c - mean_chi) ** 2 for c in chis) / len(chis))
    return {
        "district":        store.run_meta.get("district", "South 24 Parganas"),
        "scenario":        scenario,
        "scenario_name":   SCENARIO_NAMES[scenario],
        "n_blocks":        len(rows),
        "chi_mean":        round(mean_chi, 4),
        "chi_std":         round(std_chi, 4),
        "critical_blocks": len([c for c in chis if c > 0.70]),
        "high_blocks":     len([c for c in chis if 0.55 < c <= 0.70]),
        "moderate_blocks": len([c for c in chis if 0.40 < c <= 0.55]),
        "low_blocks":      len([c for c in chis if c <= 0.40]),
        "top5_risk":       sorted(rows, key=lambda r: r["chi"], reverse=True)[:5],
        "data_source":     store.run_meta,
    }


@app.get("/api/v1/policy/clpi", tags=["policy"])
def clpi_ranking(
    scenario: str = Query("ssp245", pattern="^ssp(126|245|585)$"),
    limit: int    = Query(29, ge=1, le=100),
) -> dict:
    rows = []
    for name, b in store.block_chi.items():
        chi = _chi(b, scenario)
        if chi is None:
            continue
        meta_rows = store.block_meta.get(name, [{}])
        season    = meta_rows[0] if meta_rows else {}
        clpi      = _clpi(chi, b)
        tier      = _risk_tier(chi)
        rows.append({
            "block_name":        name,
            "chi_mean":          round(chi, 4),
            "clpi":              clpi,
            "risk_tier":         tier,
            "salinity":          season.get("salinity_level", "-"),
            "livelihood":        season.get("livelihood_type", "-"),
            "budget_suggestion": (
                "Rs 80-100L" if tier == "critical" else
                "Rs 50-75L"  if tier == "high"     else
                "Rs 25-45L"  if tier == "moderate" else
                "Rs 10-20L"
            ),
            "priority_action": (
                "Embankment reinforcement + evacuation + early warning" if tier == "critical" else
                "Flood-resistant infra + drainage upgrade + insurance"  if tier == "high"     else
                "Capacity building + contingency seeds + soil health"   if tier == "moderate" else
                "Standard development programme"
            ),
        })
    rows.sort(key=lambda r: r["clpi"], reverse=True)
    return {
        "district":       store.run_meta.get("district", "South 24 Parganas"),
        "scenario":       scenario,
        "year":           "2030",
        "roi_multiplier": 2.2,
        "clpi_formula":   "0.40*wind + 0.30*CHI + 0.20*inundation + 0.10*(1-wealth)",
        "blocks":         rows[:limit],
    }


@app.get("/api/v1/finance/racs", tags=["finance"])
def racs_portfolio(
    scenario: str = Query("ssp245", pattern="^ssp(126|245|585)$"),
) -> dict:
    rows = []
    for name, b in store.block_chi.items():
        chi = _chi(b, scenario)
        if chi is None:
            continue
        rows.append({"block_name": name, "chi": round(chi, 4), **_racs(chi, b)})
    rows.sort(key=lambda r: r["chi"], reverse=True)
    avg_racs = round(sum(r["racs"] for r in rows) / len(rows), 1) if rows else None
    return {
        "scenario":         scenario,
        "portfolio_blocks": len(rows),
        "avg_racs":         avg_racs,
        "blocks":           rows,
    }


@app.get("/api/v1/trend", tags=["data"])
def get_trend() -> dict:
    if not store.trend:
        raise HTTPException(
            status_code=503,
            detail="Trend data not loaded. Run export_for_web.py first.",
        )
    return store.trend


@app.get("/api/v1/metadata/seasons/{block_name}", tags=["data"])
def get_seasons(block_name: str) -> dict:
    seasons = store.block_meta.get(block_name)
    if not seasons:
        raise HTTPException(
            status_code=404,
            detail=f"No season data found for block '{block_name}'.",
        )
    return {"block_name": block_name, "seasons": seasons}


@app.get("/api/v1/geography", tags=["geography"])
def list_geography() -> dict:
    return {
        "states": [
            {
                "state": "West Bengal",
                "districts": [
                    {
                        "district":       "South 24 Parganas",
                        "blocks":         store.block_names,
                        "status":         "live",
                        "data_available": True,
                    }
                ],
            }
        ],
        "note": (
            "Additional states/districts can be onboarded by uploading "
            "new block_summary.json and block_metadata.csv for each district."
        ),
    }


# ─────────────────────────────────────────────────────────
# FEEDBACK
# ─────────────────────────────────────────────────────────
class FeedbackIn(BaseModel):
    block_name:    str
    event_type:    str
    event_date:    str
    severity:      int
    description:   Optional[str] = None
    reporter_type: str


@app.post("/api/v1/feedback", tags=["feedback"])
def submit_feedback(body: FeedbackIn) -> dict:
    log_path = DATA_DIR / "feedback_log.jsonl"
    entry = {
        **body.model_dump(),          # .dict() is deprecated in Pydantic v2
        "received_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.error("Could not write feedback log: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to persist feedback.")
    return {"status": "received", "entry": entry}
