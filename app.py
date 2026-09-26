import math, os, shutil, subprocess, tempfile, threading, time, uuid, zipfile
from flask import Flask, jsonify, request, send_file, send_from_directory

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 Mo max

WORKDIR = tempfile.mkdtemp(prefix="decoupeur_")
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_MAX_AGE = 2 * 3600  # ménage des anciens jobs après 2h


@app.get("/")
def home():
    return send_from_directory("static", "index.html")


def nettoyer_anciens_jobs():
    now = time.time()
    for name in os.listdir(WORKDIR):
        path = os.path.join(WORKDIR, name)
        try:
            if now - os.path.getmtime(path) > JOB_MAX_AGE:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def duree(chemin):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", chemin],
        capture_output=True, text=True)
    return float(r.stdout.strip())


def cut_one(src, out, start, dur):
    # Essai rapide : copie les flux sans ré-encoder (beaucoup plus rapide,
    # mais la coupe se cale sur l'image clé la plus proche).
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(dur),
         "-i", src, "-c", "copy", "-avoid_negative_ts", "make_zero", out],
        capture_output=True)
    if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        return
    # Repli : ré-encodage classique si la copie a échoué (plus lent mais fiable)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(dur),
         "-i", src, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", out], capture_output=True)


def process_job(job_id, src, secondes):
    job_dir = os.path.dirname(src)
    try:
        total = duree(src)
        total_clips = max(1, math.ceil(total / secondes))
        while total_clips > 0 and total - (total_clips - 1) * secondes < 0.5:
            total_clips -= 1
        with JOBS_LOCK:
            JOBS[job_id]["totalClips"] = total_clips

        zip_path = os.path.join(job_dir, "clips.zip")
        with zipfile.ZipFile(zip_path, "w") as z:
            for idx in range(total_clips):
                out = os.path.join(job_dir, f"clip_{idx + 1:02d}.mp4")
                cut_one(src, out, idx * secondes, secondes)
                if os.path.exists(out):
                    z.write(out, os.path.basename(out))
                    os.remove(out)
                with JOBS_LOCK:
                    JOBS[job_id]["currentClip"] = idx + 1
                    JOBS[job_id]["progress"] = round((idx + 1) / total_clips * 100)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["zipPath"] = zip_path
    except Exception:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = "Une erreur est survenue pendant le découpage."
    finally:
        try:
            os.remove(src)
        except OSError:
            pass


@app.post("/start")
def start():
    nettoyer_anciens_jobs()
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="Aucune vidéo reçue."), 400
    try:
        secondes = max(1, min(600, int(request.form.get("seconds", 10))))
    except ValueError:
        return jsonify(error="Durée invalide."), 400

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(WORKDIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    src = os.path.join(job_dir, "source" + os.path.splitext(f.filename)[1])
    f.save(src)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "processing", "progress": 0,
            "currentClip": 0, "totalClips": 0,
            "zipPath": None, "error": None,
        }
    threading.Thread(target=process_job, args=(job_id, src, secondes), daemon=True).start()
    return jsonify(jobId=job_id)


@app.get("/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Session inconnue ou expirée."), 404
    return jsonify(
        status=job["status"], progress=job["progress"],
        currentClip=job["currentClip"], totalClips=job["totalClips"],
        error=job["error"],
    )


@app.get("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job.get("zipPath"):
        return jsonify(error="Fichier indisponible."), 404
    return send_file(job["zipPath"], mimetype="application/zip",
                      as_attachment=True, download_name="clips.zip")
