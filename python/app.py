from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from ctgov import CTGovClient
from interpreter import QueryInterpreter

app = FastAPI(title="ClinicalTrials.gov Visualization API")

# Allow CORS for local development and the frontend served from this app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the simple frontend (see python/frontend/index.html)
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
def read_index():
    return FileResponse("frontend/index.html")

client = CTGovClient()
interpreter = QueryInterpreter(client)


class QueryRequest(BaseModel):
    query: str = Field(..., description="Natural language clinical-trial question")
    drug_name: Optional[str] = None
    condition: Optional[str] = None
    trial_phase: Optional[str] = None
    sponsor: Optional[str] = None
    country: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None
    extra: Optional[Dict[str, Any]] = None


@app.post("/visualize")
async def visualize(req: QueryRequest):
    try:
        spec = interpreter.handle_request(req.dict())
        return spec
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
