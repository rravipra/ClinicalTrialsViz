import requests
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional

BASE = "https://clinicaltrials.gov/api/v2"


class CTGovClient:
    """Wrapper around ClinicalTrials.gov Data API v2."""

    def __init__(self):
        self.base = BASE

    def search_studies(self, term: str = None, fields: Optional[str] = None,
                       page_size: int = 100, page_token: Optional[str] = None) -> Dict[str, Any]:
        params = {"format": "json", "pageSize": page_size}
        if term:
            params["query.term"] = term
        if fields:
            params["fields"] = fields
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(f"{self.base}/studies", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def count_by_year(self, term: str = None, start_year: int = 2000,
                      end_year: int = 2025) -> List[Dict[str, Any]]:
        """Fetch yearly trial counts in parallel (8 workers) for speed."""
        years = list(range(start_year, end_year + 1))

        def fetch_one(y: int):
            params = {"format": "json", "pageSize": 1,
                      "filter.advanced": f"AREA[StartDate]RANGE[{y}-01-01,{y}-12-31]"}
            if term:
                params["query.term"] = term
            try:
                r = requests.get(f"{self.base}/studies", params=params, timeout=20)
                r.raise_for_status()
                data = r.json()
                total = data.get("totalCount")
                count = int(total) if total is not None else len(data.get("studies", []))
                return y, count
            except Exception:
                return y, 0

        results: Dict[int, int] = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            for y, count in ex.map(fetch_one, years):
                results[y] = count

        return [{"year": y, "count": results.get(y, 0)} for y in years]

    def studies_by_phase(self, term: str = None, max_studies: int = 1000) -> Dict[str, int]:
        """Count studies by trial phase (correctly parses v2 nested structure)."""
        counts: Dict[str, int] = {}
        fetched = 0
        page_token = None

        while True:
            data = self.search_studies(term, page_size=200, page_token=page_token)
            for s in data.get("studies", []):
                try:
                    phases = (s.get("protocolSection", {})
                               .get("designModule", {})
                               .get("phases", []))
                    phase_str = " / ".join(phases) if phases else "N/A"
                except Exception:
                    phase_str = "Unknown"
                counts[phase_str] = counts.get(phase_str, 0) + 1
                fetched += 1
                if fetched >= max_studies:
                    return counts
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return counts

    def top_sponsors(self, term: str = None, n: int = 20) -> List[Dict[str, Any]]:
        """Return top N lead sponsors by study count."""
        counts: Dict[str, int] = {}
        fetched = 0
        page_token = None

        while fetched < 1000:
            data = self.search_studies(term, page_size=200, page_token=page_token)
            for s in data.get("studies", []):
                try:
                    sponsor = (s.get("protocolSection", {})
                                .get("sponsorCollaboratorsModule", {})
                                .get("leadSponsor", {})
                                .get("name", "Unknown"))
                except Exception:
                    sponsor = "Unknown"
                counts[sponsor] = counts.get(sponsor, 0) + 1
                fetched += 1
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:n]
        return [{"sponsor": sp, "count": ct} for sp, ct in top]

    def enrollment_distribution(self, term: str = None,
                                max_studies: int = 500) -> List[Dict[str, Any]]:
        """Return pre-binned enrollment counts for histogram rendering."""
        enrollments: List[int] = []
        data = self.search_studies(term, page_size=min(max_studies, 1000))
        for s in data.get("studies", []):
            try:
                count = (s.get("protocolSection", {})
                          .get("designModule", {})
                          .get("enrollmentInfo", {})
                          .get("count"))
                if count and isinstance(count, (int, float)) and count > 0:
                    enrollments.append(int(count))
            except Exception:
                pass

        if not enrollments:
            return []

        bins = [(1, 10), (11, 50), (51, 100), (101, 250), (251, 500),
                (501, 1000), (1001, 5000), (5001, 10**9)]
        labels = ["1–10", "11–50", "51–100", "101–250", "251–500",
                  "501–1k", "1k–5k", "5k+"]
        bin_counts = [0] * len(bins)
        for e in enrollments:
            for i, (lo, hi) in enumerate(bins):
                if lo <= e <= hi:
                    bin_counts[i] += 1
                    break

        return [{"bucket": lbl, "count": ct}
                for lbl, ct in zip(labels, bin_counts) if ct > 0]

    def status_breakdown(self, term: str = None) -> Dict[str, int]:
        """Count studies by overall status."""
        counts: Dict[str, int] = {}
        data = self.search_studies(term, page_size=1000)
        for s in data.get("studies", []):
            try:
                status = (s.get("protocolSection", {})
                           .get("statusModule", {})
                           .get("overallStatus", "Unknown"))
            except Exception:
                status = "Unknown"
            counts[status] = counts.get(status, 0) + 1
        return counts

    def sponsor_condition_network(self, term: str = None,
                                  max_studies: int = 400) -> Dict[str, Any]:
        """Build sponsor–condition–intervention tripartite network from search results."""
        nodes: Dict[str, Dict] = {}
        edges: Dict[str, Dict] = {}
        fetched = 0
        page_token = None

        while fetched < max_studies:
            batch = min(200, max_studies - fetched)
            data = self.search_studies(term, page_size=batch, page_token=page_token)
            for s in data.get("studies", []):
                try:
                    ps = s.get("protocolSection", {})
                    sponsor = (ps.get("sponsorCollaboratorsModule", {})
                                 .get("leadSponsor", {})
                                 .get("name", "Unknown Sponsor"))
                    conditions = ps.get("conditionsModule", {}).get("conditions", [])
                    interventions = [
                        i.get("name", "") for i in
                        ps.get("armsInterventionsModule", {}).get("interventions", [])[:3]
                        if i.get("name")
                    ]
                except Exception:
                    continue

                sp_id = "s:" + sponsor
                nodes[sp_id] = {"id": sp_id, "label": sponsor, "type": "sponsor"}

                for cond in conditions:
                    c_id = "c:" + cond
                    nodes[c_id] = {"id": c_id, "label": cond, "type": "condition"}
                    key = sp_id + "||" + c_id
                    if key not in edges:
                        edges[key] = {"source": sp_id, "target": c_id, "weight": 0}
                    edges[key]["weight"] += 1

                for interv in interventions:
                    d_id = "d:" + interv
                    nodes[d_id] = {"id": d_id, "label": interv, "type": "drug"}
                    key = sp_id + "||" + d_id
                    if key not in edges:
                        edges[key] = {"source": sp_id, "target": d_id, "weight": 0}
                    edges[key]["weight"] += 1

                fetched += 1

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return {"nodes": list(nodes.values()), "edges": list(edges.values())}

    def get_field_values(self, field: str, term: str = None) -> List[Dict[str, Any]]:
        params = {"format": "json"}
        if term:
            params["query.term"] = term
        r = requests.get(f"{self.base}/stats/fieldValues/{field}", params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("fieldValues") or []
