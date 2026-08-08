import os
import random
import subprocess
import threading
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
REPO_ROOT     = Path(__file__).resolve().parent
IMAGES_DIR    = REPO_ROOT / "images"
SONGS_DIR     = REPO_ROOT / "songs"
TMP           = Path("/tmp/mjx9")

IMAGES_FOLDER_URL = "https://drive.google.com/drive/folders/1sXwstU6lFL2G6msCSYife8GFt_YEmwN4"
SONGS_FOLDER_URL  = "https://drive.google.com/drive/folders/1KJRX_fSFyyRHhW_Lh846o9POxcrGgqL4"

OUT_W, OUT_H = 1920, 1080
FPS = 1  # image source, no need for a real framerate

IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
SONG_EXTS  = {".mp3", ".wav", ".m4a"}

# ── Duration: flat random 1h - 3h ────────────────────────────────────────────
MIN_DURATION = 1 * 60 * 60
MAX_DURATION = 3 * 60 * 60


def pick_duration():
    return random.randint(MIN_DURATION, MAX_DURATION)


DURATION = pick_duration()

# ── File size budget: random 1.0GB - 1.9GB ───────────────────────────────────
MIN_SIZE_BYTES    = int(1.00 * 1024 ** 3)
MAX_SIZE_BYTES    = int(1.90 * 1024 ** 3)
TARGET_SIZE_BYTES = random.randint(int(1.20 * 1024 ** 3), int(1.85 * 1024 ** 3))
AUDIO_BITRATE_K   = 128
VIDEO_KBPS        = int((TARGET_SIZE_BYTES * 8) / DURATION / 1000) - AUDIO_BITRATE_K
VIDEO_KBPS        = max(VIDEO_KBPS, 300)

TARGET_IMAGE_NAME = os.environ.get("TARGET_IMAGE_NAME")
if not TARGET_IMAGE_NAME:
    raise SystemExit("TARGET_IMAGE_NAME env var not set.")

TMP.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
SONGS_DIR.mkdir(parents=True, exist_ok=True)

print(f"\n>>> DURATION     : {DURATION}s ({DURATION // 3600}h {(DURATION % 3600) // 60}m)")
print(f">>> TARGET SIZE  : {TARGET_SIZE_BYTES / 1e9:.2f} GB (range 1.00-1.90 GB)")


def check_disk(min_gb, label):
    stat = os.statvfs(str(TMP))
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
    print(f"[DISK] {label}: {free_gb:.1f} GB free")
    if free_gb < min_gb:
        raise SystemExit(f"Not enough free space ({free_gb:.1f} GB) for '{label}'.")


def gdown_fetch(folder_url: str, dest: Path, label: str, attempts: int = 3):
    """Pull a Drive folder via the gdown CLI, matching the LWK workflow's approach."""
    for attempt in range(1, attempts + 1):
        print(f"[{label}] attempt {attempt}/{attempts}...")
        result = subprocess.run(
            ["gdown", "--folder", folder_url, "-O", str(dest)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"[{label}] succeeded on attempt {attempt}")
            return
        print(f"[{label}] attempt {attempt} failed:\n{result.stdout}\n{result.stderr}")
        if attempt < attempts:
            time.sleep(20)
    raise SystemExit(f"[{label}] all {attempts} attempts failed.")


check_disk(4.0, "before fetch")
print("\nFetching images...")
gdown_fetch(IMAGES_FOLDER_URL, IMAGES_DIR, "images")
print("Fetching songs...")
gdown_fetch(SONGS_FOLDER_URL, SONGS_DIR, "songs")
check_disk(2.0, "after fetch")

matches = list(IMAGES_DIR.rglob(TARGET_IMAGE_NAME))
if not matches:
    raise SystemExit(f"Target image {TARGET_IMAGE_NAME} not found in {IMAGES_DIR}.")
image_path = matches[0]
output_path = TMP / f"OUT_{image_path.stem}.mp4"

print(f"\n>>> SOURCE       : {image_path.name}")
print(f">>> OUTPUT FRAME : {OUT_W}x{OUT_H}")
print(f">>> VIDEO BITRATE: {VIDEO_KBPS}k\n")


def probe_duration(path: Path) -> float:
    """Get real duration of a media file via ffprobe, instead of guessing."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            text=True,
        ).strip()
        return float(out)
    except Exception as e:
        print(f"[WARN] Could not probe {path.name} ({e}) — assuming 200s.")
        return 200.0


# ── Audio: songs playlist looped/shuffled to DURATION ────────────────────────
songs = sorted(p for p in SONGS_DIR.rglob("*") if p.suffix.lower() in SONG_EXTS)
if not songs:
    raise SystemExit(f"No song files found in {SONGS_DIR}.")

print(f"Songs pool: {len(songs)} files")


def build_looped_pool(files, label):
    durations = [probe_duration(f) for f in files]
    total_len = sum(durations)
    SAFETY_MARGIN = 1.20
    repeats_needed = max(1, int((DURATION * SAFETY_MARGIN) // total_len) + 1)
    shuffled = list(files)
    random.shuffle(shuffled)
    concat_path = TMP / f"concat_{label}.txt"
    with open(concat_path, "w") as f:
        for _ in range(repeats_needed):
            random.shuffle(shuffled)
            for s in shuffled:
                f.write(f"file '{s.resolve()}'\n")
    pool_wav = TMP / f"{label}_pool.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
         "-t", str(DURATION), "-ar", "44100", "-ac", "2", str(pool_wav)],
        check=True,
    )
    return pool_wav


songs_wav = build_looped_pool(songs, "songs")

# ── Build ffmpeg command: Ken-Burns-free still image + audio ────────────────
filter_complex = (
    f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
    f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,format=yuv420p[outv]"
)

cmd = [
    "ffmpeg", "-y",
    "-loop", "1", "-framerate", str(FPS), "-i", str(image_path),
    "-i", str(songs_wav),
    "-t", str(DURATION),
    "-filter_complex", filter_complex,
    "-map", "[outv]", "-map", "1:a",
    "-c:v", "libx264", "-preset", "medium", "-tune", "stillimage",
    "-b:v", f"{VIDEO_KBPS}k", "-maxrate", f"{int(VIDEO_KBPS * 1.5)}k", "-bufsize", f"{VIDEO_KBPS * 2}k",
    "-r", str(FPS), "-g", str(FPS * 2),
    "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_K}k", "-ar", "44100",
    "-movflags", "+faststart",
    str(output_path),
]

print("\nRunning FFmpeg...")
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
stopped_by_watcher = False


def size_watcher():
    global stopped_by_watcher
    while proc.poll() is None:
        time.sleep(15)
        if output_path.exists():
            size = output_path.stat().st_size
            mb = size / (1024 * 1024)
            gb = size / (1024 * 1024 * 1024)
            print(f"[SIZE] {output_path.name} -> {mb:.1f} MB ({gb:.3f} GB)", flush=True)
            if size >= MAX_SIZE_BYTES:
                print("[SIZE] Hit 1.90 GB cap - stopping.", flush=True)
                stopped_by_watcher = True
                proc.terminate()
                break


watcher = threading.Thread(target=size_watcher, daemon=True)
watcher.start()
for line in proc.stdout:
    print(line, end="", flush=True)
proc.wait()
watcher.join()

if not stopped_by_watcher and proc.returncode != 0:
    raise SystemExit(f"FFmpeg failed with return code {proc.returncode}")

if not output_path.exists() or output_path.stat().st_size == 0:
    raise SystemExit("No output produced.")

final_size = output_path.stat().st_size
final_size_mb = final_size / (1024 * 1024)
final_size_gb = final_size / (1024 * 1024 * 1024)
under_minimum = final_size < MIN_SIZE_BYTES
stop_reason = "capped at 1.90 GB" if stopped_by_watcher else "duration reached"

if under_minimum:
    print(f"[WARN] Output is only {final_size_gb:.3f} GB - below the 1.00 GB minimum target.")

print(f"\nDONE - {output_path}")
print(f"Stop reason  : {stop_reason}")
print(f"Bitrate used : {VIDEO_KBPS}k")
print(f"Frame        : {OUT_W}x{OUT_H}")
print(f"Duration     : {DURATION}s ({DURATION // 3600}h {(DURATION % 3600) // 60}m)")
print(f"Size         : {final_size_mb:.1f} MB ({final_size_gb:.3f} GB)")
print(f"Source       : {image_path.name}")

github_output = os.environ.get("GITHUB_OUTPUT")
if github_output:
    with open(github_output, "a") as f:
        f.write(f"output_path={output_path}\n")
        f.write(f"image_name={image_path.name}\n")
        f.write(f"duration_seconds={DURATION}\n")
        f.write(f"final_size_mb={final_size_mb:.1f}\n")
        f.write(f"under_minimum={str(under_minimum).lower()}\n")
