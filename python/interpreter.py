import os
import re
import json
from typing import Dict, Any, Optional
from ctgov import CTGovClient
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())


class QueryInterpreter:
    """
    Interpret a natural-language clinical-trial query and return a
    structured visualization specification.

    Intent resolution order:
      1. OpenAI (GPT-4o) — if OPENAI_API_KEY is set
      2. Keyword heuristics — fast fallback, always available

    Supported intents → visualization types:
      time_series          → line chart (trials per year)
      phase_breakdown      → bar chart  (trials by phase)
      network_graph        → force-directed graph (sponsor–condition)
      top_sponsors         → horizontal bar chart
      enrollment_histogram → histogram (enrollment bucket counts)
      status_breakdown     → horizontal bar chart (trial status)
    """

    INTENTS = [
        "time_series", "phase_breakdown", "network_graph",
        "top_sponsors", "enrollment_histogram", "status_breakdown",
    ]

    def __init__(self, ctgov_client: CTGovClient):
        self.ct = ctgov_client
        self.openai_key = os.environ.get("OPENAI_API_KEY")
        self.openai_model = os.environ.get("OPENAI_MODEL", "gpt-4o")
        self._oai_client = None
        if self.openai_key:
            try:
                from openai import OpenAI
                self._oai_client = OpenAI(api_key=self.openai_key)
            except Exception:
                self._oai_client = None

    # ── LLM intent parsing ────────────────────────────────────────────────────

    def _call_llm(self, original_query: str,
                  req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call OpenAI to extract intent + slots. Returns dict or None on failure."""
        if not self._oai_client:
            return None

        provided = {k: req.get(k) for k in
                    ["drug_name", "condition", "start_year", "end_year",
                     "sponsor", "country", "trial_phase"]
                    if req.get(k)}

        system_msg = (
            "You are a JSON extraction assistant for clinical trial queries.\n"
            "Return ONLY a JSON object — no markdown, no explanation — with:\n"
            "  intent: one of [time_series, phase_breakdown, network_graph, "
            "top_sponsors, enrollment_histogram, status_breakdown]\n"
            "  slots: { drug_name?, condition?, sponsor?, start_year?, end_year?, "
            "country?, trial_phase? }\n\n"
            "Intent mapping:\n"
            "  time_series          – trends over time, yearly counts, how has X changed\n"
            "  phase_breakdown      – phase distribution, trials by phase\n"
            "  network_graph        – relationships, connections, sponsor-condition network\n"
            "  top_sponsors         – who sponsors, leading organizations, most active\n"
            "  enrollment_histogram – enrollment sizes, patient counts, sample size distribution\n"
            "  status_breakdown     – trial status, recruiting vs completed, active trials"
        )

        user_msg = (
            f"Question: {original_query}\n"
            f"Already-provided fields: {json.dumps(provided)}"
        )

        try:
            resp = self._oai_client.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=300,
                temperature=0.0,
            )
            text = resp.choices[0].message.content.strip()
            # Strip markdown code fences if model added them
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                return json.loads(m.group(0))
        except Exception as exc:
            return {"_error": str(exc)}
        return None

    # ── Heuristic fallback ────────────────────────────────────────────────────

    def _heuristic_intent(self, query: str) -> str:
        q = query.lower()
        if any(w in q for w in ["over time", "per year", "trend", "timeline",
                                 "yearly", "change", "history", "growth"]):
            return "time_series"
        if any(w in q for w in ["by phase", "phase breakdown", "phase distribution",
                                 "which phase", "phase of"]):
            return "phase_breakdown"
        if any(w in q for w in ["network", "relationship", "connection",
                                 "collaboration", "linked to"]):
            return "network_graph"
        if any(w in q for w in ["top sponsor", "who sponsor", "leading",
                                 "most active", "fund", "who runs", "who is running"]):
            return "top_sponsors"
        if any(w in q for w in ["enrollment", "patient", "sample size",
                                 "how many patients", "distribution of", "participants"]):
            return "enrollment_histogram"
        if any(w in q for w in ["status", "recruiting", "completed",
                                 "active", "terminated", "withdrawn"]):
            return "status_breakdown"
        # looser fallbacks
        if "phase" in q:
            return "phase_breakdown"
        if "sponsor" in q:
            return "top_sponsors"
        return "time_series"

    # ── Main entry point ──────────────────────────────────────────────────────

    def handle_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        original_query = req.get("query", "")
        drug = req.get("drug_name")
        condition = req.get("condition")
        sponsor = req.get("sponsor")
        start_year = req.get("start_year") or 2000
        end_year = req.get("end_year") or 2024

        # 1. Try LLM
        llm_used = False
        llm_error = None
        parsed = self._call_llm(original_query, req)

        if parsed and "_error" in parsed:
            llm_error = parsed["_error"]
            parsed = None

        _YEAR_MIN, _YEAR_MAX = 1990, 2025

        if parsed and isinstance(parsed, dict):
            raw_intent = parsed.get("intent") or ""
            if raw_intent in self.INTENTS:
                llm_used = True
                intent = raw_intent
            else:
                # LLM returned an unknown intent — fall back silently
                llm_error = (llm_error or
                             f"LLM returned unknown intent '{raw_intent}'; fell back to heuristics")
                intent = self._heuristic_intent(original_query)

            slots = parsed.get("slots") or {}
            drug = slots.get("drug_name") or drug
            condition = slots.get("condition") or condition
            sponsor = slots.get("sponsor") or sponsor
            if slots.get("start_year"):
                start_year = max(_YEAR_MIN, min(int(slots["start_year"]), _YEAR_MAX))
            if slots.get("end_year"):
                end_year = max(_YEAR_MIN, min(int(slots["end_year"]), _YEAR_MAX))
        else:
            intent = self._heuristic_intent(original_query)

        # 2. Build ClinicalTrials.gov search expression
        parts = [p for p in [drug, condition, sponsor] if p]
        base_expr = " AND ".join(parts) if parts else None

        # 3. Shared metadata added to every response
        meta_base = {
            "source": "clinicaltrials.gov",
            "llm_used": llm_used,
            "llm_model": self.openai_model if llm_used else None,
            "llm_error": llm_error,
            "parsed_intent": intent,
            "filters": {k: v for k, v in {
                "drug_name": drug,
                "condition": condition,
                "sponsor": sponsor,
            }.items() if v},
        }

        # 4. Dispatch to handler
        if intent == "phase_breakdown":
            result = self._phase_breakdown(base_expr, drug, condition, meta_base)
        elif intent == "network_graph":
            result = self._network_graph(base_expr, drug, condition, meta_base)
        elif intent == "top_sponsors":
            result = self._top_sponsors(base_expr, condition, drug, meta_base)
        elif intent == "enrollment_histogram":
            result = self._enrollment_histogram(base_expr, condition, drug, meta_base)
        elif intent == "status_breakdown":
            result = self._status_breakdown(base_expr, condition, drug, meta_base)
        else:
            result = self._time_series(base_expr, drug, condition, start_year, end_year, meta_base)

        # 5. Detect empty results and surface a user-facing reason
        data = result.get("visualization", {}).get("data")
        is_empty = (
            data is None
            or (isinstance(data, list) and len(data) == 0)
            or (isinstance(data, dict) and not data.get("nodes"))
        )
        if is_empty:
            result["meta"]["empty_result"] = True
            result["meta"]["empty_reason"] = (
                "No studies found for your filters. "
                "Try broadening your search terms or removing a filter."
            )

        return result

    # ── Visualization builders ────────────────────────────────────────────────

    def _time_series(self, expr, drug, condition, start_year, end_year, meta):
        data = self.ct.count_by_year(expr, start_year=start_year, end_year=end_year)
        label = self._label(drug, condition)
        return {
            "visualization": {
                "type": "time_series",
                "title": f"Trials over time{label}",
                "encoding": {
                    "x": {"field": "year", "type": "quantitative", "title": "Year"},
                    "y": {"field": "count", "type": "quantitative", "title": "Number of Trials"},
                },
                "data": data,
            },
            "meta": {**meta, "time_granularity": "year",
                     "filters": {**meta["filters"],
                                  "start_year": start_year, "end_year": end_year}},
        }

    def _phase_breakdown(self, expr, drug, condition, meta):
        counts = self.ct.studies_by_phase(expr)
        data = sorted(
            [{"phase": k, "trial_count": v} for k, v in counts.items()],
            key=lambda x: x["trial_count"], reverse=True,
        )
        label = self._label(drug, condition)
        return {
            "visualization": {
                "type": "bar_chart",
                "title": f"Trials by Phase{label}",
                "encoding": {
                    "x": {"field": "phase", "type": "nominal", "title": "Phase"},
                    "y": {"field": "trial_count", "type": "quantitative",
                          "title": "Number of Trials"},
                },
                "data": data,
            },
            "meta": meta,
        }

    def _network_graph(self, expr, drug, condition, meta):
        net = self.ct.sponsor_condition_network(expr, max_studies=300)
        label = self._label(drug, condition)
        return {
            "visualization": {
                "type": "network_graph",
                "title": f"Sponsor–Condition Network{label}",
                "encoding": {
                    "node_id": "id", "node_label": "label", "node_type": "type",
                    "edge_source": "source", "edge_target": "target", "edge_weight": "weight",
                },
                "data": net,
            },
            "meta": {**meta,
                     "note": "Blue nodes = sponsors, green nodes = conditions. "
                             "Edge weight = number of shared studies."},
        }

    def _top_sponsors(self, expr, condition, drug, meta):
        sponsors = self.ct.top_sponsors(expr, n=20)
        label = self._label(drug, condition)
        return {
            "visualization": {
                "type": "bar_chart",
                "title": f"Top Sponsors{label}",
                "encoding": {
                    "x": {"field": "count", "type": "quantitative",
                          "title": "Number of Trials"},
                    "y": {"field": "sponsor", "type": "nominal",
                          "title": "Sponsor", "sort": "-x"},
                },
                "data": sponsors,
            },
            "meta": meta,
        }

    def _enrollment_histogram(self, expr, condition, drug, meta):
        data = self.ct.enrollment_distribution(expr)
        label = self._label(drug, condition)
        return {
            "visualization": {
                "type": "histogram",
                "title": f"Enrollment Size Distribution{label}",
                "encoding": {
                    "x": {"field": "bucket", "type": "ordinal",
                          "title": "Enrollment Count Range"},
                    "y": {"field": "count", "type": "quantitative",
                          "title": "Number of Trials"},
                },
                "data": data,
            },
            "meta": {**meta, "unit": "participants per trial"},
        }

    def _status_breakdown(self, expr, condition, drug, meta):
        counts = self.ct.status_breakdown(expr)
        data = sorted(
            [{"status": k, "count": v} for k, v in counts.items()],
            key=lambda x: x["count"], reverse=True,
        )
        label = self._label(drug, condition)
        return {
            "visualization": {
                "type": "bar_chart",
                "title": f"Trial Status Breakdown{label}",
                "encoding": {
                    "x": {"field": "count", "type": "quantitative",
                          "title": "Number of Trials"},
                    "y": {"field": "status", "type": "nominal",
                          "title": "Status", "sort": "-x"},
                },
                "data": data,
            },
            "meta": meta,
        }

    @staticmethod
    def _label(drug: Optional[str], condition: Optional[str]) -> str:
        parts = []
        if drug:
            parts.append(drug)
        if condition:
            parts.append(condition)
        return (" — " + " · ".join(parts)) if parts else ""
