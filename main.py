from fastapi import FastAPI

app = FastAPI(
    title="REMAKE AI",
    description="REMAKE AI Video Processing API",
    version="1.0.0"
)

@app.get("/")
def home():
    return {
        "name": "REMAKE AI",
        "status": "online",
        "message": "Backend is running!"
    }

@app.get("/health")
def health():
    return {
        "status": "healthy"
    }
