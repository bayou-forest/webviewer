from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
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
except ImportError:  # Pillow is optional; copy original file if missing.
	Image = None


APP_ROOT = Path(__file__).resolve().parent
METADATA_ROOT = APP_ROOT / "_metadata"
THUMB_DIR = METADATA_ROOT / "thumbnails"
PREVIEW_DIR = METADATA_ROOT / "previews"
DB_PATH = METADATA_ROOT / "ratings.sqlite3"
HASH_CACHE_PATH = METADATA_ROOT / "hash_cache.json"
VIDEO_INFO_PATH = METADATA_ROOT / "video_info.json"
FFMPEG_BINARY = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BINARY = os.environ.get("FFPROBE_BIN", "ffprobe")
VIDEO_PREVIEW_COUNT = 6
PREVIEW_STEP_SECONDS = 2
PREVIEW_START_SECONDS = 10
THUMB_WIDTH = 360
HASH_CHUNK_SIZE = 4 * 1024 * 1024

IMAGE_EXTENSIONS = {
	".jpg",
	".jpeg",
	".png",
	".gif",
	".bmp",
	".webp",
}
VIDEO_EXTENSIONS = {
	".mp4",
	".mkv",
	".mov",
	".avi",
	".webm",
	".m4v",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def log(message: str) -> None:
	timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	print(f"[{timestamp}] {message}")


def ensure_metadata_tree() -> None:
	if not METADATA_ROOT.exists():
		log("_metadata フォルダが存在しません。作成します。")
		METADATA_ROOT.mkdir(parents=True, exist_ok=True)
	for directory in (THUMB_DIR, PREVIEW_DIR):
		directory.mkdir(parents=True, exist_ok=True)
	if not DB_PATH.exists():
		log("評価データベースが存在しません。初期化します。")


ensure_metadata_tree()


def init_db() -> None:
	with sqlite3.connect(DB_PATH) as conn:
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS ratings (
				hash TEXT PRIMARY KEY,
				score INTEGER NOT NULL DEFAULT 0,
				updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
			);
			"""
		)


init_db()


def load_hash_cache() -> Dict[str, Dict[str, float]]:
	if not HASH_CACHE_PATH.exists():
		return {}
	try:
		with HASH_CACHE_PATH.open("r", encoding="utf-8") as handle:
			return json.load(handle)
	except json.JSONDecodeError:
		return {}


def save_hash_cache(cache: Dict[str, Dict[str, float]]) -> None:
	with HASH_CACHE_PATH.open("w", encoding="utf-8") as handle:
		json.dump(cache, handle, ensure_ascii=False, indent=2)


HASH_CACHE_LOCK = threading.Lock()
HASH_CACHE = load_hash_cache()
HASH_CACHE_DIRTY = False


def load_video_info_cache() -> Dict[str, Dict[str, float]]:
	if not VIDEO_INFO_PATH.exists():
		return {}
	try:
		with VIDEO_INFO_PATH.open("r", encoding="utf-8") as handle:
			return json.load(handle)
	except json.JSONDecodeError:
		return {}


def save_video_info_cache(cache: Dict[str, Dict[str, float]]) -> None:
	with VIDEO_INFO_PATH.open("w", encoding="utf-8") as handle:
		json.dump(cache, handle, ensure_ascii=False, indent=2)


VIDEO_INFO_LOCK = threading.Lock()
VIDEO_INFO_CACHE = load_video_info_cache()
VIDEO_INFO_DIRTY = False


def compute_file_hash(path: Path) -> str:
	global HASH_CACHE_DIRTY
	stat = path.stat()
	cache_key = str(path)
	cached = HASH_CACHE.get(cache_key)
	sig = f"{stat.st_mtime_ns}:{stat.st_size}"
	if cached and cached.get("sig") == sig:
		return cached["hash"]

	digest = sha256()
	with path.open("rb") as handle:
		while True:
			chunk = handle.read(HASH_CHUNK_SIZE)
			if not chunk:
				break
			digest.update(chunk)
	hex_hash = digest.hexdigest()
	HASH_CACHE[cache_key] = {"sig": sig, "hash": hex_hash}
	HASH_CACHE_DIRTY = True
	return hex_hash


def flush_hash_cache_if_needed() -> None:
	global HASH_CACHE_DIRTY
	if HASH_CACHE_DIRTY:
		with HASH_CACHE_LOCK:
			save_hash_cache(HASH_CACHE)
			HASH_CACHE_DIRTY = False


def flush_video_info_cache_if_needed() -> None:
	global VIDEO_INFO_DIRTY
	if VIDEO_INFO_DIRTY:
		with VIDEO_INFO_LOCK:
			save_video_info_cache(VIDEO_INFO_CACHE)
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
		duration_raw = fmt.get("duration")
		if duration_raw is None:
			return None
		return max(float(duration_raw), 0.0)
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
	return run_ffmpeg([
		"-y",
		"-ss",
		str(offset),
		"-i",
		str(src),
		"-frames:v",
		"1",
		"-vf",
		f"scale={THUMB_WIDTH}:-1",
		str(dest),
	])


def compute_thumbnail_offset(duration: Optional[float]) -> float:
	if not duration or duration <= 0:
		return float(PREVIEW_START_SECONDS)
	if duration < PREVIEW_START_SECONDS:
		return max(duration * 0.1, 0.0)
	return float(PREVIEW_START_SECONDS)


def compute_preview_offsets(duration: Optional[float]) -> List[float]:
	if not duration or duration <= 0:
		return [float(PREVIEW_START_SECONDS + PREVIEW_STEP_SECONDS * idx) for idx in range(VIDEO_PREVIEW_COUNT)]
	step = max(duration / max(VIDEO_PREVIEW_COUNT, 1), 0.1)
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
		offsets = compute_preview_offsets(duration)
		for index, offset in enumerate(offsets):
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
		cursor = conn.execute("SELECT hash, score FROM ratings")
		return {row[0]: row[1] for row in cursor.fetchall()}


def update_rating(media_hash: str, delta: int) -> int:
	with sqlite3.connect(DB_PATH) as conn:
		cursor = conn.execute("SELECT score FROM ratings WHERE hash=?", (media_hash,))
		row = cursor.fetchone()
		if row:
			score = row[0] + delta
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
			"viewUrl": url_for("view_media", media_path=self.relative_path),
			"mediaUrl": url_for("serve_media", media_path=self.relative_path),
		}


MEDIA_CACHE: List[MediaEntry] = []
MEDIA_LOOKUP: Dict[str, MediaEntry] = {}
MEDIA_LOCK = threading.Lock()
SCAN_METADATA: Dict[str, object] = {}


def iter_media_files() -> List[Path]:
	files: List[Path] = []
	for root, dirs, filenames in os.walk(APP_ROOT):
		root_path = Path(root)
		if METADATA_ROOT in root_path.parents or root_path == METADATA_ROOT:
			dirs[:] = []
			continue
		dirs[:] = [d for d in dirs if not d.startswith(".")]
		for filename in filenames:
			if filename.startswith("."):
				continue
			path = root_path / filename
			if path.suffix.lower() in MEDIA_EXTENSIONS:
				files.append(path)
	files.sort()
	return files


def refresh_media_index() -> Dict[str, int]:
	log("メディアファイルを走査しています...")
	rating_map = fetch_ratings()
	files = iter_media_files()
	new_entries: List[MediaEntry] = []
	for path in files:
		media_type = "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "image"
		media_hash = compute_file_hash(path)
		rel = path.relative_to(APP_ROOT).as_posix()
		duration = get_video_duration(media_hash, path) if media_type == "video" else None
		thumbs = ensure_thumbnails(path, media_hash, media_type == "video", duration)
		rating = rating_map.get(media_hash, 0)
		entry = MediaEntry(
			relative_path=rel,
			name=path.name,
			media_hash=media_hash,
			media_type=media_type,
			size=path.stat().st_size,
			modified=path.stat().st_mtime,
			thumbnail_name=thumbs["thumbnail"],
			preview_names=thumbs["previews"] if media_type == "video" else [],
			rating=rating,
			duration=duration,
		)
		new_entries.append(entry)

	with MEDIA_LOCK:
		MEDIA_CACHE.clear()
		MEDIA_CACHE.extend(new_entries)
		MEDIA_LOOKUP.clear()
		for entry in MEDIA_CACHE:
			MEDIA_LOOKUP[entry.relative_path] = entry
		SCAN_METADATA["lastScan"] = datetime.now().isoformat()
		SCAN_METADATA["total"] = len(MEDIA_CACHE)
		SCAN_METADATA["videos"] = sum(1 for e in MEDIA_CACHE if e.media_type == "video")
		SCAN_METADATA["images"] = sum(1 for e in MEDIA_CACHE if e.media_type == "image")

	flush_hash_cache_if_needed()
	flush_video_info_cache_if_needed()
	log(f"走査完了: {len(new_entries)} 件")
	return {
		"total": len(new_entries),
		"videos": SCAN_METADATA["videos"],
		"images": SCAN_METADATA["images"],
	}


refresh_media_index()


app = Flask(__name__)


def filter_entries(include_subfolders: bool, favorites_only: bool) -> List[Dict[str, object]]:
	with MEDIA_LOCK:
		entries = list(MEDIA_CACHE)
	filtered: List[MediaEntry] = []
	for entry in entries:
		if not include_subfolders and "/" in entry.relative_path:
			continue
		if favorites_only and entry.rating <= 0:
			continue
		filtered.append(entry)
	return [entry.serialize() for entry in filtered]


@app.route("/")
def index() -> str:
	return render_template_string(
		INDEX_TEMPLATE,
		scan_info=SCAN_METADATA,
	)


@app.route("/api/files")
def api_files() -> "flask.Response":
	include_subfolders = request.args.get("includeSubfolders", "true").lower() == "true"
	favorites_only = request.args.get("favoritesOnly", "false").lower() == "true"
	data = filter_entries(include_subfolders, favorites_only)
	return jsonify({
		"media": data,
		"scan": SCAN_METADATA,
	})


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
		abort(400, "hash and delta(±1) が必要です")
	new_score = update_rating(media_hash, delta)
	with MEDIA_LOCK:
		for entry in MEDIA_CACHE:
			if entry.media_hash == media_hash:
				entry.rating = new_score
	return jsonify({"hash": media_hash, "rating": new_score})


@app.route("/api/low-rated")
def api_low_rated() -> "flask.Response":
	threshold = int(request.args.get("threshold", 0))
	paths: List[str] = []
	with MEDIA_LOCK:
		for entry in MEDIA_CACHE:
			if entry.rating < threshold:
				paths.append(entry.relative_path)
	return jsonify({"count": len(paths), "paths": paths})


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
	return render_template_string(
		VIEW_TEMPLATE,
		entry=entry.serialize(),
	)


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
			font-family: "Segoe UI", sans-serif;
			background: #13151a;
			color: #f5f5f5;
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
	</style>
</head>
<body>
	<header>
		<h1 style=\"margin:0\">ローカルメディアビューア</h1>
		<div class=\"controls\">
			<label>
				<input type=\"checkbox\" id=\"includeSubfolders\" checked>
				サブフォルダを含める
			</label>
			<label>
				<input type=\"checkbox\" id=\"favoritesOnly\">
				高評価のみ表示
			</label>
			<button id=\"refreshBtn\">再探索</button>
			<button id=\"lowRatedBtn\">低評価リスト出力</button>
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
		const favoritesOnly = document.getElementById('favoritesOnly');
		const refreshBtn = document.getElementById('refreshBtn');
		const lowRatedBtn = document.getElementById('lowRatedBtn');
		const lowRatedOutput = document.getElementById('lowRatedOutput');
		const scanInfo = document.getElementById('scanInfo');
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
				favoritesOnly: favoritesOnly.checked,
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
			items.forEach(item => {
				const card = cardTemplate.content.firstElementChild.cloneNode(true);
				const img = card.querySelector('img');
				img.src = item.thumbnailUrl;
				img.dataset.thumbnail = item.thumbnailUrl;
				img.dataset.type = item.type;
				img.dataset.preview = (item.previewUrls || []).join('|');
				img.addEventListener('mouseenter', handleHoverStart);
				img.addEventListener('mouseleave', handleHoverEnd);

				const titleEl = card.querySelector('.title');
				titleEl.textContent = item.name;
				titleEl.title = item.relativePath;
				const infoParts = [`${item.type}`, formatBytes(item.size)];
				if (item.type === 'video' && typeof item.duration === 'number') {
					const formatted = formatDuration(item.duration);
					if (formatted) infoParts.push(formatted);
				}
				card.querySelector('.info').textContent = infoParts.filter(Boolean).join(' / ');
				card.querySelector('.rating').textContent = `評価: ${item.rating}`;

				card.querySelector('.rate-up').addEventListener('click', () => vote(item, 1, card));
				card.querySelector('.rate-down').addEventListener('click', () => vote(item, -1, card));
				const viewLink = card.querySelector('.view-link');
				viewLink.href = item.viewUrl;

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

		includeSubfolders.addEventListener('change', loadMedia);
		favoritesOnly.addEventListener('change', loadMedia);
		refreshBtn.addEventListener('click', refreshIndex);
		lowRatedBtn.addEventListener('click', fetchLowRated);

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
		body { margin: 0; background: #0d0f14; color: #fff; font-family: 'Segoe UI', sans-serif; }
		header { padding: 1rem; }
		main { display: flex; justify-content: center; padding: 1rem; }
		.viewer { max-width: 90vw; }
		video, img { max-width: 100%; height: auto; }
		a { color: #9cc9ff; }
	</style>
</head>
<body>
	<header>
		<h1 style=\"margin:0\">{{ entry.name }}</h1>
		<p>{{ entry.relativePath }}</p>
		<p>評価: {{ entry.rating }}</p>
		<p><a href=\"/\">一覧へ戻る</a></p>
	</header>
	<main>
		<div class=\"viewer\">
			{% if entry.type == 'video' %}
			<video controls preload=\"metadata\" src=\"{{ entry.mediaUrl }}\"></video>
			{% else %}
			<img src=\"{{ entry.mediaUrl }}\" alt=\"{{ entry.name }}\">
			{% endif %}
		</div>
	</main>
</body>
</html>
"""


def main() -> None:
	host = os.environ.get("MEDIA_VIEWER_HOST", "0.0.0.0")
	port = int(os.environ.get("MEDIA_VIEWER_PORT", "5000"))
	log(f"サーバーを起動します: http://{host}:{port}")
	app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
		log("停止します。")


# 以下の仕様を満たすwebアプリケーションプログラムを作成してください。
# 
# 要件
# 1枚のページからなるwebアプリケーションである。
# アプリケーションルート以下にある、画像・動画ファイルを一覧で表示し、スマホ端末、PC端末からアクセスし、表示・再生できる。
# アプリケーションルート以下のフォルダを再探索するための更新ボタンを用意する。
# サブフォルダ内のファイルも表示するかどうかは、チェックボックスで指定できる。
# 動画、画像ファイルはサムネイルが表示される。
# 動画にマウスオーバーすると、サムネイルのサイズで、動画のキーフレームが順次表示される。（部分が順次表示されれば、キーフレームでなくてもいい。）
# 初期表示では、サムネイルのみが表示され、動画のデータは端末に読み込まれない。
# ローカルネットワークにある別端末からアクセスできるようにする。
# 動画のサムネイル(キーフレーム表示も含む)はあらかじめ作成しておく。動画の開始から10秒程度のところを初期表示されるサムネイル画像として使用する。
# アプリケーション起動時と、更新ボタン押下時に、ファイルの追加、削除を検出し、表示内容を更新する。
# ファイル1つずつには個別のURLが割り当てられ、クリックすると、そのURLに遷移し、画像・動画がフルサイズで表示・再生される。
# 各ファイルには、高評価／低評価ボタンがあり、クリックすると、そのファイルに対する評価が登録される。クリック回数に制限はない。
# 評価は数値として管理し、0からスタートし、高評価ボタン押下で+1、低評価ボタン押下で-1される。
# 評価はファイルごとに保存され、アプリケーション再起動後も保持される。
# 高評価のみを表示するフィルターボタンを用意する。
# 低評価のファイルパスを一覧出力するボタンを用意する。

# 概要設計
# pythonでflaskを使ってローカルサーバーを立てる。
# アプリケーション起動時に、起動ルート配下に _metadata というフォルダを作成し、そこにサムネイルや、評価データを保存する。
# _metadataフォルダが存在しない場合、コンソールで確認の上、作成する。
# _metadataフォルダが存在し（または作成直後）、その中に関連ファイルやディレクトリがない場合、必要なものを作成する。
# 各動画ファイルのファイルハッシュ値を計算し、それをキーにして、サムネイル画像や評価データを保存する。
# ファイルパスが異なる、ハッシュが同一のファイルが存在する場合にも対応できるようにする。（その場合、同一ファイルとみなし、サムネイルや評価は共通としてよい。）
# 評価データはsqlite3データベースに保存する。

'''
次のとおり仕様を追加・変更してください

1. 動画の長さを取得して、データとして保存してください。一覧のファイルサイズの横に動画の長さを表示してください。
2. 動画の長さを考慮して、サムネイル生成のオフセット時間を調整してください。動画の長さが PREVIEW_START_SECONDS 未満の場合、動画の長さの10%の時間をオフセット時間としてください。(開始から10%過ぎたところをサムネに使う、ということ)
3. 同様に、動画の長さを考慮して、プレビュー画像のオフセット時間も調整してください。
   動画の長さを VIDEO_PREVIEW_COUNT で割り、それをオフセット時間としてください。（60秒の動画なら,10秒おきに6枚のプレビュー画像を生成する、ということ）
4. 動画のサムネイル生成、プレビュー画像生成に失敗した場合、空のファイルを作成し、次回以降の生成をスキップするようにしてください。
5. 一覧にファイル名が二重に表示されています。（ファイル名とパス、ルートにある場合は同じなので。）ファイル名のみでよいです。
'''


