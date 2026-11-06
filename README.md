# PLAY — YouTube Downloader

A sleek web app to download YouTube playlists and individual videos as MP4 or MP3. Built with Python, Flask, and yt-dlp.

## Features
- Fetch playlist or single video metadata
- Select/deselect specific videos
- Download as MP4 (video) or MP3 (audio)
- For MP3, choose bitrate (64-320 kbps)
- For MP4, choose quality (360p - 1080p, or Best)
- Up to 3 parallel downloads
- Per-video progress bars
- Download individual files or full ZIP
- Optional speed limiting
- Error handling for private/unavailable videos

## Requirements
- Python 3.8+
- FFmpeg (for MP3 conversion and MP4 merging)

## Local Setup & Run

### 1. Install FFmpeg
- **Ubuntu/Debian**: `sudo apt install ffmpeg`
- **macOS**: `brew install ffmpeg`
- **Windows**: Download from https://ffmpeg.org/download.html and add to PATH

### 2. Install Python Dependencies
```bash
pip install -r requirements.txt
```

### 3. Start the Server
```bash
python app.py
```
Open [http://localhost:5500](http://localhost:5500) in your browser.

## Deployment

This app is fully configured for deployment on modern PaaS platforms like **Render**, **Heroku**, or **Railway**.

### Render.com (Recommended)
This repository includes a `render.yaml` blueprint. Render's native Python runtime already includes FFmpeg.
1. Push this repository to GitHub.
2. Sign in to [Render](https://render.com/).
3. Click **New → Blueprint** and connect your GitHub repo.
4. Render detects `render.yaml`, installs dependencies, and starts `gunicorn` (1 worker / 8 threads).

### Heroku
This repository is Heroku-ready and utilizes `Procfile` and `Aptfile`.
1. Make sure you add the **FFmpeg buildpack** (`https://github.com/jonathanong/heroku-buildpack-ffmpeg-latest.git`) alongside the Python buildpack.
2. Deploy the app. The `Procfile` automatically utilizes `gunicorn`.

## Project Structure
```text
YT-Downloader v1.3/
├── app.py              # Flask backend processor
├── requirements.txt    # Python dependencies (includes gunicorn for deploy)
├── Procfile            # Webserver configuration
├── render.yaml         # Render.com Blueprint configuration
├── Aptfile             # FFmpeg dependencies for cloud deployment
├── downloads/          # Downloaded cache (ignored in Git)
├── static/             # Extraneous static assets
└── templates/          # Jinja HTML Templates
    ├── index.html      # Playlist fetcher
    ├── single.html     # Single fetcher
    ├── about.html
    ├── contact.html
    ├── privacy.html
    └── disclaimer.html
```

## API Endpoints
- `POST /api/fetch` — Fetch playlist/video info
- `POST /api/download` — Start download job
- `GET /api/status/<job_id>` — Poll job status
- `GET /api/download-file/<filename>` — Download a file
- `GET /api/download-zip/<job_id>` — Download all as ZIP
- `DELETE /api/cleanup/<job_id>` — Delete downloaded files
