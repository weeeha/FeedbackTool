#!/usr/bin/env python3
"""feedback_video.py

Turns a Quest feedback recording into a timestamped transcript plus a handful
of contact sheets of stamped frames, using ffmpeg, whisper and Pillow.

Everything of consequence lives in a function. `main(argv=None)` is the only
entry point and returns a process exit code; run this file directly and it
calls `sys.exit(main())`. Importing this module has no side effects, which is
what lets the test suite import it freely.

Every subprocess call is isolated in its own thin wrapper (`_run_subprocess`
plus one function per external tool) so tests can monkeypatch just that
function instead of touching adb, ffmpeg or whisper for real.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - exercised only when Pillow is absent
    Image = ImageDraw = ImageFont = None
    _PIL_IMPORT_ERROR = exc


# ---------------------------------------------------------------------------
# Pick-policy constants (spec-mandated names and values)
# ---------------------------------------------------------------------------
SCENE_OFFSET = 0.3      # seconds added to a detected scene change
MIN_GAP = 2.0           # minimum seconds between kept picks
SPEECH_CHUNK = 6.0      # a speech segment gets ceil(len / SPEECH_CHUNK) picks
FILL_GAP = 10.0         # a stretch with no pick longer than this gets one every FILL_GAP
NO_SPEECH_MAX = 0.6     # segments above this no_speech_prob are dropped
REASON_PRIORITY = {"speech": 0, "scene": 1, "fill": 2}

# ---------------------------------------------------------------------------
# Other constants
# ---------------------------------------------------------------------------
WHISPER_MODEL = "turbo"
SHEET_COLS = 3
SHEET_ROWS = 3
TILES_PER_SHEET = SHEET_COLS * SHEET_ROWS
SHEET_WIDTH = 1568
DEDUPE_THUMB_SIZE = 16
DEDUPE_THRESHOLD = 4.0  # mean absolute difference (0-255 scale) below which a frame counts as a duplicate of the previous kept one

STAGE_ORDER = ["pull", "audio", "transcribe", "scenes", "pick", "extract", "dedupe", "sheets"]

DEVICE_RECORDING_DIR = "/sdcard/Oculus/VideoShots"

CACHE_ROOT_ENV_VAR = "FEEDBACK_VIDEO_CACHE_DIR"  # test/dev override; unset in normal use

RECORDING_NAME_RE = re.compile(
    r"^(?P<package>.+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<n>\d+)\.mp4$"
)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------
def parse_recording_name(filename: str) -> dict:
    """Parse a Quest recording filename into its parts.

    "com.weeeha.vrroom-20260918-170744-0.mp4" ->
        {"package": "com.weeeha.vrroom", "date": "2026-09-18", "time": "17:07:44",
         "slug_hint": "vrroom-1707", "stem": "com.weeeha.vrroom-20260918-170744-0"}

    Never raises: a name that does not match the pattern gets package/date/time
    set to None and a slug_hint sanitised from the stem.
    """
    filename = filename or ""
    basename = os.path.basename(filename)

    if basename.lower().endswith(".mp4"):
        stem = basename[: -len(".mp4")]
    else:
        stem = basename

    match = RECORDING_NAME_RE.match(basename)
    if match:
        package = match.group("package")
        date_raw = match.group("date")
        time_raw = match.group("time")
        date = f"{date_raw[0:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        time = f"{time_raw[0:2]}:{time_raw[2:4]}:{time_raw[4:6]}"
        pkg_last = package.rsplit(".", 1)[-1] if package else "recording"
        slug_hint = f"{pkg_last}-{time_raw[0:4]}"
        return {
            "package": package,
            "date": date,
            "time": time,
            "slug_hint": slug_hint,
            "stem": stem,
        }

    sanitized = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    slug_hint = sanitized or "recording"
    return {
        "package": None,
        "date": None,
        "time": None,
        "slug_hint": slug_hint,
        "stem": stem or slug_hint,
    }


def format_clock(seconds: float) -> str:
    """5.0 -> "00:05"; 65.4 -> "01:05"; 3725.0 -> "1:02:05" (past one hour)."""
    total = int(round(max(seconds, 0.0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_transcript_line(segment: dict) -> str:
    """"[00:01-00:12] okay I wanted to show feedback" (text stripped, single spaces)."""
    start = format_clock(segment["start"])
    end = format_clock(segment["end"])
    text = " ".join(str(segment.get("text", "")).split())
    return f"[{start}-{end}] {text}"


def filter_segments(segments: list, no_speech_max: float = NO_SPEECH_MAX) -> list:
    """Drop segments whose no_speech_prob is above the threshold.

    A missing no_speech_prob counts as 0.0 (i.e. kept).
    """
    return [s for s in segments if s.get("no_speech_prob", 0.0) <= no_speech_max]


def frame_cap(duration: float) -> int:
    """40 up to and including 300s; +8 per additional started minute; ceiling 72."""
    if duration <= 300:
        cap = 40
    else:
        extra_minutes = math.ceil((duration - 300) / 60.0)
        cap = 40 + 8 * extra_minutes
    return min(cap, 72)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _speech_picks(segments: list, duration: float) -> list:
    picks = []
    for idx, seg in enumerate(segments):
        start = float(seg["start"])
        end = float(seg["end"])
        length = end - start
        if length <= 0:
            t = _clamp(start, 0.0, duration)
            picks.append({"time": t, "reason": "speech", "segment_index": idx})
            continue
        n = max(1, math.ceil(length / SPEECH_CHUNK))
        if n == 1:
            t = start + length / 2.0
            picks.append(
                {"time": _clamp(t, 0.0, duration), "reason": "speech", "segment_index": idx}
            )
        else:
            for j in range(n):
                t = start + (j + 0.5) * length / n
                picks.append(
                    {
                        "time": _clamp(t, 0.0, duration),
                        "reason": "speech",
                        "segment_index": idx,
                    }
                )
    return picks


def _scene_picks(scenes: list, duration: float) -> list:
    picks = []
    for s in scenes:
        t = float(s) + SCENE_OFFSET
        if t > duration:
            continue
        picks.append({"time": _clamp(t, 0.0, duration), "reason": "scene", "segment_index": None})
    return picks


def _fill_picks(existing_times: list, duration: float) -> list:
    times = sorted(existing_times)
    boundaries = [0.0] + times + [duration]
    fills = []
    for i in range(len(boundaries) - 1):
        gap_start = boundaries[i]
        gap_end = boundaries[i + 1]
        gap = gap_end - gap_start
        if gap > FILL_GAP:
            t = gap_start + FILL_GAP
            while t < gap_end - 1e-9:
                fills.append({"time": _clamp(t, 0.0, duration), "reason": "fill", "segment_index": None})
                t += FILL_GAP
    return fills


def _apply_spacing(picks: list) -> list:
    """Sort by time and walk forward, dropping any pick within MIN_GAP of the
    previously kept pick -- except when the new pick has a better
    REASON_PRIORITY, in which case it replaces the previously kept pick.
    """
    ordered = sorted(picks, key=lambda p: (p["time"], REASON_PRIORITY[p["reason"]]))
    kept: list = []
    for p in ordered:
        if kept and (p["time"] - kept[-1]["time"]) < MIN_GAP:
            if REASON_PRIORITY[p["reason"]] < REASON_PRIORITY[kept[-1]["reason"]]:
                kept[-1] = p
            # else: worse or equal priority than what's already kept -> drop p
        else:
            kept.append(p)
    return kept


def _thin_evenly(items: list, budget: int) -> list:
    n = len(items)
    if budget <= 0:
        return []
    if budget >= n:
        return list(items)
    if budget == 1:
        return [items[n // 2]]
    indices = sorted({round(i * (n - 1) / (budget - 1)) for i in range(budget)})
    if len(indices) < budget:
        remaining = [i for i in range(n) if i not in indices]
        indices = sorted(indices + remaining[: budget - len(indices)])
    return [items[i] for i in indices[:budget]]


def _apply_cap(picks: list, cap: int) -> list:
    if len(picks) <= cap:
        return sorted(picks, key=lambda p: p["time"])

    fills = sorted([p for p in picks if p["reason"] == "fill"], key=lambda p: p["time"])
    scenes = sorted([p for p in picks if p["reason"] == "scene"], key=lambda p: p["time"])
    speeches = sorted([p for p in picks if p["reason"] == "speech"], key=lambda p: p["time"])

    # Drop fill picks first.
    while fills and (len(fills) + len(scenes) + len(speeches)) > cap:
        fills.pop()
    # Then scene picks.
    while scenes and (len(fills) + len(scenes) + len(speeches)) > cap:
        scenes.pop()
    # Finally thin speech picks evenly to fit the remaining budget.
    remaining = cap - len(fills) - len(scenes)
    if remaining < len(speeches):
        speeches = _thin_evenly(speeches, max(remaining, 0))

    result = fills + scenes + speeches
    result.sort(key=lambda p: p["time"])
    return result[:cap]


def pick_frames(segments: list, scenes: list, duration: float, cap: int) -> list:
    """Pure pick policy. See module docstring / design spec for the algorithm.

    segments: already filtered, each {"start": float, "end": float, "text": str}
    scenes: raw detected change times (floats); SCENE_OFFSET is added here.
    Returns a list of {"time": float, "reason": ..., "segment_index": ...},
    sorted by time, deterministic, never longer than `cap`.
    """
    duration = max(float(duration), 0.0)
    if duration == 0.0:
        return []

    speech = _speech_picks(segments, duration)
    scene = _scene_picks(scenes, duration)

    existing_times = [p["time"] for p in speech] + [p["time"] for p in scene]
    fill = _fill_picks(existing_times, duration)

    spaced = _apply_spacing(speech + scene + fill)
    capped = _apply_cap(spaced, cap)
    capped.sort(key=lambda p: p["time"])
    return capped


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
def check_dependencies(which=shutil.which, pillow_available: bool = None) -> list:
    """Return the list of missing required tools ("ffmpeg", "whisper", "Pillow").

    `which` and `pillow_available` are injectable for tests; production calls
    use the real `shutil.which` and the module-level Pillow import result.
    """
    if pillow_available is None:
        pillow_available = Image is not None

    missing = []
    if which("ffmpeg") is None:
        missing.append("ffmpeg")
    if which("ffprobe") is None:
        missing.append("ffprobe")
    if which("whisper") is None and not os.path.exists(os.path.expanduser("~/.local/bin/whisper")):
        missing.append("whisper")
    if not pillow_available:
        missing.append("Pillow")
    return missing


def dependency_install_hint(tool: str) -> str:
    hints = {
        "ffmpeg": "brew install ffmpeg",
        "ffprobe": "brew install ffmpeg",
        "whisper": "pipx install openai-whisper",
        "Pillow": "pip install Pillow",
    }
    return hints.get(tool, f"install {tool}")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def cache_root() -> Path:
    override = os.environ.get(CACHE_ROOT_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / "Library" / "Caches" / "feedback-video"


def cache_dir_for(stem: str) -> Path:
    d = cache_root() / stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def redo_stage(cache_dir: Path, stage: str) -> None:
    """Delete `stage`'s output and every later stage's output."""
    stage_outputs = {
        "pull": ["video.mp4"],
        "audio": ["audio.wav"],
        "transcribe": ["whisper.json"],
        "scenes": ["scenes.json"],
        "pick": ["picks.json"],
        "extract": ["frames"],
        "dedupe": [],  # dedupe rewrites picks.json in place; handled via "pick"+"extract"
        "sheets": ["sheets"],
    }
    if stage not in STAGE_ORDER:
        return
    idx = STAGE_ORDER.index(stage)
    for later_stage in STAGE_ORDER[idx:]:
        for name in stage_outputs.get(later_stage, []):
            target = cache_dir / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()


# ---------------------------------------------------------------------------
# Subprocess wrappers (each isolates exactly one external-tool call)
# ---------------------------------------------------------------------------
def _run_subprocess(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def find_adb() -> str:
    """$ADB, then adb on PATH, then the newest bundled Unity Android SDK adb."""
    env_adb = os.environ.get("ADB")
    if env_adb and os.path.exists(env_adb):
        return env_adb

    path_adb = shutil.which("adb")
    if path_adb:
        return path_adb

    candidates = glob.glob(
        "/Applications/Unity/Hub/Editor/*/PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb"
    )
    if candidates:
        candidates.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
        return candidates[0]

    return None


def adb_list_videos(adb_path: str, package: str = None) -> list:
    """List recordings in DEVICE_RECORDING_DIR, newest first.

    Returns a list of {"name": str, "size": int, "mtime": str}. Raises
    RuntimeError with the adb state on failure (no device / unauthorized).
    """
    result = _run_subprocess([adb_path, "shell", "ls", "-la", DEVICE_RECORDING_DIR])
    if result.returncode != 0:
        raise RuntimeError(f"adb ls failed: {result.stderr.strip() or result.stdout.strip()}")

    entries = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        name = parts[-1]
        if not name.endswith(".mp4"):
            continue
        if package and not name.startswith(package):
            continue
        try:
            size = int(parts[4])
        except (ValueError, IndexError):
            size = None
        entries.append({"name": name, "size": size, "mtime": " ".join(parts[5:8])})

    entries.sort(key=lambda e: e["name"], reverse=True)
    return entries


def resolve_source(source: str, package: str, adb_path) -> dict:
    """Decide whether SOURCE is a local file, or something to pull from device.

    Returns {"kind": "local", "path": str} or {"kind": "device", "name": str}.
    """
    if source and source != "latest" and os.path.exists(source):
        return {"kind": "local", "path": os.path.abspath(source)}

    if source and source != "latest" and source.endswith(".mp4") and "/" not in source:
        return {"kind": "device", "name": source}

    if source and source != "latest" and not os.path.exists(source):
        # Looks like a local path that just doesn't exist.
        if os.sep in source or source.startswith(".") or source.startswith("~"):
            raise FileNotFoundError(source)

    # "latest" (default) or an unresolved bare name: ask the device.
    videos = adb_list_videos(adb_path, package=package)
    if not videos:
        raise LookupError("no matching recordings on device")
    return {"kind": "device", "name": videos[0]["name"]}


def pull_video(resolved: dict, cache_dir: Path, adb_path: str) -> Path:
    """Materialize the source video at cache_dir/video.mp4."""
    dest = cache_dir / "video.mp4"
    if resolved["kind"] == "local":
        src = Path(resolved["path"])
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            return dest
        shutil.copy2(src, dest)
        return dest

    # device
    if dest.exists():
        return dest
    remote = f"{DEVICE_RECORDING_DIR}/{resolved['name']}"
    result = _run_subprocess([adb_path, "pull", remote, str(dest)])
    if result.returncode != 0:
        raise RuntimeError(f"adb pull failed: {result.stderr.strip() or result.stdout.strip()}")
    return dest


def extract_audio(video_path: Path, audio_path: Path) -> Path:
    """ffmpeg to 16 kHz mono wav."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-ac", "1", "-ar", "16000",
        str(audio_path),
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr}")
    return audio_path


def probe_duration(video_path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = _run_subprocess(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr}")
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def run_whisper(audio_path: Path, out_dir: Path, model: str = WHISPER_MODEL, language: str = None) -> Path:
    """Run whisper on audio_path, writing json output, and return the path to
    whisper.json in out_dir (renamed from whisper's own <stem>.json output).
    """
    whisper_bin = shutil.which("whisper") or os.path.expanduser("~/.local/bin/whisper")
    cmd = [
        whisper_bin,
        str(audio_path),
        "--model", model,
        "--output_format", "json",
        "--output_dir", str(out_dir),
        "--condition_on_previous_text", "False",
    ]
    if language:
        cmd += ["--language", language]

    result = _run_subprocess(cmd)
    produced = out_dir / (Path(audio_path).stem + ".json")
    dest = out_dir / "whisper.json"
    if result.returncode != 0 or not produced.exists():
        raise RuntimeError(f"whisper failed: {result.stderr or result.stdout}")
    if produced != dest:
        produced.replace(dest)
    return dest


PTS_TIME_RE = re.compile(r"pts_time:([0-9.]+)")


def detect_scenes(video_path: Path) -> list:
    """ffmpeg at 5 fps, 320px wide, select='gt(scene,0.3)' with showinfo.
    Returns a list of raw pts_time floats parsed from stderr.
    """
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-vf", "fps=5,scale=320:-1,select='gt(scene,0.3)',showinfo",
        "-f", "null", "-",
    ]
    result = _run_subprocess(cmd)
    times = [float(m.group(1)) for m in PTS_TIME_RE.finditer(result.stderr or "")]
    return times


def extract_frame(video_path: Path, time_s: float, out_path: Path) -> bool:
    """One ffmpeg -ss T -i video -frames:v 1, full resolution. Returns success."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{time_s:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(out_path),
    ]
    result = _run_subprocess(cmd)
    return result.returncode == 0 and out_path.exists()


# ---------------------------------------------------------------------------
# Dedupe (thumbnail signature is IO via Pillow; the diff/select logic is pure)
# ---------------------------------------------------------------------------
def thumb_signature(image_path: Path) -> list:
    """16x16 greyscale thumbnail as a flat list of 256 ints (0-255)."""
    with Image.open(image_path) as im:
        thumb = im.convert("L").resize((DEDUPE_THUMB_SIZE, DEDUPE_THUMB_SIZE))
        if hasattr(thumb, "get_flattened_data"):
            return list(thumb.get_flattened_data())
        return list(thumb.getdata())


def signature_diff(a: list, b: list) -> float:
    """Mean absolute difference between two equal-length signatures."""
    if not a or not b:
        return 255.0
    total = sum(abs(x - y) for x, y in zip(a, b))
    return total / len(a)


def dedupe_picks(picks: list, signatures: list, threshold: float = DEDUPE_THRESHOLD) -> list:
    """Drop a pick whose thumbnail differs from the previously *kept* pick's
    thumbnail by less than `threshold` (mean absolute difference).

    `signatures[i]` is the thumbnail signature for `picks[i]`; picks must
    already be in time order.
    """
    kept = []
    kept_sig = None
    for pick, sig in zip(picks, signatures):
        if kept_sig is None or signature_diff(sig, kept_sig) >= threshold:
            kept.append(pick)
            kept_sig = sig
    return kept


# ---------------------------------------------------------------------------
# Sheets (Pillow)
# ---------------------------------------------------------------------------
def build_sheets(frame_paths: list, labels: list, out_dir: Path) -> list:
    """3x3 grid, 1568px wide, each tile stamped with `labels[i]` in white on a
    dark rounded box in a corner. Writes sheet-1.jpg, sheet-2.jpg, ... to
    out_dir and returns the list of written paths, in order.
    """
    if Image is None:
        raise RuntimeError(f"Pillow is not available: {_PIL_IMPORT_ERROR}")

    out_dir.mkdir(parents=True, exist_ok=True)
    sheet_paths = []
    font = ImageFont.load_default(size=28)

    col_width = SHEET_WIDTH // SHEET_COLS
    last_col_width = SHEET_WIDTH - col_width * (SHEET_COLS - 1)

    total = len(frame_paths)
    for sheet_idx in range(math.ceil(total / TILES_PER_SHEET) or 0):
        chunk = list(
            zip(
                frame_paths[sheet_idx * TILES_PER_SHEET : (sheet_idx + 1) * TILES_PER_SHEET],
                labels[sheet_idx * TILES_PER_SHEET : (sheet_idx + 1) * TILES_PER_SHEET],
            )
        )
        if not chunk:
            continue

        # Determine a common tile height from the first frame's aspect ratio.
        with Image.open(chunk[0][0]) as sample:
            aspect = sample.height / sample.width
        tile_height = max(1, round(col_width * aspect))

        sheet = Image.new("RGB", (SHEET_WIDTH, tile_height * SHEET_ROWS), (20, 20, 20))
        draw = ImageDraw.Draw(sheet)

        for i, (frame_path, label) in enumerate(chunk):
            row, col = divmod(i, SHEET_COLS)
            this_col_width = last_col_width if col == SHEET_COLS - 1 else col_width
            x0 = col * col_width
            y0 = row * tile_height

            with Image.open(frame_path) as im:
                resized = im.convert("RGB").resize((this_col_width, tile_height))
            sheet.paste(resized, (x0, y0))

            _stamp_label(draw, label, x0, y0, this_col_width, tile_height, font)

        sheet_path = out_dir / f"sheet-{sheet_idx + 1}.jpg"
        sheet.save(sheet_path, "JPEG", quality=88)
        sheet_paths.append(sheet_path)

    return sheet_paths


def _stamp_label(draw, label: str, x0: int, y0: int, tile_w: int, tile_h: int, font) -> None:
    padding = 6
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    box_x0 = x0 + 8
    box_y0 = y0 + tile_h - text_h - padding * 2 - 8
    box_x1 = box_x0 + text_w + padding * 2
    box_y1 = box_y0 + text_h + padding * 2

    draw.rounded_rectangle([box_x0, box_y0, box_x1, box_y1], radius=6, fill=(0, 0, 0, 200))
    draw.text((box_x0 + padding - bbox[0], box_y0 + padding - bbox[1]), label, font=font, fill=(255, 255, 255))


# ---------------------------------------------------------------------------
# Git repo check (isolated for testability)
# ---------------------------------------------------------------------------
def is_inside_git_repo(path: str = ".") -> bool:
    result = _run_subprocess(["git", "-C", path, "rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------
def _print_stage(name: str, skipped: bool) -> None:
    status = "skipped (cached)" if skipped else "running"
    print(f"[{name}] {status}")


def _stage_pull(resolved: dict, cache_dir: Path, adb_path: str) -> Path:
    dest = cache_dir / "video.mp4"
    skipped = dest.exists() and resolved["kind"] != "local"
    if resolved["kind"] == "local":
        # local copies are cheap to re-check by size; still report accurately
        src = Path(resolved["path"])
        skipped = dest.exists() and dest.stat().st_size == src.stat().st_size
    _print_stage("pull", skipped)
    return pull_video(resolved, cache_dir, adb_path)


def _stage_audio(video_path: Path, cache_dir: Path) -> Path:
    dest = cache_dir / "audio.wav"
    skipped = dest.exists()
    _print_stage("audio", skipped)
    if not skipped:
        extract_audio(video_path, dest)
    return dest


def _stage_transcribe(audio_path: Path, cache_dir: Path, language: str) -> dict:
    dest = cache_dir / "whisper.json"
    skipped = dest.exists()
    _print_stage("transcribe", skipped)
    if not skipped:
        try:
            run_whisper(audio_path, cache_dir, language=language)
        except Exception as exc:
            print(f"[transcribe] whisper failed, continuing without speech: {exc}")
            return {"segments": [], "language": language, "text": "", "_failed": True}
    if not dest.exists():
        return {"segments": [], "language": language, "text": "", "_failed": True}
    with open(dest) as fh:
        data = json.load(fh)
    return data


def _stage_scenes(video_path: Path, cache_dir: Path) -> list:
    dest = cache_dir / "scenes.json"
    skipped = dest.exists()
    _print_stage("scenes", skipped)
    if skipped:
        with open(dest) as fh:
            return json.load(fh)
    times = detect_scenes(video_path)
    with open(dest, "w") as fh:
        json.dump(times, fh)
    return times


def _stage_pick(segments: list, scenes: list, duration: float, cap: int, cache_dir: Path) -> list:
    dest = cache_dir / "picks.json"
    skipped = dest.exists()
    _print_stage("pick", skipped)
    if skipped:
        with open(dest) as fh:
            return json.load(fh)
    picks = pick_frames(segments, scenes, duration, cap)
    with open(dest, "w") as fh:
        json.dump(picks, fh)
    return picks


def _stage_extract(picks: list, video_path: Path, cache_dir: Path) -> list:
    frames_dir = cache_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    skipped = all(
        (frames_dir / _frame_filename(p)).exists() for p in picks
    ) and len(picks) > 0
    _print_stage("extract", skipped)

    results = []
    for pick in picks:
        frame_path = frames_dir / _frame_filename(pick)
        ok = frame_path.exists()
        if not ok:
            ok = extract_frame(video_path, pick["time"], frame_path)
        results.append({**pick, "extracted": ok, "cached_path": str(frame_path) if ok else None})
    return results


def _frame_filename(pick: dict) -> str:
    clock = format_clock(pick["time"]).replace(":", "")
    return f"f-{clock}.jpg"


def _stage_dedupe(extracted_picks: list, cache_dir: Path) -> list:
    ok_picks = [p for p in extracted_picks if p["extracted"]]
    failed_picks = [p for p in extracted_picks if not p["extracted"]]
    if not ok_picks:
        return extracted_picks

    signatures = [thumb_signature(Path(p["cached_path"])) for p in ok_picks]
    kept = dedupe_picks(ok_picks, signatures)

    kept_times = {p["time"] for p in kept}
    result = [p for p in extracted_picks if p["time"] in kept_times or p in failed_picks]

    picks_path = cache_dir / "picks.json"
    with open(picks_path, "w") as fh:
        json.dump(result, fh)
    return result


def _stage_sheets(kept_picks: list, cache_dir: Path) -> list:
    sheets_dir = cache_dir / "sheets"
    good_picks = [p for p in kept_picks if p.get("extracted")]
    expected_count = math.ceil(len(good_picks) / TILES_PER_SHEET) if good_picks else 0
    existing = sorted(sheets_dir.glob("sheet-*.jpg")) if sheets_dir.exists() else []
    skipped = len(existing) == expected_count and expected_count > 0
    _print_stage("sheets", skipped)
    if skipped:
        return existing

    frame_paths = [Path(p["cached_path"]) for p in good_picks]
    labels = [f"#{i + 1} {format_clock(p['time'])}" for i, p in enumerate(good_picks)]
    return build_sheets(frame_paths, labels, sheets_dir)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
def write_outputs(
    out_dir: Path,
    date: str,
    slug: str,
    source_name: str,
    duration: float,
    language: str,
    segments: list,
    kept_picks: list,
    sheet_paths: list,
    transcribe_failed: bool,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    good_picks = [p for p in kept_picks if p.get("extracted")]

    transcript_path = out_dir / f"{date}-{slug}-transcript.txt"
    if segments and not transcribe_failed:
        lines = [format_transcript_line(s) for s in segments]
        transcript_path.write_text("\n".join(lines) + "\n")
    else:
        transcript_path.write_text("")

    written_sheets = []
    for i, sheet_src in enumerate(sheet_paths, start=1):
        dest = out_dir / f"{date}-{slug}-frames-{i}.jpg"
        shutil.copy2(sheet_src, dest)
        written_sheets.append(dest)

    manifest_picks = []
    for i, p in enumerate(good_picks):
        sheet_no = i // TILES_PER_SHEET + 1
        tile_no = i % TILES_PER_SHEET + 1
        words = ""
        if p["reason"] == "speech" and p.get("segment_index") is not None:
            idx = p["segment_index"]
            if 0 <= idx < len(segments):
                words = " ".join(str(segments[idx].get("text", "")).split())
        manifest_picks.append(
            {
                "index": i + 1,
                "time": p["time"],
                "reason": p["reason"],
                "segment_index": p.get("segment_index"),
                "sheet": sheet_no,
                "tile": tile_no,
                "cached_path": p.get("cached_path"),
                "words": words,
            }
        )

    manifest = {
        "source": source_name,
        "duration": duration,
        "language": language,
        "transcript": None if (transcribe_failed or not segments) else str(transcript_path.name),
        "picks": manifest_picks,
    }
    manifest_path = out_dir / f"{date}-{slug}-manifest.json"
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)

    return {
        "transcript_path": transcript_path,
        "manifest_path": manifest_path,
        "sheet_paths": written_sheets,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feedback_video.py",
        description="Turn a Quest feedback recording into a transcript and stamped contact sheets.",
    )
    parser.add_argument(
        "source",
        nargs="?",
        default="latest",
        help="'latest' (default), a filename on the device, or a local path to an mp4",
    )
    parser.add_argument("--out", help="output directory (default: docs/feedback under the current git repo)")
    parser.add_argument("--slug", help="slug for output filenames (default: derived from the recording name)")
    parser.add_argument("--package", help="narrow 'latest'/device listing to this package")
    parser.add_argument("--language", help="whisper language (default: auto-detect)")
    parser.add_argument("--cap", type=int, help="override the frame cap")
    parser.add_argument("--redo", choices=STAGE_ORDER, help="delete this stage's output and every later stage's, then rerun")
    parser.add_argument("--keep-audio", action="store_true", help="keep the cached audio.wav after transcription")
    return parser


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    missing = check_dependencies()
    if missing:
        hints = "; ".join(f"{m}: {dependency_install_hint(m)}" for m in missing)
        print(f"Missing required tool(s): {', '.join(missing)}. Install with: {hints}", file=sys.stderr)
        return 3

    if args.out:
        out_dir = Path(args.out)
    else:
        if not is_inside_git_repo("."):
            print(
                "The current directory is not inside a git repo, so I don't know where to "
                "write docs. Pass --out to choose a directory.",
                file=sys.stderr,
            )
            return 2
        out_dir = Path.cwd() / "docs" / "feedback"

    adb_path = find_adb()
    try:
        resolved = resolve_source(args.source, args.package, adb_path)
    except FileNotFoundError as exc:
        print(f"No such local file: {exc}", file=sys.stderr)
        return 2
    except LookupError:
        try:
            newest = adb_list_videos(adb_path, package=None)[:5]
        except Exception:
            newest = []
        names = ", ".join(v["name"] for v in newest) or "(none found)"
        print(f"No mp4 matches on the device. Newest files there: {names}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"Could not reach the device: {exc}. Pass a local path instead.", file=sys.stderr)
        return 2

    if resolved["kind"] == "local":
        source_filename = os.path.basename(resolved["path"])
    else:
        source_filename = resolved["name"]

    print(f"Source: {source_filename}")

    parsed = parse_recording_name(source_filename)
    stem = parsed["stem"]
    date = parsed["date"] or "unknown-date"
    slug = args.slug or parsed["slug_hint"]

    cache_dir = cache_dir_for(stem)
    if args.redo:
        redo_stage(cache_dir, args.redo)

    try:
        video_path = _stage_pull(resolved, cache_dir, adb_path)
    except RuntimeError as exc:
        print(f"Could not fetch the recording: {exc}", file=sys.stderr)
        return 2

    duration = probe_duration(video_path)

    audio_path = _stage_audio(video_path, cache_dir)
    whisper_data = _stage_transcribe(audio_path, cache_dir, args.language)
    transcribe_failed = bool(whisper_data.get("_failed"))
    language = args.language or whisper_data.get("language") or "en"

    raw_segments = whisper_data.get("segments", [])
    segments = filter_segments(raw_segments)
    if not segments:
        transcribe_failed = transcribe_failed or not raw_segments

    if not args.keep_audio and audio_path.exists():
        try:
            audio_path.unlink()
        except OSError:
            pass

    scenes = _stage_scenes(video_path, cache_dir)

    cap = args.cap if args.cap is not None else frame_cap(duration)
    picks = _stage_pick(segments, scenes, duration, cap, cache_dir)

    extracted_picks = _stage_extract(picks, video_path, cache_dir)
    kept_picks = _stage_dedupe(extracted_picks, cache_dir)

    sheet_paths = _stage_sheets(kept_picks, cache_dir)

    outputs = write_outputs(
        out_dir=out_dir,
        date=date,
        slug=slug,
        source_name=source_filename,
        duration=duration,
        language=language,
        segments=segments,
        kept_picks=kept_picks,
        sheet_paths=sheet_paths,
        transcribe_failed=transcribe_failed,
    )

    print(f"Transcript: {outputs['transcript_path']}")
    print(f"Manifest: {outputs['manifest_path']}")
    for sp in outputs["sheet_paths"]:
        print(f"Sheet: {sp}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
