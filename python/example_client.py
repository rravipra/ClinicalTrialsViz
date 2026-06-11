import requests
import json

URL = "http://localhost:8000/visualize"

examples = [
    {"query": "how has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab", "start_year": 2010, "end_year": 2023},
    {"query": "show trials by phase for pembrolizumab", "drug_name": "Pembrolizumab"},
    {"query": "build a sponsor-condition network for lung cancer", "condition": "Lung Cancer"}
]

for i, ex in enumerate(examples, 1):
    r = requests.post(URL, json=ex)
    print(f"Example {i} response:")
    print(json.dumps(r.json(), indent=2))
