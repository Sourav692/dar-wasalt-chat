from fastapi import FastAPI

app = FastAPI(title="Dar Global & Wasalt Assistant", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
