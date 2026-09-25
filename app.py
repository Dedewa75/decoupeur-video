import io, math, os, subprocess, tempfile, zipfile
from flask import Flask, abort, request, send_file, send_from_directory

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 Mo max


@app.get("/")
def home():
    return send_from_directory("static", "index.html")


def duree(chemin):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", chemin],
        capture_output=True, text=True)
    return float(r.stdout.strip())


@app.post("/cut")
def cut():
    f = request.files.get("video")
    if not f:
        abort(400, "Aucune vidéo reçue.")
    try:
        secondes = max(1, min(600, int(request.form.get("seconds", 10))))
    except ValueError:
        abort(400, "Durée invalide.")
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "source" + os.path.splitext(f.filename or "")[1])
        f.save(src)
        try:
            total = duree(src)
        except ValueError:
            abort(400, "Vidéo illisible.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i in range(math.ceil(total / secondes)):
                if total - i * secondes < 0.5:
                    break
                out = os.path.join(d, f"clip_{i + 1:02d}.mp4")
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", str(i * secondes), "-t", str(secondes),
                     "-i", src, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                     "-c:a", "aac", out], capture_output=True)
                if os.path.exists(out):
                    z.write(out, os.path.basename(out))
        buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="clips.zip")
