"""
Progyan Backend — FastAPI
=========================
Serve via:  uvicorn main:app --host 0.0.0.0 --port 8000
Deploy to:  Render (free tier) → render.yaml included

Data files expected in DATA_DIR (set via env var DATA_PATH):
  block_summary.json    — CHI/hazard per block per scenario (from export_for_web.py)
  trend_data.json       — 26-year trend per scenario
  block_metadata.csv    — sowing calendars, cropping patterns (the CSV you have)
  metadata.json         — run info
"""

import os, json, csv, math
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────
DATA_PATH = os.getenv("DATA_PATH", "./data")
DATA_DIR  = Path(DATA_PATH)

app = FastAPI(
    title="Progyan — DiCRA Climate Sensor API",
    description="Climate risk advisories for South 24 Parganas. "
                "Backed by IMD 1985–2014 baseline + NEX-GDDP-CMIP6.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to your Vercel URL in production
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────
# DATA LOADING  (loaded once at startup, held in memory)
# ─────────────────────────────────────────────────────────
class DataStore:
    block_chi: dict        = {}   # block_name → {chi_ssp126_mean, …}
    block_meta: dict       = {}   # block_name → [season_rows]
    trend: dict            = {}   # from trend_data.json
    run_meta: dict         = {}   # from metadata.json
    block_names: list      = []

store = DataStore()

def _load_json(filename: str) -> dict | list:
    p = DATA_DIR / filename
    if not p.exists():
        raise FileNotFoundError(f"Required data file missing: {p}")
    with open(p) as f:
        return json.load(f)

def _load_block_summary():
    data = _load_json("block_summary.json")
    for row in data:
        name = row.get("block_name","").strip()
        if name:
            store.block_chi[name] = row
    store.block_names = sorted(store.block_chi.keys())
    print(f"  ✅ block_summary.json — {len(store.block_chi)} blocks")

def _load_block_metadata():
    p = DATA_DIR / "block_metadata.csv"
    if not p.exists():
        print("  ⚠️  block_metadata.csv missing — advisory will use defaults")
        return
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["block_name"].strip()
            if name not in store.block_meta:
                store.block_meta[name] = []
            store.block_meta[name].append({
                "sowing_start":    row["typical_sowing_start"].strip(),
                "sowing_end":      row["typical_sowing_end"].strip(),
                "cropping_pattern":row["cropping_pattern"].strip(),
                "salinity_level":  row["salinity_level"].strip(),
                "livelihood_type": row["livelihood_type"].strip(),
            })
    print(f"  ✅ block_metadata.csv — {len(store.block_meta)} blocks, "
          f"{sum(len(v) for v in store.block_meta.values())} season rows")

def _load_trend():
    try:
        store.trend = _load_json("trend_data.json")
        print("  ✅ trend_data.json loaded")
    except FileNotFoundError:
        print("  ⚠️  trend_data.json missing")

def _load_meta():
    try:
        store.run_meta = _load_json("metadata.json")
        print("  ✅ metadata.json loaded")
    except FileNotFoundError:
        store.run_meta = {"note": "metadata.json not found"}

@app.on_event("startup")
def startup():
    print(f"\n📂 Loading data from: {DATA_DIR.resolve()}")
    _load_block_summary()
    _load_block_metadata()
    _load_trend()
    _load_meta()
    print(f"✅ Startup complete. {len(store.block_names)} blocks ready.\n")

# ─────────────────────────────────────────────────────────
# ADVISORY ENGINE
# Driven entirely by block_metadata.csv — no hardcoding
# ─────────────────────────────────────────────────────────

def _current_season_row(block_name: str) -> dict | None:
    """Return the season row from block_metadata.csv that matches today's month."""
    rows = store.block_meta.get(block_name, [])
    if not rows:
        return None
    month_idx = datetime.utcnow().month   # 1-12
    MONTH_MAP = {
        "January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
        "July":7,"August":8,"September":9,"October":10,"November":11,"December":12,
    }
    for row in rows:
        start = MONTH_MAP.get(row["sowing_start"], 0)
        end   = MONTH_MAP.get(row["sowing_end"],   0)
        # Simple range check (handles wrap-around crudely)
        if start <= month_idx <= end:
            return row
    # Fallback: return the row whose start month is closest
    def dist(row):
        s = MONTH_MAP.get(row["sowing_start"], 1)
        return min(abs(month_idx - s), 12 - abs(month_idx - s))
    return min(rows, key=dist)

def _risk_tier(chi: float) -> str:
    if chi > 0.70: return "critical"
    if chi > 0.55: return "high"
    if chi > 0.40: return "moderate"
    return "low"

def _traffic_light(tier: str) -> str:
    return {"critical":"red","high":"amber","moderate":"yellow","low":"green"}[tier]

def _generate_advisory(block_name: str, chi: float,
                        hazards: dict, season_row: dict | None) -> dict:
    """
    Generate advisory purely from:
      - chi value (computed from CMIP6 pipeline)
      - hazards dict (flood/heatwave/drought probabilities from pipeline)
      - season_row from block_metadata.csv (actual sowing dates, crops, salinity)

    No hardcoded block names. Scales to any block that appears in block_metadata.csv.
    """
    tier = _risk_tier(chi)
    salinity = (season_row or {}).get("salinity_level", "Low")
    livelihood = (season_row or {}).get("livelihood_type", "Agriculture")
    crop_pattern = (season_row or {}).get("cropping_pattern", "Paddy rice")
    sow_start = (season_row or {}).get("sowing_start", "June")
    sow_end   = (season_row or {}).get("sowing_end",   "July")

    high_sal  = salinity == "High"
    mod_sal   = salinity == "Moderate"
    pisciculture = "Pisciculture" in livelihood or "Aquaculture" in livelihood

    # Dominant hazard
    top_haz = max(hazards, key=hazards.get) if hazards else "flood"

    # ── Core advisory logic (data-driven tiers, metadata-driven content) ──
    if tier == "critical":
        return {
            "tier": tier, "traffic_light": "red",
            "headline": f"CRITICAL RISK — Do not sow in {block_name}",
            "crop_recommendation": "No sowing recommended this season",
            "varieties": "Hold all seed stock — await conditions to improve",
            "sowing_window": f"Defer beyond {sow_end} — monitor IMD bulletins",
            "insurance": "MANDATORY — PMFBY (₹1,000 per bigha). Register immediately.",
            "actions": [
                f"Harvest any standing {crop_pattern.split('+')[0].strip()} within 48 hours",
                "Raise all field bunds to minimum 60 cm height immediately",
                "Clear drainage channels of silt and debris before next rain event",
                "Move livestock, stored grain, and farming equipment to higher ground",
                "Register for PMFBY crop insurance before the sowing notification date",
                "Secure 3–5 day freshwater supply for livestock",
                "Monitor IMD cyclone and flood warnings daily (imd.gov.in)",
            ],
            "salinity_note": (
                f"URGENT — Apply gypsum 250 kg/ha before next sowing. "
                f"Current crop pattern ({crop_pattern}) must shift to salt-tolerant varieties "
                f"CSR 36 / Lunishree / CR Dhan 602. Do not irrigate from tidal sources."
            ) if high_sal else (
                f"Soil EC likely elevated — test before sowing. If EC > 1.5 dS/m, "
                f"leach with fresh water before transplanting."
            ) if mod_sal else None,
            "pisciculture_note": (
                "Close all sluice gates immediately. Pre-position emergency aerators. "
                "Do not stock new fish. Protect spawn from saline surge."
            ) if pisciculture else None,
            "sms": f"CRITICAL {block_name}: DO NOT SOW. Harvest now. Bunds 60cm. CHI={chi:.2f}. PMFBY mandatory ₹1000/bigha. IMD: imd.gov.in. 1800-180-1551"[:160],
        }

    elif tier == "high":
        delay_days = 14 if top_haz in ("flood_prob","cyclone_prob") else 12
        recommend_crop = crop_pattern  # use actual metadata crop
        if high_sal:
            recommend_crop = "Salt-tolerant Aman rice (RS-20, Tijai) + flood-tolerant varieties"
            varieties = "Swarna-Sub1, CR Dhan 602, Tijai, RS-20"
        elif top_haz == "drought_prob":
            recommend_crop = crop_pattern + " — short-duration / drought-resistant"
            varieties = "Sahbhagi Dhan, Swarna-Sub1, MTU 1010"
        else:
            varieties = "Swarna-Sub1, CR Dhan 602, HYV flood-tolerant"
        return {
            "tier": tier, "traffic_light": "amber",
            "headline": f"HIGH RISK — Delay sowing in {block_name} by {delay_days} days",
            "crop_recommendation": recommend_crop,
            "varieties": varieties,
            "sowing_window": f"Delay {delay_days} days from normal: {sow_start}–{sow_end}. Watch 7-day forecast.",
            "insurance": "RECOMMENDED — PMFBY ₹500 per bigha. Register before sowing.",
            "actions": [
                f"Delay {crop_pattern.split('+')[0].strip()} transplanting by {delay_days} days",
                "Prepare raised seed beds 15 cm above normal field level",
                "Clear and deepen main drainage channels before monsoon onset",
                "Stockpile contingency seed covering at least 20% of your holding area",
                "Apply mulch (paddy straw 3–4 t/ha) to retain soil moisture",
                "Install shade nets over nurseries to reduce heat stress on seedlings",
                "Enrol in PMFBY before the block sowing notification date",
            ],
            "salinity_note": (
                f"Use salt-tolerant varieties from your existing pattern ({crop_pattern}). "
                f"Apply gypsum 200 kg/ha before transplanting. Avoid furrow irrigation — use basin flooding."
            ) if high_sal else (
                f"Test soil EC before sowing. If above 1.2 dS/m, apply 150 kg/ha gypsum and leach field."
            ) if mod_sal else None,
            "pisciculture_note": (
                f"Monitor pond salinity (>5 ppt stressful for major carps). "
                f"Prepare lime application 25 kg/ha against acidification risk. "
                f"Keep 10-day feed stock."
            ) if pisciculture else None,
            "sms": f"HIGH RISK {block_name}: Delay {crop_pattern.split('+')[0][:20]} {delay_days}d. Use Swarna-Sub1. PMFBY ₹500/bigha. CHI={chi:.2f}. Window: {sow_start}-{sow_end}."[:160],
        }

    elif tier == "moderate":
        return {
            "tier": tier, "traffic_light": "yellow",
            "headline": f"MODERATE RISK — Proceed with precautions in {block_name}",
            "crop_recommendation": crop_pattern,
            "varieties": (
                "Sahbhagi Dhan, DroughtMaster, IET-5656" if top_haz == "drought_prob"
                else "Swarna-Sub1, IET-5656, Samba Masuri"
            ),
            "sowing_window": f"Delay 7–10 days from normal: {sow_start}–{sow_end}",
            "insurance": "OPTIONAL — ₹200 per bigha (recommended for small holders <2 ha)",
            "actions": [
                f"Delay {crop_pattern.split('+')[0].strip()} by 7–10 days",
                "Apply mulch (paddy straw 2 t/ha) to conserve soil moisture",
                "Use drip or sprinkler irrigation if available; minimise flood irrigation",
                "Monitor soil moisture weekly using tensiometer or feel method",
                "Keep 10% extra contingency seed in reserve",
            ],
            "salinity_note": (
                "Test soil EC before sowing. If EC > 1 dS/m, apply 100 kg/ha gypsum."
            ) if mod_sal else None,
            "pisciculture_note": (
                "Monitor water salinity in ponds twice weekly. Watch for pH drop after heavy rain."
            ) if pisciculture else None,
            "sms": f"MODERATE {block_name}: Delay {crop_pattern.split('+')[0][:20]} 7-10d. CHI={chi:.2f}. Normal window {sow_start}-{sow_end}. Insurance optional ₹200/bigha."[:160],
        }

    else:  # low
        return {
            "tier": tier, "traffic_light": "green",
            "headline": f"LOW RISK — Normal operations in {block_name}",
            "crop_recommendation": crop_pattern,
            "varieties": "HYVs — IET-5656, Swarna, MTU 1010, Samba Masuri",
            "sowing_window": f"Normal window: {sow_start}–{sow_end}",
            "insurance": "Standard crop insurance — not urgently required",
            "actions": [
                f"Proceed with {crop_pattern.split('+')[0].strip()} on normal calendar",
                "Apply recommended NPK fertiliser doses at transplanting",
                "Ensure field levelling before transplanting for even water distribution",
                "Monitor for pest/disease outbreaks (blast, BLB, brown plant hopper)",
                "Verify irrigation channel functionality before sowing",
            ],
            "salinity_note": None,
            "pisciculture_note": None,
            "sms": f"LOW RISK {block_name}: Proceed {crop_pattern.split('+')[0][:20]} {sow_start}-{sow_end}. CHI={chi:.2f}. Standard practices. Normal conditions expected."[:160],
        }

# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────
SCENARIO_NAMES = {
    "ssp126": "SSP1-2.6 (Sustainability)",
    "ssp245": "SSP2-4.5 (Middle Road)",
    "ssp585": "SSP5-8.5 (Fossil-Fueled)",
}

def _chi(block_data: dict, scenario: str) -> float | None:
    val = block_data.get(f"chi_{scenario}_mean")
    return float(val) if val is not None else None

def _hazards(block_data: dict, scenario: str) -> dict:
    keys = ["heatwave_prob","drought_prob","flood_prob",
            "extreme_precip_prob","cyclone_prob","landslide_prob"]
    return {
        k: float(block_data[f"{k}_{scenario}"])
        for k in keys
        if f"{k}_{scenario}" in block_data
    }

def _racs(chi: float, block_data: dict) -> dict:
    salinity = (store.block_meta.get(block_data["block_name"],[{}])[0]
                .get("salinity_level","Low"))
    adopted = salinity == "Low"
    base = 700
    disc = int(base * 0.15) if adopted else 0
    racs = base - disc
    rate = 12.0 if chi > 0.70 else 9.5 if chi > 0.50 else 7.0
    if adopted: rate = round(rate - 1.5, 1)
    prov = "15%" if chi > 0.70 else "8%" if chi > 0.50 else "4%"
    risk = _risk_tier(chi)
    return {
        "racs": racs, "base_score": base,
        "adoption_discount": disc, "adopted": adopted,
        "interest_rate": rate, "provisioning": prov,
        "risk_category": risk.upper(),
        "recommendation": (
            "Hold — review provisioning immediately" if chi > 0.70
            else "Proceed with caution; monitor quarterly" if chi > 0.50
            else "Normal lending permitted"
        ),
    }

def _clpi(chi: float, block_data: dict) -> float:
    salinity = (store.block_meta.get(block_data["block_name"],[{}])[0]
                .get("salinity_level","Low"))
    wealth = {"High": 0.25, "Moderate": 0.45, "Low": 0.65}.get(salinity, 0.55)
    wind   = min(0.97, chi * 0.72 + 0.08)
    inund  = min(0.97, chi * 0.65)
    return round(0.4 * wind + 0.3 * chi + 0.2 * inund + 0.1 * (1 - wealth), 4)

# ─────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.get("/", tags=["health"])
def root():
    return {
        "project": "Progyan — DiCRA Climate Sensor",
        "version": "1.0.0",
        "status": "operational",
        "blocks_loaded": len(store.block_names),
        "docs": "/docs",
    }

@app.get("/api/v1/health", tags=["health"])
def health():
    return {
        "status": "ok",
        "blocks": len(store.block_names),
        "metadata_blocks": len(store.block_meta),
        "trend_scenarios": list(store.trend.get("scenarios",{}).keys()),
        "data_path": str(DATA_DIR.resolve()),
        "run_info": store.run_meta,
    }

@app.get("/api/v1/blocks", tags=["data"])
def list_blocks():
    """All blocks with CHI summary. Scalable — reads from JSON, no hardcoding."""
    return {
        "district": store.run_meta.get("district", "South 24 Parganas"),
        "count": len(store.block_names),
        "blocks": store.block_names,
    }

@app.get("/api/v1/blocks/{block_name}", tags=["data"])
def get_block(
    block_name: str,
    scenario: str = Query("ssp245", pattern="^ssp(126|245|585)$"),
):
    """Full block data for a given scenario — used by dashboard block selector."""
    b = store.block_chi.get(block_name)
    if not b:
        raise HTTPException(404, f"Block '{block_name}' not found. "
                                 f"Available: {store.block_names[:5]}…")
    chi  = _chi(b, scenario)
    if chi is None:
        raise HTTPException(422, f"CHI not available for scenario '{scenario}'")
    hazards = _hazards(b, scenario)
    season  = _current_season_row(block_name)
    advisory = _generate_advisory(block_name, chi, hazards, season)
    racs   = _racs(chi, b)
    clpi   = _clpi(chi, b)

    return {
        "block_name":   block_name,
        "scenario":     scenario,
        "scenario_name":SCENARIO_NAMES[scenario],
        "data_year":    "2030 representative (2028–2032 mean)",
        "chi": {
            "mean": chi,
            "std":  b.get(f"chi_{scenario}_std"),
            "p10":  b.get(f"chi_{scenario}_p10"),
            "p90":  b.get(f"chi_{scenario}_p90"),
            "n_pixels": b.get("n_pixels"),
        },
        "hazards":  hazards,
        "advisory": advisory,
        "racs":     racs,
        "clpi":     clpi,
        "metadata": {
            "salinity":    (season or {}).get("salinity_level"),
            "livelihood":  (season or {}).get("livelihood_type"),
            "current_crop":(season or {}).get("cropping_pattern"),
            "sowing_start":(season or {}).get("sowing_start"),
            "sowing_end":  (season or {}).get("sowing_end"),
            "all_seasons": store.block_meta.get(block_name, []),
        },
        "scenario_comparison": {
            sc: {"chi_mean": _chi(b, sc), "chi_std": b.get(f"chi_{sc}_std")}
            for sc in ["ssp126","ssp245","ssp585"]
        },
    }

@app.get("/api/v1/district/summary", tags=["data"])
def district_summary(
    scenario: str = Query("ssp245", pattern="^ssp(126|245|585)$")
):
    """District-level aggregated stats for all blocks."""
    rows = []
    for name, b in store.block_chi.items():
        chi = _chi(b, scenario)
        if chi is None: continue
        rows.append({"name": name, "chi": chi, "clpi": _clpi(chi, b)})

    chis = [r["chi"] for r in rows]
    if not chis:
        raise HTTPException(503, "No CHI data loaded")

    return {
        "district":     store.run_meta.get("district","South 24 Parganas"),
        "scenario":     scenario,
        "scenario_name":SCENARIO_NAMES[scenario],
        "n_blocks":     len(rows),
        "chi_mean":     round(sum(chis)/len(chis), 4),
        "chi_std":      round(math.sqrt(sum((c-sum(chis)/len(chis))**2 for c in chis)/len(chis)), 4),
        "critical_blocks": len([c for c in chis if c > 0.70]),
        "high_blocks":     len([c for c in chis if 0.55 < c <= 0.70]),
        "moderate_blocks": len([c for c in chis if 0.40 < c <= 0.55]),
        "low_blocks":      len([c for c in chis if c <= 0.40]),
        "top5_risk": sorted(rows, key=lambda r: r["chi"], reverse=True)[:5],
        "data_source": store.run_meta,
    }

@app.get("/api/v1/policy/clpi", tags=["policy"])
def clpi_ranking(
    scenario: str = Query("ssp245", pattern="^ssp(126|245|585)$"),
    limit: int    = Query(29, ge=1, le=50),
):
    """CLPI-ranked blocks for policymaker budget allocation."""
    rows = []
    for name, b in store.block_chi.items():
        chi = _chi(b, scenario)
        if chi is None: continue
        season = (store.block_meta.get(name,[{}])[0])
        clpi = _clpi(chi, b)
        tier = _risk_tier(chi)
        rows.append({
            "block_name": name,
            "chi_mean":   round(chi, 4),
            "clpi":       clpi,
            "risk_tier":  tier,
            "salinity":   season.get("salinity_level","—"),
            "livelihood": season.get("livelihood_type","—"),
            "budget_suggestion": (
                "₹80–100L" if tier=="critical" else
                "₹50–75L"  if tier=="high"     else
                "₹25–45L"  if tier=="moderate" else "₹10–20L"
            ),
            "priority_action": (
                "Embankment reinforcement + evacuation + early warning" if tier=="critical" else
                "Flood-resistant infra + drainage upgrade + insurance" if tier=="high" else
                "Capacity building + contingency seeds + soil health" else "Standard programme"
            ) if tier in ("critical","high","moderate") else "Standard development programme",
        })

    rows.sort(key=lambda r: r["clpi"], reverse=True)
    return {
        "district": store.run_meta.get("district","South 24 Parganas"),
        "scenario": scenario, "year": "2030",
        "roi_multiplier": 2.2,
        "clpi_formula": "0.40×wind + 0.30×CHI + 0.20×inundation + 0.10×(1-wealth)",
        "blocks": rows[:limit],
    }

@app.get("/api/v1/finance/racs", tags=["finance"])
def racs_portfolio(
    scenario: str = Query("ssp245", pattern="^ssp(126|245|585)$"),
):
    """RACS scores for entire portfolio — for NABARD/bank view."""
    rows = []
    for name, b in store.block_chi.items():
        chi = _chi(b, scenario)
        if chi is None: continue
        rows.append({"block_name": name, "chi": round(chi,4), **_racs(chi, b)})
    rows.sort(key=lambda r: r["chi"], reverse=True)
    avg_racs = round(sum(r["racs"] for r in rows)/len(rows), 1) if rows else None
    return {
        "scenario": scenario,
        "portfolio_blocks": len(rows),
        "avg_racs": avg_racs,
        "blocks": rows,
    }

@app.get("/api/v1/trend", tags=["data"])
def get_trend():
    """26-year CHI trend for all three scenarios (from chi_trend_*.npy)."""
    if not store.trend:
        raise HTTPException(503, "Trend data not loaded. Run export_for_web.py first.")
    return store.trend

@app.get("/api/v1/metadata/seasons/{block_name}", tags=["data"])
def get_seasons(block_name: str):
    """All sowing seasons for a block from block_metadata.csv."""
    seasons = store.block_meta.get(block_name)
    if not seasons:
        raise HTTPException(404, f"No season data for '{block_name}'")
    return {"block_name": block_name, "seasons": seasons}

# Feedback endpoint
class FeedbackIn(BaseModel):
    block_name: str
    event_type: str   # flood/drought/heatwave/cyclone/other
    event_date: str
    severity:   int   # 1-5
    description: Optional[str] = None
    reporter_type: str  # farmer/officer/ngo

@app.post("/api/v1/feedback", tags=["feedback"])
def submit_feedback(body: FeedbackIn):
    log_path = DATA_DIR / "feedback_log.jsonl"
    entry = {
        **body.dict(),
        "received_at": datetime.utcnow().isoformat()+"Z",
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"status": "received", "entry": entry}

# ─────────────────────────────────────────────────────────
# GEOGRAPHY — scalability hook
# For All-India expansion: add state/district/block hierarchy here
# ─────────────────────────────────────────────────────────
@app.get("/api/v1/geography", tags=["geography"])
def list_geography():
    """
    Geography hierarchy. Currently: West Bengal → South 24 Parganas → 29 blocks.
    For All-India expansion: populate from a states.json lookup file.
    """
    return {
        "states": [
            {
                "state": "West Bengal",
                "districts": [
                    {
                        "district": "South 24 Parganas",
                        "blocks": store.block_names,
                        "status": "live",
                        "data_available": True,
                    }
                ]
            }
        ],
        "note": "Additional states/districts can be added by uploading new block_summary.json and block_metadata.csv for each district."
    }
