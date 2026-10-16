# AI Video Studio (Free-quota-first starter)

A Flask application for:
- Uploading audio and asking Gemini to transcribe it.
- Generating SEO metadata and a Meta AI scene prompt.
- Editing the transcript and saving persistent creator rules in SQLite.
- Manually uploading a Meta AI-generated MP4 for preview/revision workflow.
- Creating a simple typographic slideshow MP4 with the original audio using FFmpeg.
- Connecting YouTube with OAuth and uploading an MP4 with title, description, tags and visibility.
- Creating a simple thumbnail locally from the first slide (no paid image API required).

## Important limitation: Meta AI integration

This starter does **not** claim to automate Meta AI itself. There is no assumed private/browser automation. It creates a prompt you can copy to Meta AI, then lets you upload the MP4 that Meta AI returns. It previews that result and saves corrections. To automatically send revision prompts and retrieve video output, Meta must provide supported API access for your account/use case.

## 1. Requirements

- Python 3.11+ recommended.
- Git.
- A Gemini API key from Google AI Studio.
- For YouTube: a Google Cloud project with YouTube Data API v3 enabled and OAuth web client credentials.
- For local MP4 generation: `imageio-ffmpeg` downloads/uses an FFmpeg binary. Some deployment platforms may restrict memory/time, especially on free plans.

## 2. Run locally on Windows

Open PowerShell in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and fill in your keys. PowerShell does not automatically load `.env`; either set variables in the terminal or use the commands below after editing:

```powershell
$env:FLASK_SECRET_KEY="a-long-random-secret"
$env:GEMINI_API_KEY="YOUR_GEMINI_KEY"
$env:GEMINI_MODEL="gemini-2.5-flash"
$env:YOUTUBE_CLIENT_ID="YOUR_CLIENT_ID"
$env:YOUTUBE_CLIENT_SECRET="YOUR_CLIENT_SECRET"
python app.py
```

Open http://127.0.0.1:5000

## 3. Get a Gemini API key

1. Visit https://aistudio.google.com/app/apikey
2. Sign in and create an API key.
3. Keep the key private. Do not put it in HTML/JavaScript or commit it to GitHub.
4. Add it as `GEMINI_API_KEY` in Render Environment or local environment.
5. Review current free tier and model quotas: https://ai.google.dev/gemini-api/docs/pricing and https://ai.google.dev/gemini-api/docs/models

Free quotas and available models change. This app uses `GEMINI_MODEL` as a configurable setting. If `gemini-2.5-flash` is no longer available for your account, choose an eligible current model listed in AI Studio and update the environment variable. The app does not automatically fall back to a paid model.

## 4. Connect YouTube (official OAuth)

1. Open https://console.cloud.google.com/
2. Create a project or choose an existing project.
3. Go to APIs & Services → Library → enable **YouTube Data API v3**.
4. Go to Google Auth Platform / OAuth consent screen and configure the app details and test users as required.
5. In APIs & Services → Credentials, create **OAuth client ID** → **Web application**.
6. Add an Authorized redirect URI:
   - Local: `http://127.0.0.1:5000/youtube/callback`
   - Render: `https://YOUR-APP-NAME.onrender.com/youtube/callback`
7. Copy Client ID and Client Secret to `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` environment variables.
8. Run the app and click **Connect YouTube**. Sign in to the correct channel account and approve permissions.
9. Generate or upload a video, review title/description/tags/visibility, then click **Upload to YouTube**.

The app requests upload and management scopes. Keep OAuth secrets private. Do not publish the client secret in GitHub. Use private visibility for initial tests. Google may restrict uploads from unverified API projects; see current YouTube API policies:
- https://developers.google.com/youtube/v3/guides/authentication
- https://developers.google.com/youtube/v3/docs/videos/insert
- https://developers.google.com/youtube/v3/docs/thumbnails/set
- https://developers.google.com/youtube/v3/getting-started

A successful upload returns a YouTube video URL. Thumbnail upload may fail if the channel/API project is not eligible; the app reports that separately.

## 5. Deploy to Render + GitHub

1. Create a new GitHub repository, e.g. `ai-video-studio`.
2. Upload these files (do not upload `.env`, API keys, database files, audio, or videos).
3. On Render choose **New → Blueprint** and connect the repository, or create a Python Web Service manually.
4. Set build command: `pip install -r requirements.txt`
5. Set start command: `gunicorn app:app --workers 1 --threads 2 --timeout 900`
6. Add Environment variables:
   - `FLASK_SECRET_KEY`: long random secret
   - `GEMINI_API_KEY`: key from AI Studio
   - `GEMINI_MODEL`: eligible model ID
   - `YOUTUBE_CLIENT_ID`
   - `YOUTUBE_CLIENT_SECRET`
7. Update the OAuth redirect URI in Google Cloud to the exact deployed URL `/youtube/callback`.
8. Redeploy and click Connect YouTube.

### Data persistence warning

Render's free service filesystem is ephemeral. SQLite project history, saved rules, OAuth tokens, and uploaded files can be lost when the service restarts/redeploys. This starter works best locally for persistent experiments. For reliable hosted persistence, attach a persistent disk (usually paid) or migrate project data/files to a durable database/storage service. Never rely on free ephemeral storage as permanent memory.

## 6. How to use

1. Upload an audio file.
2. Choose aspect ratio, language, platform and style.
3. The app generates transcript, SEO JSON and `meta_ai_prompt.txt`.
4. Copy the prompt into Meta AI manually and generate your slideshow.
5. Upload Meta AI's returned MP4 in the project page to preview it.
6. Enter feedback. Tick “Save as a permanent creator rule” to remember it for future prompts.
7. Or click “Create a basic MP4 locally” to create a typography slideshow from the transcript and original audio.
8. Review metadata and upload to YouTube.

## 7. Free-cost guarantees and limitations

- The code does not intentionally call OpenAI image-generation APIs or any paid image API.
- Gemini API usage is limited by the account's current free quota; if quota is exhausted, the task errors rather than switching providers.
- YouTube Data API generally has no per-upload fee, but uses project quota and policy restrictions.
- FFmpeg is free software, but hosting CPU/RAM/time and storage are limited on free plans.
- Meta AI generation remains a manual step unless an officially supported API is available.
- The local thumbnail is a simple generated image, not an image-generation model output. You can use the prompt in ChatGPT manually if you prefer.
- Do not publish copyrighted audio/visuals without the necessary rights.

## 8. Security notes

This is a starter for personal use, not a hardened public multi-user app. Add authentication before making it public. Do not expose it to strangers with your OAuth tokens. Use a strong `FLASK_SECRET_KEY`; never commit `.env`, OAuth secrets, tokens or user media to GitHub.
