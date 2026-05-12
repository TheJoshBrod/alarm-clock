import array
import json
import math
import re
import socket
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from aiy.board import Board, Led

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "audio"
ALARMS_FILE = BASE_DIR / "alarms.json"
AUDIO_META_FILE = BASE_DIR / "audio_meta.json"
AUDIO_DIR.mkdir(exist_ok=True)

# ALSA mixer control used for gradual volume ramp.
# Change to "PCM" or another name if your Pi setup differs.
ALSA_MIXER = "Master"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload limit

_state_lock = threading.Lock()
_alarms = []
_last_fired = {}

_alarm_active = threading.Event()
_active_proc = None
_active_proc_lock = threading.Lock()

_board = None
_led_lock = threading.Lock()

_original_volume = None  # int or None; saved before gradual ramp, restored on silence

# Regex to identify auto-generated per-alarm tone files (8 hex chars + .wav).
# These are owned by their alarm and deleted with it.
# Uploaded library files have user-chosen names and are not auto-deleted.
_TONE_RE = re.compile(r"^[0-9a-f]{8}\.wav$")


_audio_meta: dict = {}  # filename -> {"favorite": bool}


def load_alarms():
    global _alarms
    if ALARMS_FILE.exists():
        with ALARMS_FILE.open() as f:
            _alarms = json.load(f)
    else:
        _alarms = []


def save_alarms():
    tmp = ALARMS_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(_alarms, f, indent=2)
    tmp.replace(ALARMS_FILE)


def load_audio_meta():
    global _audio_meta
    if AUDIO_META_FILE.exists():
        with AUDIO_META_FILE.open() as f:
            _audio_meta = json.load(f)
    else:
        _audio_meta = {}


def save_audio_meta():
    tmp = AUDIO_META_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(_audio_meta, f, indent=2)
    tmp.replace(AUDIO_META_FILE)


def generate_tone_wav(dest_wav, frequency=880, duration=1.0, sample_rate=44100):
    n_samples = int(sample_rate * duration)
    samples = array.array("h", [
        int(32767 * math.sin(2 * math.pi * frequency * i / sample_rate))
        for i in range(n_samples)
    ])
    with wave.open(str(dest_wav), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def play_loop_until_stopped(wav_path):
    global _active_proc
    ext = Path(wav_path).suffix.lower()
    if ext == ".mp3":
        cmd = ["mpg123", "-q", str(wav_path)]
    else:
        cmd = ["aplay", "-q", str(wav_path)]
    while _alarm_active.is_set():
        with _active_proc_lock:
            _active_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        proc = _active_proc
        proc.wait()
        with _active_proc_lock:
            _active_proc = None


def stop_active_audio():
    with _active_proc_lock:
        if _active_proc is not None and _active_proc.poll() is None:
            _active_proc.terminate()


def _get_alsa_volume():
    try:
        out = subprocess.check_output(
            ["amixer", "get", ALSA_MIXER], stderr=subprocess.DEVNULL, timeout=2, text=True
        )
        m = re.search(r'\[(\d+)%\]', out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _set_alsa_volume(pct: int):
    try:
        subprocess.run(
            ["amixer", "sset", ALSA_MIXER, f"{max(0, min(100, pct))}%"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except Exception:
        pass


def _volume_ramp_loop(start_pct: int, end_pct: int, duration_sec: int):
    # One step every ~10 s, minimum 10 steps.
    steps = max(10, duration_sec // 10)
    interval = duration_sec / steps
    _set_alsa_volume(start_pct)
    for i in range(1, steps + 1):
        for _ in range(int(interval * 10)):
            if not _alarm_active.is_set():
                return
            time.sleep(0.1)
        pct = start_pct + (end_pct - start_pct) * i // steps
        _set_alsa_volume(pct)


def trigger_alarm(alarm):
    if _alarm_active.is_set():
        return
    wav_path = AUDIO_DIR / alarm["audio_file"]
    if not wav_path.exists():
        print(f"Audio file missing for alarm {alarm['id']}: {wav_path}")
        return

    _alarm_active.set()
    with _led_lock:
        if _board is not None:
            _board.led.state = Led.BLINK
    print(f"ALARM: {alarm.get('label') or alarm['id']}")
    threading.Thread(
        target=play_loop_until_stopped, args=(wav_path,), daemon=True
    ).start()
    if alarm.get("gradual"):
        global _original_volume
        _original_volume = _get_alsa_volume()
        duration_sec = int(alarm.get("gradual_minutes", 10)) * 60
        threading.Thread(
            target=_volume_ramp_loop, args=(10, 100, duration_sec), daemon=True
        ).start()


def silence_alarm():
    if not _alarm_active.is_set():
        return
    _alarm_active.clear()
    stop_active_audio()
    global _original_volume
    if _original_volume is not None:
        _set_alsa_volume(_original_volume)
        _original_volume = None
    with _led_lock:
        if _board is not None:
            _board.led.state = Led.ON
    print("Alarm silenced.")


def scheduler_loop():
    # Fires when local time matches an alarm's HH:MM and today is in days[].
    # Uses _last_fired to prevent re-trigger within the same minute.
    while True:
        try:
            now = datetime.now()
            day_idx = now.weekday()  # Monday = 0
            hhmm = now.strftime("%H:%M")
            minute_key = now.strftime("%Y-%m-%dT%H:%M")

            with _state_lock:
                snapshot = list(_alarms)

            for alarm in snapshot:
                if not alarm.get("enabled", True):
                    continue
                if alarm["time"] != hhmm:
                    continue
                days = alarm.get("days") or list(range(7))
                if day_idx not in days:
                    continue
                if _last_fired.get(alarm["id"]) == minute_key:
                    continue
                _last_fired[alarm["id"]] = minute_key
                trigger_alarm(alarm)
        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(1)


def button_loop():
    # board.button.wait_for_press() blocks until the next press.
    # While the alarm is active, the press silences it. Otherwise it's a no-op.
    while True:
        try:
            _board.button.wait_for_press()
            if _alarm_active.is_set():
                silence_alarm()
            else:
                print("Button pressed (no alarm active).")
            # Tiny debounce so a held button doesn't spam.
            time.sleep(0.3)
        except Exception as e:
            print(f"Button error: {e}")
            time.sleep(1)


def _is_mobile():
    ua = request.headers.get("User-Agent", "")
    return any(token in ua for token in ("Mobi", "Android", "iPhone", "iPad"))


def _library_audio_files():
    """Return audio file dicts sorted favorites-first then alphabetically."""
    names = sorted(
        p.name for p in AUDIO_DIR.iterdir()
        if p.is_file() and not _TONE_RE.match(p.name) and p.suffix.lower() in (".wav", ".mp3")
    )
    files = [
        {"name": n, "stem": Path(n).stem, "ext": Path(n).suffix,
         "favorite": bool(_audio_meta.get(n, {}).get("favorite"))}
        for n in names
    ]
    files.sort(key=lambda f: (not f["favorite"], f["name"].lower()))
    return files


def _validate_audio_bytes(data: bytes, ext: str) -> bool:
    if ext == ".wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if ext == ".mp3":
        # ID3 tag header OR MPEG sync frame
        return data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and data[1] in (0xFB, 0xFA, 0xF3, 0xF2))
    return False


@app.route("/")
def index():
    view = request.args.get("view", "")
    use_mobile = (view == "mobile") or (view != "desktop" and _is_mobile())

    with _state_lock:
        alarms = sorted(_alarms, key=lambda a: a["time"])

    now = datetime.now()
    h = now.hour
    if 5 <= h < 8:
        tod = "dawn"
    elif 8 <= h < 17:
        tod = "day"
    elif 17 <= h < 20:
        tod = "dusk"
    else:
        tod = "night"

    ctx = dict(
        alarms=alarms,
        active=_alarm_active.is_set(),
        audio_files=_library_audio_files(),
        current_hour=h,
        current_minute=now.minute,
        weekday_name=now.strftime("%A"),
        date_str=now.strftime("%b %-d"),
        tod=tod,
    )

    if use_mobile:
        return render_template("mobile.html", **ctx)
    return render_template("desktop.html", **ctx, audio_meta=_audio_meta)


@app.route("/silence", methods=["POST"])
def silence():
    silence_alarm()
    return redirect(url_for("index"))


@app.route("/audio/upload", methods=["POST"])
def upload_audio():
    f = request.files.get("audio")
    if not f or not f.filename:
        return "No file provided", 400

    ext = Path(f.filename).suffix.lower()
    if ext not in (".wav", ".mp3"):
        return "Only .wav and .mp3 files are accepted", 400

    raw = f.read()
    if not _validate_audio_bytes(raw, ext):
        return f"File does not look like a valid {ext[1:].upper()}", 400

    safe_name = secure_filename(f.filename).lower()
    dest = AUDIO_DIR / safe_name
    dest.write_bytes(raw)
    return redirect(url_for("index"))


@app.route("/audio/<filename>/rename", methods=["POST"])
def rename_audio(filename):
    new_stem = request.form.get("new_name", "").strip()
    if not new_stem:
        return jsonify({"error": "Name cannot be empty"}), 400
    src = AUDIO_DIR / secure_filename(filename)
    if not src.exists() or _TONE_RE.match(src.name):
        return jsonify({"error": "File not found"}), 404
    new_name = secure_filename(new_stem + src.suffix).lower()
    dst = AUDIO_DIR / new_name
    if dst.exists() and dst != src:
        return jsonify({"error": f'A file named "{new_name}" already exists'}), 409
    src.rename(dst)
    with _state_lock:
        for a in _alarms:
            if a["audio_file"] == filename:
                a["audio_file"] = new_name
        save_alarms()
    if filename in _audio_meta:
        _audio_meta[new_name] = _audio_meta.pop(filename)
        save_audio_meta()
    return jsonify({"ok": True})


@app.route("/audio/<filename>/favorite", methods=["POST"])
def favorite_audio(filename):
    target = AUDIO_DIR / secure_filename(filename)
    if not target.exists():
        return "File not found", 404
    meta = _audio_meta.setdefault(filename, {})
    meta["favorite"] = not meta.get("favorite", False)
    save_audio_meta()
    return redirect(url_for("index"))


@app.route("/audio/<filename>/delete", methods=["POST"])
def delete_audio(filename):
    with _state_lock:
        in_use = any(a["audio_file"] == filename for a in _alarms)
    if in_use:
        return "File is referenced by an alarm — remove or reassign the alarm first", 409
    target = AUDIO_DIR / secure_filename(filename)
    if target.exists() and not _TONE_RE.match(target.name):
        target.unlink()
    return redirect(url_for("index"))


@app.route("/alarms", methods=["POST"])
def create_alarm():
    label = request.form.get("label", "").strip()
    time_str = request.form.get("time", "").strip()
    days = request.form.getlist("days")
    audio_file_choice = request.form.get("audio_file", "").strip()
    gradual = bool(request.form.get("gradual"))
    try:
        gradual_minutes = max(1, int(request.form.get("gradual_minutes", 10)))
    except ValueError:
        gradual_minutes = 10
    days_int = sorted({int(d) for d in days}) if days else list(range(7))

    if not time_str:
        return "time is required", 400
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        return "time must be HH:MM", 400

    alarm_id = uuid.uuid4().hex[:8]

    if audio_file_choice and (AUDIO_DIR / audio_file_choice).exists():
        audio_filename = audio_file_choice
    else:
        wav_path = AUDIO_DIR / f"{alarm_id}.wav"
        generate_tone_wav(wav_path)
        audio_filename = wav_path.name

    alarm = {
        "id": alarm_id,
        "label": label,
        "time": time_str,
        "days": days_int,
        "audio_file": audio_filename,
        "enabled": True,
        "gradual": gradual,
        "gradual_minutes": gradual_minutes,
    }
    with _state_lock:
        _alarms.append(alarm)
        save_alarms()
    return redirect(url_for("index"))


@app.route("/alarms/<alarm_id>/delete", methods=["POST"])
def delete_alarm(alarm_id):
    with _state_lock:
        keep = []
        for a in _alarms:
            if a["id"] == alarm_id:
                # Only delete the audio file if it's the auto-generated tone for this alarm.
                if _TONE_RE.match(a["audio_file"]):
                    wav = AUDIO_DIR / a["audio_file"]
                    if wav.exists():
                        wav.unlink()
            else:
                keep.append(a)
        _alarms[:] = keep
        save_alarms()
    return redirect(url_for("index"))


@app.route("/alarms/<alarm_id>/edit", methods=["POST"])
def edit_alarm(alarm_id):
    label = request.form.get("label", "").strip()
    time_str = request.form.get("time", "").strip()
    days = request.form.getlist("days")
    audio_file_choice = request.form.get("audio_file", "").strip()
    gradual = bool(request.form.get("gradual"))
    try:
        gradual_minutes = max(1, int(request.form.get("gradual_minutes", 10)))
    except ValueError:
        gradual_minutes = 10
    enabled = request.form.get("enabled") == "1"
    days_int = sorted({int(d) for d in days}) if days else list(range(7))

    if not time_str:
        return "time is required", 400
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        return "time must be HH:MM", 400

    with _state_lock:
        for a in _alarms:
            if a["id"] == alarm_id:
                a["label"] = label
                a["time"] = time_str
                a["days"] = days_int
                a["gradual"] = gradual
                a["gradual_minutes"] = gradual_minutes
                a["enabled"] = enabled
                if audio_file_choice and (AUDIO_DIR / audio_file_choice).exists():
                    a["audio_file"] = audio_file_choice
                elif not audio_file_choice and not _TONE_RE.match(a["audio_file"]):
                    wav_path = AUDIO_DIR / f"{a['id']}.wav"
                    if not wav_path.exists():
                        generate_tone_wav(wav_path)
                    a["audio_file"] = wav_path.name
                break
        save_alarms()
    return redirect(url_for("index"))


@app.route("/alarms/<alarm_id>/toggle", methods=["POST"])
def toggle_alarm(alarm_id):
    with _state_lock:
        for a in _alarms:
            if a["id"] == alarm_id:
                a["enabled"] = not a.get("enabled", True)
                break
        save_alarms()
    return redirect(url_for("index"))


@app.route("/status")
def status():
    return jsonify({"alarm_active": _alarm_active.is_set()})


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    global _board
    load_alarms()
    load_audio_meta()
    with Board() as board:
        _board = board
        board.led.state = Led.ON

        threading.Thread(target=scheduler_loop, daemon=True).start()
        threading.Thread(target=button_loop, daemon=True).start()

        try:
            ip = get_local_ip()
            print(f"Web UI: http://{ip}:8080")
        except Exception:
            pass

        app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
