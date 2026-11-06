import os
import json
import uuid
import threading
import zipfile
import time
import io
import shutil
from collections import deque
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_DIR = os.path.join(BASE_DIR, 'ffmpeg')
_local_ffmpeg_name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
_local_ffmpeg = os.path.join(FFMPEG_DIR, _local_ffmpeg_name)

# Prefer bundled ffmpeg/ next to app.py (local Windows); fall back to system PATH (Render/Linux).
if os.path.isfile(_local_ffmpeg):
    FFMPEG = _local_ffmpeg
    FFMPEG_LOCATION = FFMPEG_DIR
else:
    FFMPEG = shutil.which('ffmpeg') or 'ffmpeg'
    FFMPEG_LOCATION = None

_deno_bin = os.path.join(os.path.expanduser('~'), '.deno', 'bin')
if os.path.isdir(_deno_bin) and _deno_bin not in os.environ.get('PATH', ''):
    os.environ['PATH'] = _deno_bin + os.pathsep + os.environ.get('PATH', '')


app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
JOB_TTL_SECONDS = 10 * 60  # expiry is based on completion time (finished_at)
MAX_RUNNING_JOBS = 5
MAX_QUEUED_JOBS = 20
JOB_PARALLEL_DOWNLOADS = 3
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60

job_lock = threading.Lock()
queue_lock = threading.Lock()
pending_jobs = deque()
active_jobs = set()

rate_lock = threading.Lock()
rate_limit_state = {}


def _get_client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _allow_request_by_rate_limit(ip):
    now = time.time()
    with rate_lock:
        dq = rate_limit_state.setdefault(ip, deque())
        window_start = now - RATE_LIMIT_WINDOW_SECONDS
        while dq and dq[0] < window_start:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_REQUESTS:
            return False
        dq.append(now)
        return True


@app.before_request
def _basic_rate_limit():
    if not request.path.startswith('/api/'):
        return None
    # Do not rate-limit high-frequency polling or file delivery endpoints.
    # Frontend polls /api/status every second and auto-download triggers many file requests.
    if request.path.startswith('/api/status/') or request.path.startswith('/api/download-file/'):
        return None
    # Also avoid blocking zip delivery requests.
    if request.path.startswith('/api/download-zip/'):
        return None
    ip = _get_client_ip()
    if _allow_request_by_rate_limit(ip):
        return None
    return jsonify({'error': 'Too many requests. Please try again shortly.'}), 429


def _expires_at(job):
    finished_at = job.get('finished_at')
    if not finished_at:
        return None
    return float(finished_at) + JOB_TTL_SECONDS


def _is_job_expired(job, now=None):
    exp = _expires_at(job)
    if exp is None:
        return False
    now = time.time() if now is None else now
    return now >= exp


def _find_job_by_filename(filename):
    for job_id, job in jobs.items():
        if filename in (job.get('files') or []):
            return job_id, job
    return None, None


def _remove_job_files(job):
    for fname in job.get('files', []) or []:
        try:
            os.remove(os.path.join(DOWNLOAD_DIR, fname))
        except Exception:
            pass
    job['files'] = []


def _remove_files_by_video_ids(video_ids):
    if not video_ids:
        return
    try:
        names = os.listdir(DOWNLOAD_DIR)
    except Exception:
        return
    for fname in names:
        if any(vid_id and vid_id in fname for vid_id in video_ids):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, fname))
            except Exception:
                pass


def _cleanup_job_record_and_files(job):
    if not job:
        return
    _remove_job_files(job)
    vid_ids = list((job.get('videos') or {}).keys())
    _remove_files_by_video_ids(vid_ids)


def _start_job_thread(job_id, videos, fmt, mp3_bitrate, mp4_quality):
    active_jobs.add(job_id)
    t = threading.Thread(
        target=run_downloads,
        args=(job_id, videos, fmt, mp3_bitrate, mp4_quality),
        daemon=True
    )
    t.start()


def _dispatch_jobs_locked():
    while pending_jobs and len(active_jobs) < MAX_RUNNING_JOBS:
        job_id, videos, fmt, mp3_bitrate, mp4_quality = pending_jobs.popleft()
        job = jobs.get(job_id)
        if not job or job.get('cancelled'):
            continue
        _start_job_thread(job_id, videos, fmt, mp3_bitrate, mp4_quality)


def _cleanup_worker():
    while True:
        now = time.time()
        expired_job_ids = []
        for job_id, job in list(jobs.items()):
            if job.get('status') in ('done', 'cancelled') and _is_job_expired(job, now=now):
                expired_job_ids.append(job_id)

        for job_id in expired_job_ids:
            job = jobs.pop(job_id, None)
            if job:
                _cleanup_job_record_and_files(job)

        time.sleep(30)


threading.Thread(target=_cleanup_worker, daemon=True).start()

def _verify_ffmpeg():
    if FFMPEG_LOCATION:
        if not os.path.isfile(FFMPEG):
            raise RuntimeError(
                f"ffmpeg not found at: {FFMPEG}\n"
                "Please make sure the 'ffmpeg' folder containing ffmpeg/ffprobe "
                "exists next to app.py."
            )
        return
    if not shutil.which('ffmpeg'):
        raise RuntimeError(
            "ffmpeg not found on PATH. Install ffmpeg system-wide, "
            "or place binaries in the local 'ffmpeg' folder next to app.py."
        )


def get_ydl_opts(fmt='mp4', output_template=None, mp3_bitrate=192, mp4_quality='best'):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'outtmpl': output_template or os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
        'socket_timeout': 30,
        'retries': 10,
        'fragment_retries': 10,
        'extractor_retries': 5,
        'file_access_retries': 3,
        'continuedl': True,
        'concurrent_fragment_downloads': 1,
        'remote_components': ['ejs:github'],
    }
    if FFMPEG_LOCATION:
        opts['ffmpeg_location'] = FFMPEG_LOCATION
    if fmt == 'mp3':
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': str(mp3_bitrate),
        }]
    else:
        # Keep quality caps strict for fixed resolutions.
        # Require a known positive height to avoid "unknown-height" formats bypassing limits.
        mp4_selectors = {
            'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            '1080': 'bestvideo[height<=1080][height>0][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080][height>0]+bestaudio/best[height<=1080][height>0]',
            '720': 'bestvideo[height<=720][height>0][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720][height>0]+bestaudio/best[height<=720][height>0]',
            '480': 'bestvideo[height<=480][height>0][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480][height>0]+bestaudio/best[height<=480][height>0]',
            '360': 'bestvideo[height<=360][height>0][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360][height>0]+bestaudio/best[height<=360][height>0]',
        }
        opts['format'] = mp4_selectors.get(str(mp4_quality), mp4_selectors['best'])
        if str(mp4_quality) != 'best':
            # Bias selection toward the requested cap where possible.
            opts['format_sort'] = [f'res:{mp4_quality}', 'vcodec:h264']
        opts['merge_output_format'] = 'mp4'
    return opts


ALLOWED_MP3_BITRATES_KBPS = {64, 96, 128, 160, 192, 224, 256, 320}
ALLOWED_MP4_QUALITIES = {'best', '1080', '720', '480', '360'}


def get_mp3_bitrate_from_request(value, default=192):
    try:
        ivalue = int(value)
    except Exception:
        return default
    return ivalue if ivalue in ALLOWED_MP3_BITRATES_KBPS else default


def get_mp4_quality_from_request(value, default='best'):
    sval = str(value).strip().lower() if value is not None else default
    return sval if sval in ALLOWED_MP4_QUALITIES else default


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/single')
def single_page():
    return render_template('single.html')


@app.route('/about')
def about_page():
    return render_template('about.html')


@app.route('/contact')
def contact_page():
    return render_template('contact.html')


@app.route('/privacy')
def privacy_page():
    return render_template('privacy.html')


@app.route('/disclaimer')
def disclaimer_page():
    return render_template('disclaimer.html')


@app.route('/api/fetch', methods=['POST'])
def fetch_info():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': 'in_playlist', 'skip_download': True}) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({'error': 'Could not fetch info'}), 400

        videos = []
        if info.get('_type') == 'playlist':
            for entry in (info.get('entries') or []):
                if entry:
                    vid_id = entry.get('id', '')
                    videos.append({
                        'id': vid_id,
                        'title': entry.get('title', 'Unknown'),
                        'duration': entry.get('duration'),
                        'thumbnail': entry.get('thumbnail') or f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
                        'url': entry.get('url') or f"https://www.youtube.com/watch?v={vid_id}",
                        'filesize': entry.get('filesize'),
                    })
            return jsonify({'type': 'playlist', 'title': info.get('title', 'Playlist'), 'videos': videos})
        else:
            with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl2:
                detail = ydl2.extract_info(url, download=False)
            videos.append({
                'id': detail.get('id', ''),
                'title': detail.get('title', 'Unknown'),
                'duration': detail.get('duration'),
                'thumbnail': detail.get('thumbnail'),
                'url': url,
                'filesize': detail.get('filesize'),
            })
            return jsonify({'type': 'video', 'title': detail.get('title', 'Video'), 'videos': videos})
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if 'private' in msg:
            return jsonify({'error': 'This video/playlist is private'}), 400
        if 'not available' in msg:
            return jsonify({'error': 'Video not available'}), 400
        return jsonify({'error': f'Error: {str(e)[:200]}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)[:300]}), 500


@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.json or {}
    videos = data.get('videos', [])
    fmt = data.get('format', 'mp4')
    mp3_bitrate = get_mp3_bitrate_from_request(data.get('mp3Bitrate'), default=192)
    mp4_quality = get_mp4_quality_from_request(data.get('mp4Quality'), default='best')
    if not videos:
        return jsonify({'error': 'No videos selected'}), 400
    try:
        _verify_ffmpeg()
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 500

    job_id = str(uuid.uuid4())
    with job_lock:
        if len(active_jobs) >= MAX_RUNNING_JOBS and len(pending_jobs) >= MAX_QUEUED_JOBS:
            return jsonify({'error': 'Server is busy. Please try again later.'}), 503

        jobs[job_id] = {
            'status': 'queued', 'total': len(videos),
            'completed': 0, 'failed': 0, 'files': [],
            'created_at': time.time(), 'finished_at': None,
            'cancelled': False,
            'format': fmt,
            'mp4_quality': mp4_quality,
            'videos': {v['id']: {'status': 'queued', 'progress': 0, 'title': v.get('title', ''), 'file': None, 'error': None} for v in videos},
        }

        pending_jobs.append((job_id, videos, fmt, mp3_bitrate, mp4_quality))
        _dispatch_jobs_locked()
        queued_ahead = 0
        for idx, item in enumerate(pending_jobs):
            if item[0] == job_id:
                queued_ahead = idx
                break
    return jsonify({'jobId': job_id, 'queuedAhead': queued_ahead})


def run_downloads(job_id, videos, fmt, mp3_bitrate, mp4_quality):
    job = jobs.get(job_id)
    if not job:
        return
    job['status'] = 'running'
    sem = threading.Semaphore(JOB_PARALLEL_DOWNLOADS)
    threads = [threading.Thread(target=download_one, args=(job_id, v, fmt, mp3_bitrate, mp4_quality, sem), daemon=True) for v in videos]
    for t in threads: t.start()
    for t in threads: t.join()
    if job.get('cancelled'):
        job['status'] = 'cancelled'
    else:
        job['status'] = 'done'
    job['finished_at'] = time.time()
    with job_lock:
        active_jobs.discard(job_id)
        _dispatch_jobs_locked()


def download_one(job_id, video, fmt, mp3_bitrate, mp4_quality, semaphore):
    vid_id = video['id']
    url = video['url']
    job = jobs.get(job_id)
    if not job:
        return
    vinfo = job['videos'][vid_id]
    with semaphore:
        if job.get('cancelled'):
            vinfo['status'] = 'cancelled'
            return
        vinfo['status'] = 'downloading'
        ext = 'mp3' if fmt == 'mp3' else 'mp4'
        safe = "".join(c for c in vinfo['title'] if c.isalnum() or c in ' ._-')[:60]
        if fmt == 'mp4':
            quality_tag = mp4_quality if mp4_quality else 'best'
            out_tpl = os.path.join(DOWNLOAD_DIR, f"{safe}_{vid_id}_{quality_tag}.%(ext)s")
        else:
            out_tpl = os.path.join(DOWNLOAD_DIR, f"{safe}_{vid_id}.%(ext)s")

        def hook(d):
            if job.get('cancelled'):
                # Abort yt-dlp quickly when user leaves/reloads.
                raise yt_dlp.utils.DownloadError('cancelled')
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                dl = d.get('downloaded_bytes', 0)
                if total: vinfo['progress'] = round(dl / total * 100, 1)
            elif d['status'] == 'finished':
                vinfo['progress'] = 99

        opts = get_ydl_opts(
            fmt,
            out_tpl,
            mp3_bitrate=mp3_bitrate,
            mp4_quality=mp4_quality
        )
        opts['progress_hooks'] = [hook]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            actual = None
            for f in os.listdir(DOWNLOAD_DIR):
                if vid_id in f and f.lower().endswith(f".{ext}"):
                    actual = f
                    break
            vinfo['status'] = 'completed'
            vinfo['progress'] = 100
            vinfo['file'] = actual
            if actual: job['files'].append(actual)
            job['completed'] += 1
        except yt_dlp.utils.DownloadError as e:
            if job.get('cancelled'):
                vinfo['status'] = 'cancelled'
                vinfo['error'] = 'cancelled'
            else:
                vinfo['status'] = 'failed'
                # Surface the most useful part of yt-dlp errors (timeouts/resets/private/etc.).
                vinfo['error'] = str(e).replace('ERROR:', '').strip()[:220]
                job['failed'] += 1
        except Exception as e:
            if job.get('cancelled'):
                vinfo['status'] = 'cancelled'
                vinfo['error'] = 'cancelled'
            else:
                vinfo['status'] = 'failed'
                vinfo['error'] = str(e)[:220]
                job['failed'] += 1


@app.route('/api/status/<job_id>')
def get_status(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({'error': 'Not found'}), 404
    payload = dict(job)
    payload['expires_at'] = _expires_at(job)
    payload['expired'] = _is_job_expired(job)
    payload['running_jobs'] = len(active_jobs)
    payload['max_running_jobs'] = MAX_RUNNING_JOBS
    queue_position = None
    with job_lock:
        for idx, item in enumerate(pending_jobs):
            if item[0] == job_id:
                queue_position = idx
                break
    payload['queue_position'] = queue_position
    return jsonify(payload)


@app.route('/api/download-file/<filename>')
def download_file(filename):
    fp = os.path.join(DOWNLOAD_DIR, filename)
    job_id, job = _find_job_by_filename(filename)
    if job and _is_job_expired(job):
        return jsonify({'error': 'Expired'}), 410
    if not os.path.exists(fp):
        # If we still have a job record, treat missing file as expired/cleaned.
        if job:
            return jsonify({'error': 'Expired'}), 410
        return jsonify({'error': 'File not found'}), 404
    return send_file(fp, as_attachment=True, download_name=filename)


@app.route('/api/download-zip/<job_id>')
def download_zip(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({'error': 'Not found'}), 404
    if _is_job_expired(job):
        return jsonify({'error': 'Expired'}), 410
    files = [f for f in job.get('files', []) if f]
    if not files:
        return jsonify({'error': 'No files'}), 404
    zip_buffer = io.BytesIO()
    added = 0
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in files:
            src = os.path.join(DOWNLOAD_DIR, fname)
            try:
                zf.write(src, fname)
                added += 1
            except FileNotFoundError:
                # File can disappear between list/check and zip write due to cleanup.
                continue
            except OSError:
                # Skip unreadable files but continue creating ZIP from valid files.
                continue
    if added == 0:
        return jsonify({'error': 'No files available for ZIP'}), 404
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name='playlist.zip', mimetype='application/zip')


@app.route('/api/cleanup/<job_id>', methods=['DELETE'])
def cleanup(job_id):
    with job_lock:
        job = jobs.get(job_id)
        if job:
            job['cancelled'] = True
            active_jobs.discard(job_id)

            # Remove job from waiting queue if still queued.
            remaining = deque()
            for item in pending_jobs:
                if item[0] != job_id:
                    remaining.append(item)
            pending_jobs.clear()
            pending_jobs.extend(remaining)

            _cleanup_job_record_and_files(job)

            # Remove job record (downloads will abort via `cancelled` flag).
            jobs.pop(job_id, None)
            _dispatch_jobs_locked()
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5500))
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
