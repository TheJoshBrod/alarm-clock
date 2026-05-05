import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from aiy.board import Board, Led

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "audio"
ALARMS_FILE = BASE_DIR / "alarms.json"
AUDIO_DIR.mkdir(exist_ok=True)

app = Flask(__name__)

_state_lock = threading.Lock()
_alarms = []
_last_fired = {}

_alarm_active = threading.Event()
_active_proc = None
_active_proc_lock = threading.Lock()

_board = None
_led_lock = threading.Lock()


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


def download_audio(url, dest_wav):
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-x",
        "--audio-format", "wav",
        "--audio-quality", "0",
        # YouTube has been rejecting the default ios/android player clients;
        # tv + web_safari + mweb is the combination that's currently working.
        "--extractor-args", "youtube:player_client=tv,web_safari,mweb,web",
        "-o", str(dest_wav.with_suffix(".%(ext)s")),
    ]
    cookies = os.environ.get("YTDLP_COOKIES")
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    subprocess.run(cmd, check=True)
    if not dest_wav.exists():
        # yt-dlp may have produced a slightly different filename — find it
        candidates = list(AUDIO_DIR.glob(f"{dest_wav.stem}.*"))
        wav_candidates = [c for c in candidates if c.suffix == ".wav"]
        if wav_candidates:
            wav_candidates[0].rename(dest_wav)
        else:
            raise RuntimeError("yt-dlp did not produce a wav file")


def play_loop_until_stopped(wav_path):
    global _active_proc
    while _alarm_active.is_set():
        with _active_proc_lock:
            _active_proc = subprocess.Popen(
                ["aplay", "-q", str(wav_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        proc = _active_proc
        proc.wait()
        with _active_proc_lock:
            _active_proc = None
        # If the alarm is still active, loop the audio.


def stop_active_audio():
    with _active_proc_lock:
        if _active_proc is not None and _active_proc.poll() is None:
            _active_proc.terminate()


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


def silence_alarm():
    if not _alarm_active.is_set():
        return
    _alarm_active.clear()
    stop_active_audio()
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


@app.route("/")
def index():
    with _state_lock:
        alarms = sorted(_alarms, key=lambda a: a["time"])
    return render_template("index.html", alarms=alarms, active=_alarm_active.is_set())


@app.route("/alarms", methods=["POST"])
def create_alarm():
    label = request.form.get("label", "").strip()
    time_str = request.form.get("time", "").strip()
    youtube_url = request.form.get("youtube_url", "").strip()
    days = request.form.getlist("days")
    days_int = sorted({int(d) for d in days}) if days else list(range(7))

    if not time_str or not youtube_url:
        return "time and youtube_url are required", 400
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        return "time must be HH:MM", 400

    alarm_id = uuid.uuid4().hex[:8]
    wav_path = AUDIO_DIR / f"{alarm_id}.wav"
    try:
        download_audio(youtube_url, wav_path)
    except subprocess.CalledProcessError as e:
        return f"Failed to download audio: {e}", 500

    alarm = {
        "id": alarm_id,
        "label": label,
        "time": time_str,
        "days": days_int,
        "youtube_url": youtube_url,
        "audio_file": wav_path.name,
        "enabled": True,
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
                wav = AUDIO_DIR / a["audio_file"]
                if wav.exists():
                    wav.unlink()
            else:
                keep.append(a)
        _alarms[:] = keep
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

        # Threaded so the scheduler / button continue while requests are served.
        # use_reloader=False because the reloader spawns a second process and
        # would grab the GPIO button a second time.
        app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
