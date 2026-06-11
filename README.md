ClinicalTrials.gov Visualization API
=====================================

A FastAPI service that accepts natural-language clinical-trial questions, resolves them to a structured visualization specification, and returns data fetched live from the ClinicalTrials.gov v2 API. A single-page frontend renders bar charts, line charts, histograms, and force-directed network graphs.

---

1. How to Run
-------------

### Prerequisites
- Python 3.9+
- An OpenAI API key (optional — keyword heuristics work without one)

### Install

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

Create `python/.env` (copy from the root `.env.example`):

```
OPENAI_API_KEY=sk-...        # optional; enables GPT-4o intent parsing
OPENAI_MODEL=gpt-4o          # optional; defaults to gpt-4o
PORT=8000
```

If `OPENAI_API_KEY` is not set the system still works — intent is detected by keyword heuristics and every response will show `"llm_used": false`.

### Start

```bash
cd python
source .venv/bin/activate
uvicorn app:app --port 8000
```

Open `http://localhost:8000` in a browser. The frontend is served from the same process.

To test via `curl`:

```bash
curl -s -X POST http://localhost:8000/visualize \
  -H "Content-Type: application/json" \
  -d '{"query": "show trials by phase for pembrolizumab", "drug_name": "Pembrolizumab"}' \
  | python3 -m json.tool
```

---

2. Request / Response Schema
-----------------------------

### POST /visualize — Request

```json
{
  "query":       "string  (required) — natural language question",
  "drug_name":   "string  (optional) — drug / intervention name",
  "condition":   "string  (optional) — disease or condition",
  "sponsor":     "string  (optional) — lead sponsor organization",
  "trial_phase": "string  (optional) — e.g. PHASE2, PHASE3",
  "country":     "string  (optional) — 2-letter ISO country code",
  "start_year":  "integer (optional) — earliest trial start year",
  "end_year":    "integer (optional) — latest trial start year",
  "extra":       "object  (optional) — pass-through key/value pairs"
}
```

All fields except `query` are optional. Structured fields (drug_name, condition, etc.) are combined with `AND` when building the ClinicalTrials.gov search expression. The LLM can also extract these slots from the free-text `query` if they are not provided explicitly.

### POST /visualize — Response

```json
{
  "visualization": {
    "type":     "time_series | bar_chart | histogram | network_graph",
    "title":    "string — human-readable chart title",
    "encoding": { ... },
    "data":     [ ... ] | { "nodes": [...], "edges": [...] }
  },
  "meta": {
    "source":       "clinicaltrials.gov",
    "llm_used":     true | false,
    "llm_model":    "gpt-4o" | null,
    "parsed_intent":"time_series | phase_breakdown | network_graph | top_sponsors | enrollment_histogram | status_breakdown",
    "filters":      { ... },
    "time_granularity": "year"   // only present for time_series
  }
}
```

### Encoding by visualization type

**time_series**
```json
"encoding": {
  "x": { "field": "year",  "type": "quantitative", "title": "Year" },
  "y": { "field": "count", "type": "quantitative", "title": "Number of Trials" }
}
```
`data` is an array of `{ "year": int, "count": int }`.

**bar_chart** (phase_breakdown, top_sponsors, status_breakdown)
```json
"encoding": {
  "x": { "field": "phase | sponsor | status", "type": "nominal | quantitative", "title": "..." },
  "y": { "field": "trial_count | count",       "type": "quantitative | nominal",  "title": "..." }
}
```
Vertical bar (x=nominal, y=quantitative) for phase breakdown; horizontal bar (x=quantitative, y=nominal) for sponsors and status.

**histogram** (enrollment_histogram)
```json
"encoding": {
  "x": { "field": "bucket", "type": "ordinal",      "title": "Enrollment Count Range" },
  "y": { "field": "count",  "type": "quantitative", "title": "Number of Trials" }
}
```
`data` is an array of `{ "bucket": "51–100", "count": int }`.

**network_graph**
```json
"encoding": {
  "node_id": "id", "node_label": "label", "node_type": "type",
  "edge_source": "source", "edge_target": "target", "edge_weight": "weight"
}
```
`data` is `{ "nodes": [{ "id", "label", "type" }], "edges": [{ "source", "target", "weight" }] }`.

---

3. Key Design Decisions and Tradeoffs
---------------------------------------

**Two-stage intent resolution (LLM → heuristic fallback)**
GPT-4o is called first when an API key is available; keyword heuristics are always available as a fallback. This keeps the service functional with zero external dependencies while offering richer parsing when the key is present. Tradeoff: silent fallback means it is not obvious from the response *why* the LLM was skipped — only `"llm_used": false` signals this.

**Live data, no database**
Every request hits ClinicalTrials.gov directly. Data is always current and there is nothing to deploy or keep in sync. Tradeoff: latency of 2–8 seconds per request; no caching means identical queries pay the full round-trip every time.

**Parallelised year fetches**
`count_by_year` fires one HTTP request per year (up to 26) concurrently via `ThreadPoolExecutor(max_workers=8)`. This cuts wall-clock time from ~52 s to ~7 s. Tradeoff: 8 simultaneous outbound connections to clinicaltrials.gov — acceptable for a single user, could trigger rate limiting at scale.

**Tripartite network: sponsor ↔ condition and sponsor ↔ intervention**
The network graph links lead sponsors to both conditions and drug/intervention nodes (up to 3 interventions per study). Node type is encoded in colour (blue = sponsor, green = condition, orange = drug). Capped at 3 interventions per study to prevent graph explosion from multi-arm trials. Tradeoff: condition–drug direct edges are not drawn (only sponsor is the hub), which avoids a dense hairball but means you can't directly read "Osimertinib treats NSCLC" from the graph alone.

**Pre-binned histogram**
Enrollment distribution is binned server-side into 8 log-scale buckets before returning. This keeps the response small (8 rows instead of thousands). Tradeoff: the bin boundaries are fixed — a user cannot re-bin interactively in the browser.

**Single-file frontend**
The entire UI is one HTML file (index.html) using CDN-hosted Vega-Lite and D3. No build step, no Node.js toolchain. Tradeoff: adding a new chart type requires editing a large monolithic file; state management is manual DOM manipulation.

**Pydantic v1 + FastAPI 0.95**
Kept at these versions for Python 3.9 compatibility (`.dict()` vs `.model_dump()` API). Tradeoff: both are now superseded; upgrading requires replacing `.dict()` calls throughout.

---

4. Limitations and What I Would Improve
-----------------------------------------

**Current limitations**

- Study counts are capped at 1000 for most endpoints. Broad queries (e.g. condition = "Cancer") are analysed against a sample, not the full dataset.
- No caching — repeated identical queries always re-fetch from the API.
- LLM errors are surfaced in `meta.llm_error` and shown in the UI banner, but the system always falls back to heuristics rather than failing hard.
- The year range for `time_series` defaults to 2000–2024 regardless of the query; asking "over the last 5 years" does not adjust the window unless the user fills `start_year`.
- The network graph has no client-side size limit: a broad query can produce hundreds of nodes and make D3's force simulation unstable.
- CORS is open (`allow_origins=["*"]`), which is fine for local development but not production.

**What I would improve with more time**

1. **Caching layer** — Redis or a simple in-memory TTL cache keyed on (intent, filters). Would eliminate redundant API calls and cut median latency dramatically.
2. **Full pagination** — Iterate all pages from ClinicalTrials.gov instead of capping at 1000; use `totalCount` to report coverage honestly.
3. **Better LLM error visibility** — Log the exception and surface a `"llm_error"` field in `meta` so operators can distinguish "key not set" from "API call failed".
4. **Graph size controls** — Cap network nodes by frequency before returning and expose a `max_nodes` parameter so the caller can tune the graph.
5. **Unit and integration tests** — Mock the ClinicalTrials.gov API for unit tests; add a real-API smoke test suite.
6. **Streaming / progressive loading** — Stream partial results for the time-series endpoint so the UI can render as years arrive rather than waiting for all 26 requests to complete.

---

5. Example Runs
----------------

Five example queries with the actual JSON output produced by the system. All outputs are in `python/examples/`.

| # | File | Query | Intent | Viz Type |
|---|------|-------|--------|----------|
| 1 | `example1_pembrolizumab_timeseries.json` | "How has the number of Pembrolizumab trials changed over time?" | time_series | Line chart |
| 2 | `example2_pembrolizumab_by_phase.json` | "Show trials by phase for Pembrolizumab" | phase_breakdown | Bar chart |
| 3 | `example3_lung_network.json` | "Build a sponsor-condition network for lung cancer" | network_graph | Force-directed graph |
| 4 | `example4_cancer_top_sponsors.json` | "Which organizations sponsor the most cancer trials?" | top_sponsors | Horizontal bar chart |
| 5 | `example5_diabetes_status.json` | "What is the current status of diabetes clinical trials?" | status_breakdown | Horizontal bar chart |

See each file for the full `request` and `response` JSON.

6. UI

-------------

<img width="3420" height="1868" alt="image" src="https://github.com/user-attachments/assets/f9b298a1-ff69-4e2b-afef-659473958967" />

