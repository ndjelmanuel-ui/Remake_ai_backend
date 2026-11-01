import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 500 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}

app = FastAPI(
    title="REMAKE AI",
    description="REMAKE AI video processing backend",
    version="1.1.0"
)

# Pour le prototype. En production, remplacer "*" par le domaine exact du site.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")


def run_command(command: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Le traitement vidéo a dépassé le délai autorisé.")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "Erreur FFmpeg")[-2500:]
        raise HTTPException(status_code=500, detail=f"Erreur du moteur vidéo : {detail}")


def get_duration(path: Path) -> float:
    result = run_command([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ])
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Impossible de lire la durée de cette vidéo.")


def find_keep_segments(path: Path, duration: float) -> list[tuple[float, float]]:
    """Détecte les silences audio et renvoie les intervalles à conserver."""
    result = run_command([
        "ffmpeg", "-hide_banner", "-i", str(path),
        "-vn", "-af", "silencedetect=noise=-35dB:d=0.65",
        "-f", "null", "-"
    ], timeout=900)

    log = result.stderr
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log)]

    silences = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else duration
        silences.append((max(0.0, start), min(duration, end)))

    keep = []
    cursor = 0.0
    for start, end in silences:
        if start - cursor >= 0.35:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= 0.35:
        keep.append((cursor, duration))

    # Évite un résultat vide si la vidéo ne contient que peu de son.
    return keep if keep else [(0.0, duration)]


def make_silence_cut(input_path: Path, output_path: Path, segments: list[tuple[float, float]]) -> None:
    if len(segments) == 1 and segments[0][0] == 0 and segments[0][1] > 0:
        run_command([
            "ffmpeg", "-y", "-i", str(input_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            str(output_path)
        ])
        return

    filters = []
    labels = []
    for i, (start, end) in enumerate(segments):
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        labels.append(f"[v{i}][a{i}]")

    filters.append("".join(labels) + f"concat=n={len(segments)}:v=1:a=1[outv][outa]")

    run_command([
        "ffmpeg", "-y", "-i", str(input_path),
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        str(output_path)
    ])


@app.get("/")
def home():
    return {
        "name": "REMAKE AI",
        "status": "online",
        "message": "Backend is running!"
    }


@app.get("/health")
def health():
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ffprobe_ok = shutil.which("ffprobe") is not None
    return {
        "status": "healthy" if ffmpeg_ok and ffprobe_ok else "missing_video_tools",
        "ffmpeg": ffmpeg_ok,
        "ffprobe": ffprobe_ok
    }


@app.post("/api/edit")
async def edit_video(
    file: UploadFile = File(...),
    style: str = Form("viral"),
    format: str = Form("9:16"),
    options: str = Form("{}")
):
    extension = Path(file.filename or "video.mp4").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Format non accepté. Utilise MP4, MOV ou WEBM.")

    try:
        settings = json.loads(options)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Options de montage invalides.")

    job_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}{extension}"
    output_path = OUTPUT_DIR / f"{job_id}.mp4"

    size = 0
    try:
        with input_path.open("wb") as destination:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail="Vidéo trop volumineuse : maximum 500 Mo.")
                destination.write(chunk)
    finally:
        await file.close()

    try:
        duration = get_duration(input_path)
        if duration <= 0 or duration > 3600:
            raise HTTPException(status_code=422, detail="La vidéo doit durer moins d'une heure.")

        # Première fonction réelle du moteur : suppression des longs silences.
        if settings.get("remove_silence", True):
            segments = find_keep_segments(input_path, duration)
        else:
            segments = [(0.0, duration)]

        make_silence_cut(input_path, output_path, segments)

        return {
            "status": "completed",
            "job_id": job_id,
            "video_url": f"/outputs/{job_id}.mp4",
            "original_duration_seconds": round(duration, 2),
            "kept_segments": len(segments),
            "style_received": style,
            "format_received": format,
            "options_received": settings,
            "message": "Montage de base terminé : les silences détectés ont été retirés."
        }
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except OSError:
            pass
