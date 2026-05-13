from __future__ import annotations

import array
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
import wave
from datetime import datetime, date, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    pass
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from aiy.board import Board, Led

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "audio"
ALARMS_FILE = BASE_DIR / "alarms.json"
AUDIO_META_FILE = BASE_DIR / "audio_meta.json"
LOCATION_CACHE_FILE = BASE_DIR / "location_cache.json"
WEATHER_CACHE_FILE = BASE_DIR / "weather_cache.json"
AUDIO_DIR.mkdir(exist_ok=True)

# Deployment-specific knobs — override via environment variables.
ALSA_MIXER = os.environ.get("ALARM_ALSA_MIXER", "Master")
ALARM_PORT = int(os.environ.get("ALARM_PORT", "8080"))
TONE_FREQ = float(os.environ.get("ALARM_TONE_FREQ", "880"))
TONE_DURATION = float(os.environ.get("ALARM_TONE_DURATION", "1.0"))
TONE_SAMPLE_RATE = int(os.environ.get("ALARM_TONE_SAMPLE_RATE", "44100"))
FADE_START_PCT = int(os.environ.get("ALARM_FADE_START_PCT", "10"))

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

_original_volume = None  # int or None; saved before fade-in ramp, restored on silence

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
        # Migrate old gradual/gradual_minutes fields to fade_in_seconds/volume.
        changed = False
        for a in _alarms:
            if "snooze_minutes" in a:
                del a["snooze_minutes"]
                changed = True
            if "gradual" in a or "gradual_minutes" in a:
                grad_mins = int(a.pop("gradual_minutes", 10)) if a.pop("gradual", False) else 0
                a.setdefault("fade_in_seconds", grad_mins * 60)
                a.setdefault("volume", 80)
                changed = True
        if changed:
            save_alarms()
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


def fetch_location() -> dict | None:
    """Return {lat, lon, tz} from IP geolocation, using a cache file to survive reboots."""
    if LOCATION_CACHE_FILE.exists():
        try:
            with LOCATION_CACHE_FILE.open() as f:
                return json.load(f)
        except Exception:
            pass
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as resp:
            data = json.loads(resp.read())
        lat_str, lon_str = data.get("loc", "0,0").split(",")
        result = {
            "lat": float(lat_str),
            "lon": float(lon_str),
            "tz": data.get("timezone", "UTC"),
        }
        tmp = LOCATION_CACHE_FILE.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(result, f)
        tmp.replace(LOCATION_CACHE_FILE)
        return result
    except Exception as e:
        print(f"Location fetch failed: {e}")
        return None


def compute_sun_times(loc: dict | None, for_date: date | None = None) -> tuple[str | None, str | None]:
    """Return (sunrise_hhmm, sunset_hhmm) for today, or (None, None) on failure."""
    if loc is None:
        return None, None
    try:
        from astral import LocationInfo
        from astral.sun import sun
        target = for_date or date.today()
        location = LocationInfo("here", "", loc["tz"], loc["lat"], loc["lon"])
        s = sun(location.observer, date=target, tzinfo=loc["tz"])
        return s["sunrise"].strftime("%H:%M"), s["sunset"].strftime("%H:%M")
    except Exception as e:
        print(f"Sun time calculation failed: {e}")
        return None, None


def compute_moon_times(loc: dict | None) -> tuple[str | None, str | None, float | None]:
    """Return (moonrise_hhmm, moonset_hhmm, moon_phase) for today, or (None, None, None) on failure."""
    if loc is None:
        return None, None, None
    try:
        from astral import LocationInfo
        from astral.moon import moonrise, moonset, phase
        
        now = datetime.now(ZoneInfo(loc["tz"]))
        location = LocationInfo("here", "", loc["tz"], loc["lat"], loc["lon"])
        
        last_rise = None
        for i in range(3):
            d = now.date() - timedelta(days=i)
            try:
                r = moonrise(location.observer, date=d, tzinfo=loc["tz"])
                if r <= now:
                    if last_rise is None or r > last_rise:
                        last_rise = r
            except Exception:
                pass
                
        next_set = None
        for i in range(3):
            d = now.date() + timedelta(days=i)
            try:
                s = moonset(location.observer, date=d, tzinfo=loc["tz"])
                if s >= now:
                    if next_set is None or s < next_set:
                        next_set = s
            except Exception:
                pass
                
        r_str = last_rise.strftime("%H:%M") if last_rise else None
        s_str = next_set.strftime("%H:%M") if next_set else None
        m_phase = phase(now.date())
        
        return r_str, s_str, m_phase
    except Exception as e:
        print(f"Moon time calculation failed: {e}")
        return None, None, None


def fetch_weather(loc: dict | None) -> str | None:
    """Fetch daily weather forecast using Open-Meteo API, caching results for 10 minutes."""
    if loc is None:
        return None
    
    now_ts = time.time()
    if WEATHER_CACHE_FILE.exists():
        try:
            with WEATHER_CACHE_FILE.open() as f:
                data = json.load(f)
                if now_ts - data.get("timestamp", 0) < 600:
                    return data.get("forecast")
        except Exception:
            pass
            
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={loc['lat']}&longitude={loc['lon']}&daily=weathercode,temperature_2m_max,temperature_2m_min&current_weather=true&timezone=auto&forecast_days=1&temperature_unit=fahrenheit"
        req = urllib.request.Request(url, headers={'User-Agent': 'AlarmClockApp/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            daily = data.get("daily", {})
            current = data.get("current_weather", {})
            tmax = daily.get("temperature_2m_max", [None])[0]
            tmin = daily.get("temperature_2m_min", [None])[0]
            code = daily.get("weathercode", [0])[0]
            temp = current.get("temperature", tmax)
            
            # Basic WMO code mapping
            if code == 0: desc = "Clear"
            elif code in (1, 2, 3): desc = "Partly Cloudy"
            elif code in (45, 48): desc = "Fog"
            elif code in (51, 53, 55, 56, 57): desc = "Drizzle"
            elif code in (61, 63, 65, 66, 67): desc = "Rain"
            elif code in (71, 73, 75, 77, 85, 86): desc = "Snow"
            elif code in (80, 81, 82): desc = "Showers"
            elif code in (95, 96, 99): desc = "Thunderstorm"
            else: desc = "Variable"
            
            forecast = {
                "desc": desc,
                "tmax": round(tmax) if tmax is not None else None,
                "tmin": round(tmin) if tmin is not None else None,
                "temp": round(temp) if temp is not None else None
            }
                
            tmp = WEATHER_CACHE_FILE.with_suffix(".tmp")
            with tmp.open("w") as f:
                json.dump({"timestamp": now_ts, "forecast": forecast}, f)
            tmp.replace(WEATHER_CACHE_FILE)
            return forecast
    except Exception as e:
        print(f"Weather fetch failed: {e}")
        return None


_location: dict | None = None


def generate_tone_wav(dest_wav, frequency=TONE_FREQ, duration=TONE_DURATION, sample_rate=TONE_SAMPLE_RATE):
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
    vol = max(0, min(100, int(alarm.get("volume", 80))))
    fade_in = max(0, int(alarm.get("fade_in_seconds", 0)))
    if fade_in > 0:
        global _original_volume
        _original_volume = _get_alsa_volume()
        threading.Thread(
            target=_volume_ramp_loop, args=(FADE_START_PCT, vol, fade_in), daemon=True
        ).start()
    else:
        _set_alsa_volume(vol)
    threading.Thread(
        target=play_loop_until_stopped, args=(wav_path,), daemon=True
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

    sunrise_hhmm, sunset_hhmm = compute_sun_times(_location)
    moonrise_hhmm, moonset_hhmm, moon_phase_val = compute_moon_times(_location)
    weather_forecast = fetch_weather(_location)

    sun_x, sun_y, show_sun = 20, 23, False
    moon_x, moon_y, show_moon = 20, 23, False

    if sunrise_hhmm and sunset_hhmm:
        try:
            curr_mins = now.hour * 60 + now.minute
            h_r, m_r = map(int, sunrise_hhmm.split(':'))
            rise_mins = h_r * 60 + m_r
            h_s, m_s = map(int, sunset_hhmm.split(':'))
            set_mins = h_s * 60 + m_s
            
            if curr_mins >= rise_mins and curr_mins <= set_mins and set_mins > rise_mins:
                progress = (curr_mins - rise_mins) / (set_mins - rise_mins)
                angle = math.pi * (1 - progress)
                sun_x = 50 + 45 * math.cos(angle)
                sun_y = 50 - 45 * math.sin(angle)
                show_sun = True
        except Exception:
            pass

    if moonrise_hhmm and moonset_hhmm:
        try:
            curr_mins = now.hour * 60 + now.minute
            h_r, m_r = map(int, moonrise_hhmm.split(':'))
            rise_mins = h_r * 60 + m_r
            h_s, m_s = map(int, moonset_hhmm.split(':'))
            set_mins = h_s * 60 + m_s
            
            if set_mins <= rise_mins:
                set_mins += 24 * 60
                
            eff_curr_mins = curr_mins
            if eff_curr_mins < rise_mins and set_mins > 24 * 60:
                eff_curr_mins += 24 * 60
                
            if rise_mins <= eff_curr_mins <= set_mins and set_mins > rise_mins:
                progress = (eff_curr_mins - rise_mins) / (set_mins - rise_mins)
                angle = math.pi * (1 - progress)
                moon_x = 50 + 45 * math.cos(angle)
                moon_y = 50 - 45 * math.sin(angle)
                show_moon = True
        except Exception:
            pass

    moon_phase_emoji = "🌑"
    if moon_phase_val is not None:
        val = moon_phase_val
        if val < 1.84: moon_phase_emoji = "🌑"
        elif val < 5.53: moon_phase_emoji = "🌒"
        elif val < 9.22: moon_phase_emoji = "🌓"
        elif val < 12.91: moon_phase_emoji = "🌔"
        elif val < 16.61: moon_phase_emoji = "🌕"
        elif val < 20.30: moon_phase_emoji = "🌖"
        elif val < 23.99: moon_phase_emoji = "🌗"
        elif val < 27.68: moon_phase_emoji = "🌘"
        else: moon_phase_emoji = "🌑"

    ctx = dict(
        alarms=alarms,
        active=_alarm_active.is_set(),
        audio_files=_library_audio_files(),
        current_hour=h,
        current_minute=now.minute,
        weekday_name=now.strftime("%A"),
        date_str=now.strftime("%b %-d"),
        tod=tod,
        today_weekday=now.weekday(),  # Monday=0
        current_hhmm=now.strftime("%H:%M"),
        sunrise_hhmm=sunrise_hhmm,
        sunset_hhmm=sunset_hhmm,
        weather_forecast=weather_forecast,
        sun_x=sun_x,
        sun_y=sun_y,
        show_sun=show_sun,
        moonrise_hhmm=moonrise_hhmm,
        moonset_hhmm=moonset_hhmm,
        moon_x=moon_x,
        moon_y=moon_y,
        show_moon=show_moon,
        moon_phase_emoji=moon_phase_emoji,
    )

    if use_mobile:
        return render_template("mobile.html", **ctx)
    return render_template("desktop.html", **ctx, audio_meta=_audio_meta)


@app.route("/silence", methods=["POST"])
def silence():
    silence_alarm()
    return redirect(url_for("index"))


@app.route("/audio/<filename>")
def serve_audio(filename):
    safe = secure_filename(filename)
    return send_from_directory(AUDIO_DIR, safe)


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
    try:
        fade_in_seconds = max(0, int(request.form.get("fade_in_seconds", 0)))
    except ValueError:
        fade_in_seconds = 0
    try:
        volume = max(0, min(100, int(request.form.get("volume", 80))))
    except ValueError:
        volume = 80
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
        "fade_in_seconds": fade_in_seconds,
        "volume": volume,
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
    try:
        fade_in_seconds = max(0, int(request.form.get("fade_in_seconds", 0)))
    except ValueError:
        fade_in_seconds = 0
    try:
        volume = max(0, min(100, int(request.form.get("volume", 80))))
    except ValueError:
        volume = 80
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
                a["fade_in_seconds"] = fade_in_seconds
                a["volume"] = volume
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
    global _board, _location
    load_alarms()
    load_audio_meta()
    _location = fetch_location()
    with Board() as board:
        _board = board
        board.led.state = Led.ON

        threading.Thread(target=scheduler_loop, daemon=True).start()
        threading.Thread(target=button_loop, daemon=True).start()

        try:
            ip = get_local_ip()
            print(f"Web UI: http://{ip}:{ALARM_PORT}")
        except Exception:
            pass

        app.run(host="0.0.0.0", port=ALARM_PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
