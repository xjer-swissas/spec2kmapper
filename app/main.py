from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import init_master_tables
from app.amos.scheduler import start_scheduler
from app.amos.central import receiver
from app.matching.catalog import start_indexes
from app.routers import auth_routes, amos_routes, spec2500_routes, matching_routes
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_master_tables()
    start_scheduler()
    os.makedirs("uploads", exist_ok=True)
    start_indexes()
    receiver.start()
    try:
        yield
    finally:
        receiver.stop()
        from app.amos.scheduler import scheduler
        if scheduler.running:
            scheduler.shutdown(wait=False)

app = FastAPI(title="AMOS Spec2500 Transformer Suite", version="2.0.0", lifespan=lifespan)

@app.middleware("http")
async def revalidate_frontend(request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/index.html", "/app.js"):
        response.headers["Cache-Control"] = "no-cache"
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(amos_routes.router)
app.include_router(spec2500_routes.router)
app.include_router(matching_routes.router)

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
