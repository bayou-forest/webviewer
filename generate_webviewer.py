from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional

from flask import (
    Flask,
    abort,
    jsonify,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import safe_join

try:
    from PIL import Image
except ImportError:  # Pillow is optional; thumbnail generation degrades gracefully.
    Image = None


APP_ROOT = Path(__file__).resolve().parent
METADATA_ROOT = APP_ROOT / "_metadata"
MINUS_DIR = APP_ROOT / "_minus"
THUMB_DIR = METADATA_ROOT / "thumbnails"
PREVIEW_DIR = METADATA_ROOT / "previews"
DB_PATH = METADATA_ROOT / "ratings.sqlite3"
HASH_CACHE_PATH = METADATA_ROOT / "hash_cache.json"
VIDEO_INFO_PATH = METADATA_ROOT / "video_info.json"
LOG_FILE = METADATA_ROOT / "webviewer.log"
FFMPEG_BINARY = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BINARY = os.environ.get("FFPROBE_BIN", "ffprobe")
VIDEO_PREVIEW_COUNT = 8
PREVIEW_STEP_SECONDS = 2
PREVIEW_START_SECONDS = 10
THUMB_WIDTH = 360
HASH_CHUNK_SIZE = 4 * 1024 * 1024

# Configure logging
# Ensure the metadata directory exists so the log file can be opened.
METADATA_ROOT.mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    ]
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def log(message: str) -> None:
    logging.info(message)


def ensure_metadata_tree() -> None:
    if not METADATA_ROOT.exists():
        log("_metadata フォルダが存在しません。作成します。")
    METADATA_ROOT.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    ensure_metadata_tree()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                hash TEXT PRIMARY KEY,
                score INTEGER NOT NULL DEFAULT 0,
                play_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
            );
            """
        )
        # Migrate existing database: add play_count column if it doesn't exist
        cursor = conn.execute("PRAGMA table_info(ratings)")
        columns = [row[1] for row in cursor.fetchall()]
        if "play_count" not in columns:
            log("既存のデータベースに play_count カラムを追加しています...")
            conn.execute("ALTER TABLE ratings ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
            conn.commit()
            log("play_count カラムの追加が完了しました。")


init_db()


def load_json(path: Path) -> Dict[str, Dict[str, float]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return {}


def dump_json(path: Path, payload: Dict[str, Dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


HASH_CACHE_LOCK = threading.Lock()
HASH_CACHE = load_json(HASH_CACHE_PATH)
HASH_CACHE_DIRTY = False

VIDEO_INFO_LOCK = threading.Lock()
VIDEO_INFO_CACHE = load_json(VIDEO_INFO_PATH)
VIDEO_INFO_DIRTY = False


def compute_file_hash(path: Path) -> str:
    global HASH_CACHE_DIRTY
    stat = path.stat()
    cache_key = str(path)
    signature = f"{stat.st_mtime_ns}:{stat.st_size}"
    cached = HASH_CACHE.get(cache_key)
    if cached and cached.get("sig") == signature:
        return cached["hash"]

    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    file_hash = digest.hexdigest()
    with HASH_CACHE_LOCK:
        HASH_CACHE[cache_key] = {"sig": signature, "hash": file_hash}
        HASH_CACHE_DIRTY = True
    return file_hash


def flush_hash_cache() -> None:
    global HASH_CACHE_DIRTY
    if not HASH_CACHE_DIRTY:
        return
    with HASH_CACHE_LOCK:
        dump_json(HASH_CACHE_PATH, HASH_CACHE)
        HASH_CACHE_DIRTY = False


def flush_video_info_cache() -> None:
    global VIDEO_INFO_DIRTY
    if not VIDEO_INFO_DIRTY:
        return
    with VIDEO_INFO_LOCK:
        dump_json(VIDEO_INFO_PATH, VIDEO_INFO_CACHE)
        VIDEO_INFO_DIRTY = False


def generate_image_thumbnail(src: Path, dest: Path) -> None:
    if Image is None:
        dest.write_bytes(src.read_bytes())
        return
    with Image.open(src) as img:
        img.thumbnail((THUMB_WIDTH, THUMB_WIDTH))
        img.convert("RGB").save(dest, format="JPEG", quality=85)


def run_ffmpeg(args: List[str]) -> bool:
    try:
        result = subprocess.run(
            [FFMPEG_BINARY, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=60,
        )
        if result.stderr:
            log(result.stderr.decode(errors="ignore"))
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"ffmpeg 実行に失敗しました: {exc}")
        return False


def probe_video_duration(path: Path) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                FFPROBE_BINARY,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        )
        payload = json.loads(result.stdout.decode() or "{}")
        fmt = payload.get("format", {})
        raw = fmt.get("duration")
        if raw is None:
            return None
        return max(float(raw), 0.0)
    except Exception as exc:  # noqa: BLE001
        log(f"ffprobe で長さ取得に失敗: {exc}")
        return None


def get_video_duration(media_hash: str, path: Path) -> Optional[float]:
    global VIDEO_INFO_DIRTY
    with VIDEO_INFO_LOCK:
        cached = VIDEO_INFO_CACHE.get(media_hash)
        if cached and "duration" in cached:
            return cached["duration"]
    duration = probe_video_duration(path)
    if duration is not None:
        with VIDEO_INFO_LOCK:
            VIDEO_INFO_CACHE[media_hash] = {"duration": duration}
            VIDEO_INFO_DIRTY = True
    return duration


def generate_video_thumbnail(src: Path, dest: Path, offset: float) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    return run_ffmpeg(
        [
            "-y",
            "-ss",
            str(max(offset, 0.0)),
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            f"scale={THUMB_WIDTH}:-1",
            str(dest),
        ]
    )


def compute_thumbnail_offset(duration: Optional[float]) -> float:
    if duration and duration > 0:
        if duration < PREVIEW_START_SECONDS:
            return max(duration * 0.1, 0.0)
    return float(PREVIEW_START_SECONDS)


def compute_preview_offsets(duration: Optional[float]) -> List[float]:
    if not duration or duration <= 0:
        return [float(PREVIEW_START_SECONDS + PREVIEW_STEP_SECONDS * idx) for idx in range(VIDEO_PREVIEW_COUNT)]
    step = max(duration / (VIDEO_PREVIEW_COUNT + 1), 0.1)
    max_time = max(duration - 0.5, 0.0)
    offsets: List[float] = []
    for index in range(VIDEO_PREVIEW_COUNT):
        position = step * (index + 1)
        if position > max_time and max_time > 0:
            position = max_time
        offsets.append(position)
    return offsets


def ensure_thumbnails(path: Path, media_hash: str, is_video: bool, duration: Optional[float]) -> Dict[str, List[str]]:
    thumb_name = f"{media_hash}.jpg"
    thumb_path = THUMB_DIR / thumb_name
    preview_names: List[str] = []

    if not thumb_path.exists():
        if is_video:
            offset = compute_thumbnail_offset(duration)
            if not generate_video_thumbnail(path, thumb_path, offset):
                thumb_path.write_bytes(b"")
        else:
            generate_image_thumbnail(path, thumb_path)

    if is_video:
        for index, offset in enumerate(compute_preview_offsets(duration)):
            preview_name = f"{media_hash}_{index}.jpg"
            preview_path = PREVIEW_DIR / preview_name
            preview_names.append(preview_name)
            if preview_path.exists():
                continue
            if not generate_video_thumbnail(path, preview_path, offset):
                preview_path.write_bytes(b"")

    return {"thumbnail": thumb_name, "previews": preview_names}


def fetch_ratings() -> Dict[str, int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT hash, score FROM ratings").fetchall()
        return {row[0]: row[1] for row in rows}


def fetch_metadata() -> Dict[str, Dict[str, int]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT hash, score, play_count FROM ratings").fetchall()
        return {row[0]: {"score": row[1], "play_count": row[2]} for row in rows}


def update_rating(media_hash: str, delta: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT score FROM ratings WHERE hash=?", (media_hash,))
        hit = cursor.fetchone()
        if hit:
            score = hit[0] + delta
            conn.execute(
                "UPDATE ratings SET score=?, updated_at=strftime('%s','now') WHERE hash=?",
                (score, media_hash),
            )
        else:
            score = delta
            conn.execute(
                "INSERT INTO ratings(hash, score, updated_at) VALUES(?, ?, strftime('%s','now'))",
                (media_hash, score),
            )
        conn.commit()
    return score


def increment_play_count(media_hash: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT play_count FROM ratings WHERE hash=?", (media_hash,))
        hit = cursor.fetchone()
        if hit:
            count = hit[0] + 1
            conn.execute(
                "UPDATE ratings SET play_count=?, updated_at=strftime('%s','now') WHERE hash=?",
                (count, media_hash),
            )
        else:
            count = 1
            conn.execute(
                "INSERT INTO ratings(hash, play_count, updated_at) VALUES(?, ?, strftime('%s','now'))",
                (media_hash, count),
            )
        conn.commit()
    return count


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or not seconds > 0:
        return ''
    total = int(seconds)
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    else:
        return f"{mins}:{secs:02d}"


@dataclass
class MediaEntry:
    relative_path: str
    name: str
    media_hash: str
    media_type: str
    size: int
    modified: float
    thumbnail_name: str
    preview_names: List[str]
    rating: int
    duration: Optional[float]
    play_count: int = 0

    def serialize(self) -> Dict[str, object]:
        return {
            "relativePath": self.relative_path,
            "name": self.name,
            "hash": self.media_hash,
            "type": self.media_type,
            "size": self.size,
            "modified": self.modified,
            "thumbnailUrl": url_for("serve_thumbnail", filename=self.thumbnail_name),
            "previewUrls": [
                url_for("serve_preview", filename=name) for name in self.preview_names
            ] if self.preview_names else [],
            "rating": self.rating,
            "duration": self.duration,
            "formatted_duration": format_duration(self.duration),
            "playCount": self.play_count,
            "viewUrl": url_for("view_media", media_path=self.relative_path),
            "mediaUrl": url_for("serve_media", media_path=self.relative_path),
        }


MEDIA_CACHE: List[MediaEntry] = []
MEDIA_LOOKUP: Dict[str, MediaEntry] = {}
MEDIA_LOCK = threading.Lock()
SCAN_METADATA: Dict[str, object] = {}


def iter_media_files() -> List[Path]:
    entries: List[Path] = []
    for root, dirs, files in os.walk(APP_ROOT):
        root_path = Path(root)
        if METADATA_ROOT in root_path.parents or root_path == METADATA_ROOT:
            dirs[:] = []
            continue
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith('.') and d.lower() != '_metadata'
            ]
        for filename in files:
            if filename.startswith("."):
                continue
            path = root_path / filename
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                entries.append(path)
    entries.sort()
    return entries


def refresh_media_index() -> Dict[str, int]:
    log("メディアファイルを走査しています...")
    metadata_map = fetch_metadata()
    files = iter_media_files()
    new_entries: List[MediaEntry] = []

    for path in files:
        media_type = "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "image"
        media_hash = compute_file_hash(path)
        relative = path.relative_to(APP_ROOT).as_posix()
        duration = get_video_duration(media_hash, path) if media_type == "video" else None
        thumbs = ensure_thumbnails(path, media_hash, media_type == "video", duration)
        metadata = metadata_map.get(media_hash, {"score": 0, "play_count": 0})
        stat = path.stat()
        entry = MediaEntry(
            relative_path=relative,
            name=path.name,
            media_hash=media_hash,
            media_type=media_type,
            size=stat.st_size,
            modified=stat.st_mtime,
            thumbnail_name=thumbs["thumbnail"],
            preview_names=thumbs["previews"] if media_type == "video" else [],
            rating=metadata.get("score", 0),
            duration=duration,
            play_count=metadata.get("play_count", 0),
        )
        new_entries.append(entry)

    with MEDIA_LOCK:
        MEDIA_CACHE[:] = new_entries
        MEDIA_LOOKUP.clear()
        for entry in MEDIA_CACHE:
            MEDIA_LOOKUP[entry.relative_path] = entry
        SCAN_METADATA.update(
            {
                "lastScan": datetime.now().isoformat(),
                "total": len(MEDIA_CACHE),
                "videos": sum(1 for e in MEDIA_CACHE if e.media_type == "video"),
                "images": sum(1 for e in MEDIA_CACHE if e.media_type == "image"),
            }
        )

    flush_hash_cache()
    flush_video_info_cache()
    log(f"走査完了: {len(new_entries)} 件")
    return {
        "total": len(new_entries),
        "videos": SCAN_METADATA.get("videos", 0),
        "images": SCAN_METADATA.get("images", 0),
    }


refresh_media_index()


app = Flask(__name__)


def filter_entries(include_subfolders: bool, rating_filter: str, play_count_filter: str = "all") -> List[Dict[str, object]]:
    with MEDIA_LOCK:
        entries = list(MEDIA_CACHE)
    filtered: List[MediaEntry] = []
    for entry in entries:
        if not include_subfolders and "/" in entry.relative_path:
            continue
        if rating_filter == "positive" and entry.rating <= 0:
            continue
        elif rating_filter == "non_negative" and entry.rating < 0:
            continue
        if play_count_filter == "zero" and entry.play_count != 0:
            continue
        elif play_count_filter == "non_zero" and entry.play_count == 0:
            continue
        filtered.append(entry)
    return [entry.serialize() for entry in filtered]


@app.route("/")
def index() -> str:
    return render_template_string(INDEX_TEMPLATE, scan_info=SCAN_METADATA)


@app.route("/api/files")
def api_files() -> "flask.Response":
    include_subfolders = request.args.get("includeSubfolders", "true").lower() == "true"
    rating_filter = request.args.get("ratingFilter", "all")
    play_count_filter = request.args.get("playCountFilter", "all")
    data = filter_entries(include_subfolders, rating_filter, play_count_filter)
    return jsonify({"media": data, "scan": SCAN_METADATA})


@app.route("/api/refresh", methods=["POST"])
def api_refresh() -> "flask.Response":
    stats = refresh_media_index()
    return jsonify({"status": "ok", "stats": stats})


@app.route("/api/rate", methods=["POST"])
def api_rate() -> "flask.Response":
    payload = request.get_json(force=True)
    media_hash = payload.get("hash")
    delta = int(payload.get("delta", 0))
    if media_hash is None or delta not in (1, -1):
        abort(400, "hash と delta (±1) が必要です")
    new_score = update_rating(media_hash, delta)
    with MEDIA_LOCK:
        for entry in MEDIA_CACHE:
            if entry.media_hash == media_hash:
                entry.rating = new_score
    return jsonify({"hash": media_hash, "rating": new_score})


@app.route("/api/play", methods=["POST"])
def api_play() -> "flask.Response":
    payload = request.get_json(force=True)
    media_hash = payload.get("hash")
    if media_hash is None:
        abort(400, "hash が必要です")
    new_count = increment_play_count(media_hash)
    with MEDIA_LOCK:
        for entry in MEDIA_CACHE:
            if entry.media_hash == media_hash:
                entry.play_count = new_count
    return jsonify({"hash": media_hash, "playCount": new_count})


@app.route("/api/low-rated")
def api_low_rated() -> "flask.Response":
    threshold = int(request.args.get("threshold", 0))
    with MEDIA_LOCK:
        paths = [entry.relative_path for entry in MEDIA_CACHE if entry.rating < threshold]
    return jsonify({"count": len(paths), "paths": paths})


@app.route("/api/move-negative", methods=["POST"])
def api_move_negative() -> "flask.Response":
    """マイナス評価のファイルを _minus ディレクトリに移動する"""
    MINUS_DIR.mkdir(parents=True, exist_ok=True)
    
    moved_files: List[str] = []
    failed_files: List[str] = []
    
    with MEDIA_LOCK:
        negative_entries = [entry for entry in MEDIA_CACHE if entry.rating < 0]
    
    for entry in negative_entries:
        src_path = APP_ROOT / entry.relative_path
        if not src_path.exists():
            failed_files.append(entry.relative_path)
            continue
        
        # 移動先のパスを構築（サブフォルダ構造を保持）
        dest_path = MINUS_DIR / entry.relative_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            import shutil
            shutil.move(str(src_path), str(dest_path))
            moved_files.append(entry.relative_path)
            log(f"移動完了: {entry.relative_path} -> {dest_path}")
        except Exception as exc:
            log(f"移動失敗: {entry.relative_path} - {exc}")
            failed_files.append(entry.relative_path)
    
    # インデックスを更新
    refresh_media_index()
    
    return jsonify({
        "status": "ok",
        "moved": len(moved_files),
        "failed": len(failed_files),
        "moved_files": moved_files,
        "failed_files": failed_files,
    })


@app.route("/thumbnails/<path:filename>")
def serve_thumbnail(filename: str):
    return send_from_directory(THUMB_DIR, filename)


@app.route("/previews/<path:filename>")
def serve_preview(filename: str):
    return send_from_directory(PREVIEW_DIR, filename)


@app.route("/media/<path:media_path>")
def serve_media(media_path: str):
    safe_path = safe_join(str(APP_ROOT), media_path)
    if safe_path is None or not Path(safe_path).exists():
        abort(404)
    directory = str(Path(safe_path).parent)
    filename = Path(safe_path).name
    return send_from_directory(directory, filename)


@app.route("/view/<path:media_path>")
def view_media(media_path: str):
    with MEDIA_LOCK:
        entry = MEDIA_LOOKUP.get(media_path)
    if not entry:
        abort(404)
    return render_template_string(VIEW_TEMPLATE, entry=entry.serialize())


INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang=\"ja\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Media Viewer</title>
    <style>
        :root {
            color-scheme: dark;
            font-family: \"Segoe UI\", sans-serif;
            background: #13151a;
            color: #f5f5f5;
            font-size: 80%;
        }
        body {
            margin: 0;
            padding: 1rem;
        }
        header {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem 1rem;
            align-items: center;
            margin-bottom: 1rem;
        }
        button, label {
            font-size: 0.95rem;
        }
        .controls {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            align-items: center;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 1rem;
        }
        .card {
            background: #1d212b;
            border-radius: 12px;
            padding: 0.75rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            box-shadow: 0 6px 12px rgba(0,0,0,0.35);
        }
        .card.unplayed {
            background: #2a3040;
        }
        .thumb-wrapper {
            position: relative;
            padding-top: 56%;
            background: #000;
            border-radius: 8px;
            overflow: hidden;
        }
        .thumb-wrapper img {
            position: absolute;
            inset: 0;
            width: 100%;
            height: 100%;
            object-fit: contain;
            transition: opacity 0.25s ease;
        }
        .meta {
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }
        .meta .title {
            font-weight: 600;
            font-size: 1rem;
        }
        .meta small {
            color: #cacaca;
            word-break: break-all;
        }
        .actions {
            display: flex;
            gap: 0.4rem;
            flex-wrap: wrap;
        }
        .actions button, .actions a {
            border: none;
            border-radius: 999px;
            padding: 0.35rem 0.75rem;
            cursor: pointer;
            background: #3b82f6;
            color: #fff;
            text-decoration: none;
            font-weight: 600;
        }
        .actions button.danger {
            background: #f03a5f;
        }
        .controls button.danger {
            background: #f03a5f;
            color: #fff;
        }
        textarea {
            width: 100%;
            min-height: 120px;
            resize: vertical;
            margin-top: 0.5rem;
            background: #0f1218;
            color: #fff;
            border-radius: 8px;
            border: 1px solid #2f3440;
            padding: 0.75rem;
        }
        @media (max-width: 640px) {
            :root {
                font-size: 70%;
            }
            .grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }
    </style>
</head>
<body>
    <header id="top">
        <h1 style=\"margin:0\">ローカルメディアビューア</h1>
        <div id=\"anchors\"></div>
        <div class=\"controls\">
            <label>
                <input type=\"checkbox\" id=\"includeSubfolders\" checked>
                サブフォルダを含める
            </label>
            <label>
                評価フィルタ:
                <select id=\"ratingFilter\">
                    <option value=\"all\">すべて表示</option>
                    <option value=\"positive\">高評価のみ</option>
                    <option value=\"non_negative\">マイナス評価を表示しない</option>
                </select>
            </label>            <label>
                再生回数フィルタ:
                <select id="playCountFilter">
                    <option value="all">すべて表示</option>
                    <option value="zero">再生回数0</option>
                    <option value="non_zero">再生回数0以上</option>
                </select>
            </label>            <button id=\"refreshBtn\">再探索</button>
            <button id=\"lowRatedBtn\">低評価リスト出力</button>
            <button id=\"moveNegativeBtn\" class=\"danger\">マイナス評価を_minusへ移動</button>
        </div>
        <div id=\"scanInfo\"></div>
    </header>
    <main>
        <section class=\"grid\" id=\"mediaGrid\"></section>
        <textarea id=\"lowRatedOutput\" placeholder=\"低評価リストはここに出力されます\" readonly></textarea>
    </main>
    <template id=\"cardTemplate\">
        <article class=\"card\">
            <div class=\"thumb-wrapper\">
                <img loading=\"lazy\">
            </div>
            <div class=\"meta\">
                <strong class=\"title\"></strong>
                <small class=\"info\"></small>
                <small class=\"rating\"></small>
            </div>
            <div class=\"actions\">
                <button class=\"rate-up\">👍 +1</button>
                <button class=\"rate-down danger\">👎 -1</button>
                <a class=\"view-link\" target=\"_blank\">詳細</a>
            </div>
        </article>
    </template>
    <script>
        const grid = document.getElementById('mediaGrid');
        const cardTemplate = document.getElementById('cardTemplate');
        const includeSubfolders = document.getElementById('includeSubfolders');
        const ratingFilter = document.getElementById('ratingFilter');
        const playCountFilter = document.getElementById('playCountFilter');
        const refreshBtn = document.getElementById('refreshBtn');
        const lowRatedBtn = document.getElementById('lowRatedBtn');
        const moveNegativeBtn = document.getElementById('moveNegativeBtn');
        const lowRatedOutput = document.getElementById('lowRatedOutput');
        const scanInfo = document.getElementById('scanInfo');
        const anchors = document.getElementById('anchors');
        const hoverTimers = new WeakMap();

        const formatBytes = (bytes) => {
            if (!bytes) return '0 B';
            const units = ['B','KB','MB','GB','TB'];
            let idx = 0;
            let value = bytes;
            while (value >= 1024 && idx < units.length - 1) {
                value /= 1024;
                idx++;
            }
            return `${value.toFixed(1)} ${units[idx]}`;
        };

        const formatDuration = (seconds) => {
            if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) {
                return '';
            }
            const total = Math.floor(seconds);
            const hrs = Math.floor(total / 3600);
            const mins = Math.floor((total % 3600) / 60);
            const secs = total % 60;
            const pad = (value) => value.toString().padStart(2, '0');
            return hrs > 0 ? `${hrs}:${pad(mins)}:${pad(secs)}` : `${mins}:${pad(secs)}`;
        };

        async function loadMedia() {
            const params = new URLSearchParams({
                includeSubfolders: includeSubfolders.checked,
                ratingFilter: ratingFilter.value,
                playCountFilter: playCountFilter.value,
            });
            const response = await fetch(`/api/files?${params}`);
            const data = await response.json();
            renderMedia(data.media);
            renderScanInfo(data.scan);
        }

        function renderScanInfo(info) {
            if (!info) return;
            scanInfo.textContent = `最終更新: ${info.lastScan || '-'} / ファイル総数: ${info.total || 0} (動画 ${info.videos || 0}, 画像 ${info.images || 0})`;
        }

        function renderMedia(items) {
            grid.innerHTML = '';
            anchors.innerHTML = '';
            let anchorIndex = 1;
            items.forEach((item, index) => {
                if (index > 0 && index % 20 === 0) {
                    const anchor = document.createElement('div');
                    anchor.id = `anchor-${anchorIndex}`;
                    anchor.innerHTML = `<a href="#top">TOPに戻る</a>`;
                    grid.appendChild(anchor);
                    // ページ冒頭にリンク追加
                    const link = document.createElement('a');
                    link.href = `#anchor-${anchorIndex}`;
                    link.textContent = `${index + 1}件目`;
                    anchors.appendChild(link);
                    anchors.appendChild(document.createTextNode(' '));
                    anchorIndex++;
                }
                const card = cardTemplate.content.firstElementChild.cloneNode(true);
                const img = card.querySelector('img');
                img.src = item.thumbnailUrl;
                img.dataset.thumbnail = item.thumbnailUrl;
                img.dataset.type = item.type;
                img.dataset.preview = (item.previewUrls || []).join('|');
                img.addEventListener('mouseenter', handleHoverStart);
                img.addEventListener('mouseleave', handleHoverEnd);

                // Mark unplayed items
                if (item.playCount === 0) {
                    card.classList.add('unplayed');
                }

                const titleEl = card.querySelector('.title');
                titleEl.textContent = item.name;
                titleEl.title = item.relativePath;
                const infoParts = [`${item.type}`, formatBytes(item.size)];
                if (item.type === 'video' && typeof item.duration === 'number') {
                    const formatted = formatDuration(item.duration);
                    if (formatted) infoParts.push(formatted);
                }
                card.querySelector('.info').textContent = infoParts.filter(Boolean).join(' / ');
                card.querySelector('.rating').textContent = `評価: ${item.rating} / 再生: ${item.playCount}回`;

                card.querySelector('.rate-up').addEventListener('click', () => vote(item, 1, card));
                card.querySelector('.rate-down').addEventListener('click', () => vote(item, -1, card));
                card.querySelector('.view-link').href = item.viewUrl;

                grid.appendChild(card);
            });
        }

        async function vote(item, delta, card) {
            const response = await fetch('/api/rate', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({hash: item.hash, delta}),
            });
            if (!response.ok) return;
            const payload = await response.json();
            item.rating = payload.rating;
            card.querySelector('.rating').textContent = `評価: ${item.rating}`;
        }

        function handleHoverStart(event) {
            const img = event.currentTarget;
            const previews = (img.dataset.preview || '').split('|').filter(Boolean);
            if (!previews.length) return;
            let index = 0;
            hoverTimers.set(img, setInterval(() => {
                img.src = previews[index % previews.length];
                index++;
            }, 500));
        }

        function handleHoverEnd(event) {
            const img = event.currentTarget;
            clearInterval(hoverTimers.get(img));
            hoverTimers.delete(img);
            img.src = img.dataset.thumbnail;
        }

        async function refreshIndex() {
            refreshBtn.disabled = true;
            await fetch('/api/refresh', {method: 'POST'});
            await loadMedia();
            refreshBtn.disabled = false;
        }

        async function fetchLowRated() {
            const response = await fetch('/api/low-rated?threshold=0');
            const data = await response.json();
            lowRatedOutput.value = data.paths.join('\\n');
        }

        async function moveNegativeFiles() {
            if (!confirm('マイナス評価のファイルを _minus ディレクトリに移動します。よろしいですか？')) {
                return;
            }
            moveNegativeBtn.disabled = true;
            moveNegativeBtn.textContent = '移動中...';
            try {
                const response = await fetch('/api/move-negative', {method: 'POST'});
                const data = await response.json();
                alert(`移動完了: ${data.moved}件\\n失敗: ${data.failed}件`);
                await loadMedia();
            } catch (err) {
                alert('移動処理でエラーが発生しました');
                console.error(err);
            } finally {
                moveNegativeBtn.disabled = false;
                moveNegativeBtn.textContent = 'マイナス評価を_minusへ移動';
            }
        }

        includeSubfolders.addEventListener('change', loadMedia);
        ratingFilter.addEventListener('change', loadMedia);
        playCountFilter.addEventListener('change', loadMedia);
        refreshBtn.addEventListener('click', refreshIndex);
        lowRatedBtn.addEventListener('click', fetchLowRated);
        moveNegativeBtn.addEventListener('click', moveNegativeFiles);

        loadMedia();
    </script>
</body>

</html>
"""


VIEW_TEMPLATE = """
<!DOCTYPE html>
<html lang=\"ja\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>{{ entry.name }} - Media Viewer</title>
    <style>
        body { margin: 0; background: #0d0f14; color: #fff; font-family: 'Segoe UI', sans-serif; font-size: 0.9rem; }
        header { padding: 1rem; max-width: 960px; margin: 0 auto; }
        header .title { margin: 0; font-size: 1.4rem; font-weight: 600; }
        main { display: flex; justify-content: center; padding: 1rem; }
        .viewer { max-width: 90vw; }
        video, img { max-width: 100%; height: auto; }
        .meta { display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; margin-top: 0.5rem; }
        .meta a { color: #9cc9ff; }
        .actions { display: flex; gap: 0.5rem; margin-top: 1rem; flex-wrap: wrap; }
        .actions button { border: none; border-radius: 999px; padding: 0.5rem 1rem; cursor: pointer; background: #3b82f6; color: #fff; font-weight: 600; font-size: 0.9rem; }
        .actions button.danger { background: #f03a5f; }
    </style>
</head>
<body>
    <header>
        <h1 class=\"title\" title=\"{{ entry.relativePath }}\">{{ entry.name }}</h1>
        <div class=\"meta\">
            {% if entry.formatted_duration %}
            <span>長さ: {{ entry.formatted_duration }}</span>
            {% endif %}
            <span id=\"rating\">評価: {{ entry.rating }}</span>
            <span id=\"playCount\">再生回数: {{ entry.playCount }}回</span>
            <a href=\"/\">一覧へ戻る</a>
        </div>
        <div class=\"actions\">
            <button id=\"rateUp\">👍 高評価 (+1)</button>
            <button id=\"rateDown\" class=\"danger\">👎 低評価 (-1)</button>
        </div>
    </header>
    <main>
        <div class=\"viewer\">
            {% if entry.type == 'video' %}
            <video id=\"videoPlayer\" controls preload=\"metadata\" src=\"{{ entry.mediaUrl }}\"></video>
            {% else %}
            <img src=\"{{ entry.mediaUrl }}\" alt=\"{{ entry.name }}\">
            {% endif %}
        </div>
    </main>
    <script>
        const hash = '{{ entry.hash }}';
        let playCounted = false;

        async function vote(delta) {
            try {
                const response = await fetch('/api/rate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({hash, delta}),
                });
                if (response.ok) {
                    const data = await response.json();
                    document.getElementById('rating').textContent = `評価: ${data.rating}`;
                }
            } catch (err) {
                console.error('評価の更新に失敗:', err);
            }
        }

        async function countPlay() {
            if (playCounted) return;
            playCounted = true;
            try {
                const response = await fetch('/api/play', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({hash}),
                });
                if (response.ok) {
                    const data = await response.json();
                    document.getElementById('playCount').textContent = `再生回数: ${data.playCount}回`;
                }
            } catch (err) {
                console.error('再生回数の更新に失敗:', err);
            }
        }

        document.getElementById('rateUp').addEventListener('click', () => vote(1));
        document.getElementById('rateDown').addEventListener('click', () => vote(-1));

        const video = document.getElementById('videoPlayer');
        if (video) {
            video.addEventListener('play', countPlay, { once: false });
        }
    </script>
</body>
</html>
"""


def main() -> None:
    host = os.environ.get("MEDIA_VIEWER_HOST", "0.0.0.0")
    port = int(os.environ.get("MEDIA_VIEWER_PORT", "8080"))
    log(f"サーバーを起動します: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("停止します。")
