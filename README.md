# Progyan — DiCRA Climate Sensor  
### Deployment Guide: GitHub → Render (backend) + Vercel (frontend)

---

## Repository Structure

```
progyan/
├── main.py                    # FastAPI backend (fixed + enhanced)
├── export_for_web.py          # Data pipeline: CSV/npy → JSON
├── requirements.txt           # Pinned Python deps
├── build.sh                   # Render build script
├── render.yaml                # Render service config
├── vercel.json                # Vercel frontend config
├── .gitignore
│
├── static/
│   └── index.html             # Frontend dashboard (served by Render + Vercel)
│
├── data/                      # Generated at build time (also pre-committed)
│   ├── block_summary.json     # Per-block CHI stats (all 3 scenarios)
│   ├── trend_data.json        # 26-year trend data
│   ├── metadata.json          # Provenance
│   └── block_metadata.csv     # 🔴 YOU MUST POPULATE THIS (see below)
│
├── chi_tiles_2030_ssp126.csv  # Raw tile data (checked in)
├── chi_tiles_2030_ssp245.csv
├── chi_tiles_2030_ssp585.csv
├── chi_trend_ssp126.npy
├── chi_trend_ssp245.npy
└── chi_trend_ssp585.npy
```

---

## 🔴 Before You Deploy: Populate block_metadata.csv

The advisory engine is entirely driven by `data/block_metadata.csv`. Replace the sample file with your real data:

```
block_name  typical_sowing_start  typical_sowing_end  cropping_pattern  salinity_level  livelihood_type
Block_71    June                  July                Kharif Paddy (Aman)+Rabi Mustard  High  Agriculture+Pisciculture
Block_72    June                  July                Kharif Paddy (Aman)+Vegetables   Moderate  Agriculture
...
```

- `salinity_level`: `Low` / `Moderate` / `High`
- `livelihood_type`: e.g. `Agriculture`, `Agriculture+Pisciculture`, `Agriculture+Aquaculture`
- Month names must match exactly: `January`, `February`, … `December`

---

## Step 1: Push to GitHub

```bash
git init
git add .
git commit -m "feat: initial Progyan deployment"
git remote add origin https://github.com/YOUR_USERNAME/progyan.git
git push -u origin main
```

> ⚠️ The chi_tiles CSV files are ~3 MB each. GitHub has a 100 MB file limit — these are fine.

---

## Step 2: Deploy Backend to Render

1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your GitHub repo
3. Render auto-detects `render.yaml` — review and confirm
4. Set environment variables if needed:
   - `DATA_PATH` = `./data` (already in render.yaml)
5. Click **Deploy**

The build runs `bash build.sh` which:
- Installs dependencies
- Runs `export_for_web.py` to regenerate the JSON data files

Once deployed, your backend will be live at:
```
https://progyan-backend.onrender.com
```

Verify:
```
GET https://progyan-backend.onrender.com/api/v1/health
GET https://progyan-backend.onrender.com/docs
```

---

## Step 3: Update Frontend API URL

Edit `static/index.html` line ~603:

```javascript
const API = (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
  ? 'http://localhost:8000'
  : 'https://progyan-backend.onrender.com';   // ← your actual Render URL
```

---

## Step 4: Deploy Frontend to Vercel

```bash
npm i -g vercel
vercel --prod
```

Or:
1. Go to [vercel.com](https://vercel.com) → **Import Git Repository**
2. Point to the same GitHub repo
3. Vercel will detect `vercel.json` and serve `static/index.html`

Your frontend will be live at:
```
https://progyan.vercel.app   (or your custom domain)
```

---

## Step 5: Tighten CORS (Production)

In `main.py`, update:
```python
allow_origins=["https://progyan.vercel.app"],  # your Vercel URL
```

Then redeploy Render.

---

## Local Development

```bash
# Generate data JSON files
python export_for_web.py

# Start backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Open dashboard
open http://localhost:8000
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/health` | Health check + data status |
| GET | `/api/v1/blocks` | All block names |
| GET | `/api/v1/blocks/{name}?scenario=ssp245` | Full block data + advisory |
| GET | `/api/v1/district/summary?scenario=ssp245` | District aggregation |
| GET | `/api/v1/policy/clpi?scenario=ssp245` | CLPI-ranked blocks for policy |
| GET | `/api/v1/finance/racs?scenario=ssp245` | RACS portfolio for NABARD |
| GET | `/api/v1/trend` | 26-year CHI trend (all scenarios) |
| GET | `/api/v1/metadata/seasons/{name}` | Sowing seasons for a block |
| POST | `/api/v1/feedback` | Submit farmer field observation |
| GET | `/docs` | OpenAPI interactive docs |

---

## Bugs Fixed in This Version

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `main.py` | `SyntaxError` line 494: double `else` in ternary expression | Rewrote `priority_action` conditional |
| 2 | `render.yaml` | `PYTHON_VERSION: 3.14` (non-existent) | Changed to `3.11` |
| 3 | `main.py` | Missing `StaticFiles` / `FileResponse` imports | Added imports + static mount |
| 4 | `main.py` | Root `/` conflict between JSON and HTML responses | Moved JSON root to `/api/v1/info` |
| 5 | `process_data.py` | Produced only flat JSON (not `block_summary.json`) | Replaced with `export_for_web.py` |
| 6 | `requirements.txt` | Missing `numpy` and `pandas` | Added with pinned versions |
| 7 | `export_for_web.py` | Did not exist | Created from scratch |
| 8 | `main.py` | `data/block_summary.json` never generated | Now produced by `export_for_web.py` |

---

## Scientific Notes

- **CHI range**: 0.29 (ocean/min) to 0.79 (max in SSP5-8.5)
- **Trend arrays** span 2005–2030 (26 values), district-level spatial mean
- **"Unknown" blocks** (ocean tiles) are filtered out in `export_for_web.py`
- **1872 named blocks** loaded after filtering
- Block IDs (`Block_XXX`) map to your `block_metadata.csv` entries via `block_name`
