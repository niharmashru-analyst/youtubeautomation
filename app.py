import os, re, io, json, uuid, sqlite3, shutil, subprocess, math
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-this-secret")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "100")) * 1024 * 1024

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
UPLOADS = DATA / "uploads"
PROJECTS = DATA / "projects"
DB_PATH = DATA / "studio.sqlite3"
for p in (DATA, UPLOADS, PROJECTS):
    p.mkdir(parents=True, exist_ok=True)

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
RATIOS = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (1080, 1080)}

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS projects(
          id TEXT PRIMARY KEY, name TEXT, created_at TEXT, status TEXT, ratio TEXT,
          language TEXT, transcript TEXT, seo_json TEXT, video_path TEXT, thumb_path TEXT,
          audio_path TEXT, meta_prompt TEXT, feedback TEXT DEFAULT '')""")
        con.execute("""CREATE TABLE IF NOT EXISTS memories(
          id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, category TEXT DEFAULT 'general',
          source_project TEXT, active INTEGER DEFAULT 1, created_at TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS revisions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, feedback TEXT, created_at TEXT,
          saved_as_rule INTEGER DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS oauth_tokens(
          id INTEGER PRIMARY KEY CHECK(id=1), token_json TEXT NOT NULL, updated_at TEXT)""")
init_db()

def get_memories():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM memories WHERE active=1 ORDER BY id DESC LIMIT 100")]

def add_memory(rule, category="general", project_id=None):
    rule = (rule or "").strip()
    if not rule:
        return
    with db() as con:
        con.execute("INSERT INTO memories(rule,category,source_project,created_at) VALUES(?,?,?,?)",
                    (rule[:1000], category[:60], project_id, datetime.utcnow().isoformat(timespec="seconds")))

def get_project(pid):
    with db() as con:
        r = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

def update_project(pid, **fields):
    allowed = {"status","transcript","seo_json","video_path","thumb_path","meta_prompt","feedback","name","ratio","language"}
    fields = {k:v for k,v in fields.items() if k in allowed}
    if not fields: return
    with db() as con:
        con.execute("UPDATE projects SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                    tuple(fields.values()) + (pid,))

def gemini_client():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured. Add it to your .env or Render Environment.")
    from google import genai
    return genai.Client(api_key=key)

def gemini_text(prompt, audio_bytes=None, mime_type=None):
    from google.genai import types
    client = gemini_client()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if audio_bytes is not None:
        contents = [prompt, types.Part.from_bytes(data=audio_bytes, mime_type=mime_type or "audio/mpeg")]
    else:
        contents = prompt
    response = client.models.generate_content(model=model, contents=contents)
    return (response.text or "").strip()

def allowed_upload(file, extensions):
    return bool(file and file.filename and Path(file.filename).suffix.lower() in extensions)

def write_text(path, value):
    Path(path).write_text(value or "", encoding="utf-8")

def create_seo(transcript, language, platform, memories):
    memory_text = "\n".join("- " + m["rule"] for m in memories) or "No saved preferences yet."
    prompt = f"""Create an SEO package for a video based on the transcript below.
Return ONLY valid JSON with keys: title_options (array of 5 strings), recommended_title (string),
description (string), tags (array of strings, maximum 20), hashtags (array of strings),
category_suggestion (string), thumbnail_text (string).
Use language: {language}. Platform: {platform}.
Be accurate; do not promise guaranteed rankings or invent facts.
Creator rules to follow:\n{memory_text}\nTRANSCRIPT:\n{transcript[:25000]}"""
    raw = gemini_text(prompt)
    try:
        start, end = raw.find("{"), raw.rfind("}")
        return json.loads(raw[start:end+1])
    except Exception:
        return {"title_options": [], "recommended_title": "", "description": raw, "tags": [], "hashtags": [], "thumbnail_text": ""}

def create_meta_prompt(transcript, ratio, language, style, memories):
    memory_text = "\n".join("- " + m["rule"] for m in memories) or "No saved preferences yet."
    return f"""Create a scene-by-scene slideshow video from the narration below.
Aspect ratio: {ratio}. Language: {language}. Visual style: {style}.
Use distinct visuals for each scene, relevant to the exact narration. Avoid tiny text, irrelevant imagery,
repetitive images, watermarks, awkward crops, and unsupported factual claims. Preserve the original narration
as the soundtrack. Return one visual per scene and a coherent scene sequence.
Creator rules:\n{memory_text}\nNARRATION:\n{transcript[:25000]}"""

def safe_project_path(pid, filename):
    return PROJECTS / pid / filename

@app.route("/")
def index():
    with db() as con:
        projects = [dict(r) for r in con.execute("SELECT id,name,created_at,status,ratio FROM projects ORDER BY created_at DESC LIMIT 50")]
        memories = [dict(r) for r in con.execute("SELECT * FROM memories ORDER BY id DESC LIMIT 30")]
    return render_template("index.html", projects=projects, memories=memories, youtube_connected=youtube_connected())

@app.post("/create")
def create_project():
    f = request.files.get("audio")
    if not allowed_upload(f, AUDIO_EXTS):
        flash("Please upload a supported audio file: MP3, WAV, M4A, AAC, OGG or FLAC.", "error")
        return redirect(url_for("index"))
    ratio = request.form.get("ratio", "9:16")
    if ratio not in RATIOS: ratio = "9:16"
    language = request.form.get("language", "Hindi / Hinglish")
    platform = request.form.get("platform", "YouTube Shorts")
    style = request.form.get("style", "Cinematic slideshow")
    pid = uuid.uuid4().hex[:12]
    folder = PROJECTS / pid
    folder.mkdir(parents=True, exist_ok=True)
    audio_path = folder / ("source_audio" + Path(secure_filename(f.filename)).suffix.lower())
    f.save(audio_path)
    now = datetime.utcnow().isoformat(timespec="seconds")
    name = Path(secure_filename(f.filename)).stem or "New video"
    with db() as con:
        con.execute("""INSERT INTO projects(id,name,created_at,status,ratio,language,audio_path)
                       VALUES(?,?,?,?,?,?,?)""", (pid,name,now,"uploaded",ratio,language,str(audio_path)))
    try:
        memories = get_memories()
        transcript = gemini_text(
            f"Transcribe the audio accurately in its spoken language. Preserve meaning, names, and natural wording. "
            f"Use punctuation and paragraph breaks. Language preference: {language}. Return transcript only.",
            audio_bytes=audio_path.read_bytes(), mime_type=mime_for(audio_path.suffix)
        )
        if not transcript:
            raise RuntimeError("Gemini returned an empty transcript.")
        write_text(folder / "transcript.txt", transcript)
        seo = create_seo(transcript, language, platform, memories)
        write_text(folder / "seo_package.json", json.dumps(seo, ensure_ascii=False, indent=2))
        meta_prompt = create_meta_prompt(transcript, ratio, language, style, memories)
        write_text(folder / "meta_ai_prompt.txt", meta_prompt)
        update_project(pid, transcript=transcript, seo_json=json.dumps(seo, ensure_ascii=False), meta_prompt=meta_prompt, status="transcribed")
        flash("Audio transcribed. SEO package and Meta AI prompt are ready. Meta AI generation is a manual handoff unless Meta provides supported API access.", "success")
    except Exception as e:
        update_project(pid, status="error")
        flash("Project created, but AI processing failed: " + str(e), "error")
    return redirect(url_for("project_detail", pid=pid))

def mime_for(ext):
    return {".mp3":"audio/mpeg",".wav":"audio/wav",".m4a":"audio/mp4",".aac":"audio/aac",
            ".ogg":"audio/ogg",".flac":"audio/flac",".mp4":"audio/mp4"}.get(ext, "application/octet-stream")

@app.route("/project/<pid>")
def project_detail(pid):
    p = get_project(pid)
    if not p: return "Project not found", 404
    seo = {}
    try: seo = json.loads(p.get("seo_json") or "{}")
    except Exception: pass
    memories = get_memories()
    revisions = []
    with db() as con:
        revisions = [dict(r) for r in con.execute("SELECT * FROM revisions WHERE project_id=? ORDER BY id DESC", (pid,))]
    return render_template("project.html", p=p, seo=seo, memories=memories, revisions=revisions, youtube_connected=youtube_connected())

@app.post("/project/<pid>/transcript")
def save_transcript(pid):
    p = get_project(pid)
    if not p: return "Project not found", 404
    transcript = request.form.get("transcript", "").strip()
    if not transcript: flash("Transcript cannot be blank.", "error")
    else:
        write_text(safe_project_path(pid,"transcript.txt"), transcript)
        update_project(pid, transcript=transcript, status="transcript-edited")
        flash("Transcript saved. Regenerate SEO/prompt if needed.", "success")
    return redirect(url_for("project_detail", pid=pid))

@app.post("/project/<pid>/refresh-ai")
def refresh_ai(pid):
    p = get_project(pid)
    if not p: return "Project not found", 404
    try:
        memories = get_memories()
        seo = create_seo(p.get("transcript",""), p.get("language","Auto"), request.form.get("platform","YouTube"), memories)
        prompt = create_meta_prompt(p.get("transcript",""), p.get("ratio","9:16"), p.get("language","Auto"), request.form.get("style","Cinematic slideshow"), memories)
        write_text(safe_project_path(pid,"seo_package.json"), json.dumps(seo, ensure_ascii=False, indent=2))
        write_text(safe_project_path(pid,"meta_ai_prompt.txt"), prompt)
        update_project(pid, seo_json=json.dumps(seo, ensure_ascii=False), meta_prompt=prompt)
        flash("SEO and Meta AI prompt refreshed using your current saved rules.", "success")
    except Exception as e: flash("Refresh failed: " + str(e), "error")
    return redirect(url_for("project_detail", pid=pid))

@app.post("/project/<pid>/upload-meta-video")
def upload_meta_video(pid):
    p = get_project(pid)
    f = request.files.get("video")
    if not p: return "Project not found", 404
    if not allowed_upload(f, VIDEO_EXTS):
        flash("Upload the MP4/video you generated with Meta AI.", "error")
        return redirect(url_for("project_detail", pid=pid))
    out = safe_project_path(pid, "meta_ai_result" + Path(secure_filename(f.filename)).suffix.lower())
    f.save(out)
    update_project(pid, video_path=str(out), status="meta-video-uploaded")
    flash("Meta AI video saved and available in the preview below.", "success")
    return redirect(url_for("project_detail", pid=pid))

@app.post("/project/<pid>/feedback")
def feedback(pid):
    p = get_project(pid)
    if not p: return "Project not found", 404
    text_feedback = request.form.get("feedback","").strip()
    save_rule = request.form.get("save_rule") == "yes"
    if not text_feedback:
        flash("Enter the correction first.", "error")
        return redirect(url_for("project_detail", pid=pid))
    with db() as con:
        con.execute("INSERT INTO revisions(project_id,feedback,created_at,saved_as_rule) VALUES(?,?,?,?)",
                    (pid,text_feedback,datetime.utcnow().isoformat(timespec="seconds"),1 if save_rule else 0))
    update_project(pid, feedback=text_feedback, status="revision-requested")
    if save_rule:
        add_memory(text_feedback, "creator-correction", pid)
    # Refresh prompt using the correction plus prior creator rules.
    rules = get_memories()
    updated_prompt = create_meta_prompt(p.get("transcript",""), p.get("ratio","9:16"), p.get("language","Auto"),
        "Apply this revision to the relevant scene(s), preserving everything that is already correct. Correction: " + text_feedback,
        rules)
    write_text(safe_project_path(pid,"meta_ai_revision_prompt.txt"), updated_prompt)
    update_project(pid, meta_prompt=updated_prompt)
    flash("Correction saved. " + ("It is now a persistent rule for future projects." if save_rule else "It applies to this project only.") + " Copy the updated prompt into Meta AI, then upload the revised video here.", "success")
    return redirect(url_for("project_detail", pid=pid))

@app.post("/memory/add")
def memory_add():
    rule = request.form.get("rule","").strip()
    if rule:
        add_memory(rule, request.form.get("category","preference"))
        flash("Creator rule saved.", "success")
    return redirect(url_for("index"))

@app.post("/memory/<int:mid>/toggle")
def memory_toggle(mid):
    with db() as con:
        con.execute("UPDATE memories SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (mid,))
    flash("Memory rule status updated.", "success")
    return redirect(request.referrer or url_for("index"))

@app.route("/project/<pid>/file/<path:filename>")
def project_file(pid, filename):
    folder = PROJECTS / pid
    path = (folder / filename).resolve()
    if folder.resolve() not in path.parents or not path.is_file(): return "File not found", 404
    return send_from_directory(folder, filename, as_attachment=request.args.get("download") == "1")

@app.post("/project/<pid>/make-local-video")
def make_local_video(pid):
    p = get_project(pid)
    if not p: return "Project not found", 404
    try:
        make_slideshow(pid, p)
        flash("Free local-style slideshow MP4 created from typographic slides and your original audio.", "success")
    except Exception as e:
        flash("MP4 generation failed: " + str(e), "error")
    return redirect(url_for("project_detail", pid=pid))

def wrap_text(draw, text, font, max_width):
    words = re.split(r"\s+", text.strip())
    lines, line = [], ""
    for word in words:
        test = (line + " " + word).strip()
        if draw.textbbox((0,0), test, font=font)[2] <= max_width or not line:
            line = test
        else:
            lines.append(line); line = word
    if line: lines.append(line)
    return lines

def make_slideshow(pid, p):
    from PIL import Image, ImageDraw, ImageFont
    import imageio_ffmpeg
    folder = PROJECTS / pid
    transcript = p.get("transcript","").strip()
    audio = Path(p.get("audio_path",""))
    if not transcript or not audio.exists(): raise RuntimeError("Need a transcript and source audio.")
    w,h = RATIOS.get(p.get("ratio"), RATIOS["9:16"])
    # split transcript into readable scene cards
    chunks = re.split(r"(?<=[.!?।])\s+|\n+", transcript)
    chunks = [c.strip() for c in chunks if c.strip()]
    scenes, buf = [], ""
    limit = 150 if w < h else 220
    for c in chunks:
        if len(buf) + len(c) > limit and buf:
            scenes.append(buf); buf = c
        else: buf = (buf + " " + c).strip()
    if buf: scenes.append(buf)
    scenes = scenes[:100]
    if not scenes: scenes = [transcript[:limit]]
    font_path = os.environ.get("FONT_PATH", "")
    def font(size):
        try:
            if font_path and Path(font_path).exists(): return ImageFont.truetype(font_path, size)
            for candidate in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"]:
                if Path(candidate).exists(): return ImageFont.truetype(candidate, size)
        except Exception: pass
        return ImageFont.load_default()
    title_font = font(max(28, int(min(w,h)*0.045)))
    body_font = font(max(22, int(min(w,h)*0.035)))
    # determine audio duration
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    probe_cmd = [ffmpeg, "-i", str(audio), "-f", "null", "-"]
    probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=60)
    match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", probe.stderr or "")
    duration = int(match.group(1))*3600 + int(match.group(2))*60 + float(match.group(3)) if match else max(15, len(transcript)/12)
    duration = max(1, duration)
    each = max(1.5, duration / len(scenes))
    lines_file = folder / "slides.txt"
    with lines_file.open("w", encoding="utf-8") as cf:
        for i, text in enumerate(scenes):
            # dark gradient-ish background
            img = Image.new("RGB", (w,h), (16,16,22))
            d = ImageDraw.Draw(img)
            d.rectangle((0,0,w, max(12,int(h*0.012))), fill=(217,70,239))
            d.text((int(w*.07), int(h*.08)), f"SCENE {i+1:02d}", font=title_font, fill=(217,70,239))
            wrapped = wrap_text(d, text, body_font, int(w*.84))
            line_h = int(body_font.size*1.45) if hasattr(body_font,"size") else 36
            total_h = len(wrapped)*line_h
            y = max(int(h*.20), (h-total_h)//2)
            for line in wrapped:
                d.text((int(w*.07), y), line, font=body_font, fill=(245,245,248), stroke_width=0)
                y += line_h
            d.text((int(w*.07), int(h*.92)), "AI VIDEO STUDIO", font=font(max(14,int(min(w,h)*.018))), fill=(160,160,170))
            image_path = folder / f"scene_{i+1:03d}.jpg"
            img.save(image_path, quality=90)
            cf.write(f"file '{image_path.as_posix()}'\n")
            cf.write(f"duration {each:.3f}\n")
        cf.write(f"file '{(folder / f'scene_{len(scenes):03d}.jpg').as_posix()}'\n")
    out = folder / "final_video.mp4"
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(lines_file), "-i", str(audio),
           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
           "-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p","-c:a","aac","-b:a","128k",
           "-shortest","-movflags","+faststart",str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0: raise RuntimeError((result.stderr or "FFmpeg error")[-1800:])
    update_project(pid, video_path=str(out), status="video-ready")
    # Make a simple free thumbnail from the first scene.
    thumb = folder / "thumbnail.png"
    first = folder / "scene_001.jpg"
    Image.open(first).save(thumb)
    update_project(pid, thumb_path=str(thumb))
    return out

def youtube_connected():
    try:
        with db() as con:
            r = con.execute("SELECT token_json FROM oauth_tokens WHERE id=1").fetchone()
        return bool(r)
    except Exception: return False

def youtube_flow(state=None):
    from google_auth_oauthlib.flow import Flow
    client_id = os.environ.get("YOUTUBE_CLIENT_ID","").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET","").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in environment variables first.")
    redirect_uri = request.url_root.rstrip("/") + url_for("youtube_callback")
    client_config = {"web":{"client_id":client_id,"client_secret":client_secret,
        "auth_uri":"https://accounts.google.com/o/oauth2/auth",
        "token_uri":"https://oauth2.googleapis.com/token",
        "redirect_uris":[redirect_uri]}}
    flow = Flow.from_client_config(client_config, scopes=[
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.force-ssl"],
        autogenerate_code_verifier=True)
    flow.redirect_uri = redirect_uri
    return flow

@app.route("/youtube/connect")
def youtube_connect():
    try:
        flow = youtube_flow()
        auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        session["youtube_oauth_state"] = state
        # PKCE verifier must survive the redirect and be restored on callback.
        session["youtube_code_verifier"] = flow.code_verifier
        session.modified = True
        return redirect(auth_url)
    except Exception as e:
        flash("YouTube connection setup: " + str(e), "error")
        return redirect(url_for("index"))

@app.route("/youtube/callback")
def youtube_callback():
    try:
        flow = youtube_flow()
        if request.args.get("state") != session.get("youtube_oauth_state"):
            raise RuntimeError("OAuth state mismatch. Please connect again.")
        # The callback creates a new Flow object, so restore the verifier from the session.
        flow.code_verifier = session.pop("youtube_code_verifier", None)
        if not flow.code_verifier:
            raise RuntimeError("OAuth PKCE verifier is missing. Please click Connect YouTube and authorize again.")
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        token = {"token":creds.token,"refresh_token":creds.refresh_token,"token_uri":creds.token_uri,
                 "client_id":creds.client_id,"client_secret":creds.client_secret,
                 "scopes":creds.scopes,"expiry":creds.expiry.isoformat() if creds.expiry else None}
        with db() as con:
            con.execute("INSERT OR REPLACE INTO oauth_tokens(id,token_json,updated_at) VALUES(1,?,?)",
                        (json.dumps(token),datetime.utcnow().isoformat(timespec="seconds")))
        flash("YouTube connected. Uploads require a completed project with an MP4.", "success")
    except Exception as e:
        flash("YouTube OAuth failed: " + str(e), "error")
    return redirect(url_for("index"))

def youtube_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    with db() as con:
        r = con.execute("SELECT token_json FROM oauth_tokens WHERE id=1").fetchone()
    if not r: raise RuntimeError("Connect YouTube first.")
    d = json.loads(r["token_json"])
    creds = Credentials(token=d.get("token"), refresh_token=d.get("refresh_token"),
        token_uri=d.get("token_uri"), client_id=d.get("client_id"), client_secret=d.get("client_secret"),
        scopes=d.get("scopes"))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        d["token"] = creds.token
        with db() as con:
            con.execute("UPDATE oauth_tokens SET token_json=?,updated_at=? WHERE id=1",
                        (json.dumps(d),datetime.utcnow().isoformat(timespec="seconds")))
    return creds

@app.post("/project/<pid>/youtube-upload")
def youtube_upload(pid):
    p = get_project(pid)
    if not p: return "Project not found", 404
    video_path = Path(p.get("video_path") or "")
    if not video_path.is_file():
        flash("Create a video or upload the Meta AI MP4 first.", "error")
        return redirect(url_for("project_detail", pid=pid))
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        creds = youtube_credentials()
        service = build("youtube","v3",credentials=creds)
        try: seo = json.loads(p.get("seo_json") or "{}")
        except Exception: seo = {}
        title = request.form.get("title") or seo.get("recommended_title") or p["name"]
        description = request.form.get("description") or seo.get("description") or ""
        tags_raw = request.form.get("tags") or ", ".join(seo.get("tags", []))
        tags = [x.strip() for x in tags_raw.split(",") if x.strip()][:500]
        privacy = request.form.get("privacy","private")
        if privacy not in {"private","unlisted","public"}: privacy = "private"
        body = {"snippet":{"title":title[:100],"description":description[:5000],"tags":tags,"categoryId":"27"},
                "status":{"privacyStatus":privacy,"selfDeclaredMadeForKids":False}}
        media = MediaFileUpload(str(video_path), chunksize=8*1024*1024, resumable=True, mimetype="video/mp4")
        result = service.videos().insert(part="snippet,status", body=body, media_body=media).execute()
        vid = result.get("id")
        thumb_path = Path(p.get("thumb_path") or "")
        thumb_uploaded = False
        if thumb_path.exists():
            try:
                service.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb_path), mimetype="image/png")).execute()
                thumb_uploaded = True
            except Exception: pass
        with db() as con:
            con.execute("INSERT INTO revisions(project_id,feedback,created_at,saved_as_rule) VALUES(?,?,?,0)",
                (pid, f"YouTube upload: https://www.youtube.com/watch?v={vid}; thumbnail uploaded={thumb_uploaded}; visibility={privacy}",
                 datetime.utcnow().isoformat(timespec="seconds")))
        flash(f"Upload complete: https://www.youtube.com/watch?v={vid}. Thumbnail uploaded: {thumb_uploaded}.", "success")
    except Exception as e:
        flash("YouTube upload failed: " + str(e), "error")
    return redirect(url_for("project_detail", pid=pid))

@app.route("/health")
def health():
    return {"ok": True, "app": "AI Video Studio"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
