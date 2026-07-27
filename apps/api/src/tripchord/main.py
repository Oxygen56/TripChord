from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tripchord import __version__
from tripchord.api import VerifyRequest, VerifyResponse, verify_plan
from tripchord.config import get_settings

settings = get_settings()

app = FastAPI(
    title="TripChord API",
    version=__version__,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "tripchord", "version": __version__}


@app.post("/api/v1/plans/verify", response_model=VerifyResponse)
async def verify_endpoint(request: VerifyRequest) -> VerifyResponse:
    return verify_plan(request)
