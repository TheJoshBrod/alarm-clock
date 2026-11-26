## AIY Alarm Clock

A Raspberry Pi alarm clock designed with a specific rule: **the only way to silence a ringing alarm is by physically pressing the button on the device.** You cannot stop it from your phone.

The goal is to force you out of bed. Since you have to walk to the device to press the button, you are less likely to stay under the covers and browse your phone. You use your phone to set alarms, but not to dismiss them.

## Hardware

* Raspberry Pi 3B (or similar)
* [Google AIY Voice Kit v2](https://aiyprojects.withgoogle.com/voice): This provides the physical button and LED on top of the box.

## How it works

* A Flask web server runs on the Pi. It provides a **mobile view** for your phone and a **desktop view** for a full dashboard.
* The server detects your device type automatically using the User-Agent. You can also force a view by adding `?view=mobile` or `?view=desktop` to the URL.
* A background scheduler checks the time every second. It triggers when the current time matches an enabled alarm and the day of the week is correct.
* When an alarm goes off, the AIY LED blinks and audio loops using `aplay` for WAV files or `mpg123` for MP3s.
* **The web UI lacks a silence button.** You must walk to the Pi and press the AIY button to stop the sound.
* All settings are saved in `alarms.json` so they persist through reboots.

## Features

* **Custom Audio:** Upload `.wav` or `.mp3` files or use a default generated tone.
* **Fade-in Volume:** Increase the volume gradually over a set time (0 to 300 seconds).
* **Individual Volume:** Set a unique target volume for every alarm.
* **Day-of-Week Repeat:** Choose specific days for the alarm to trigger. Leave all days unchecked for a daily alarm.
* **Sunrise/Sunset Display:** The sidebar shows local sunrise and sunset times based on your IP location.
* **Dynamic Themes:** The UI colors change automatically based on the time of day (dawn, day, dusk, and night).
* **Audio Library:** A dedicated section to manage, favorite, rename, or delete sounds.

## Setup

```bash
sudo apt install -y alsa-utils mpg123
pip3 install -r requirements.txt
```

Note: The AIY libraries (`aiy.board`, `aiy.voice.audio`) are already included on the official Voice Kit SD card image.

## Run

```bash
python3 alarm_clock.py
```

The terminal will show your local network URL on startup. You can usually access it via the Pi's hostname:

`http://raspberrypi.local:8080`

If you have changed your hostname, replace `raspberrypi` with your chosen name. Open this link on your phone or computer and bookmark it.

## Run on boot

Create a service file at `/etc/systemd/system/alarm-clock.service`:

```ini
[Unit]
Description=AIY Alarm Clock
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/alarm_clock
EnvironmentFile=-/home/pi/alarm_clock/alarm-clock.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/pi/alarm_clock/venv/bin/python /home/pi/alarm_clock/alarm_clock.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable the service:

```bash
sudo systemctl enable --now alarm-clock
```

## Lights

The dashboard (desktop sidebar and top of the mobile view) has a **Lights** card with an on/off button for each smart device. Control is local over your LAN.

1. Copy `lights.example.json` to `lights.json` and fill in your devices. `lights.json` is git-ignored because it holds device keys.
   * `tuya`: needs `device_id`, `ip` and `local_key` (from `tinytuya wizard`). Leave `version` as `null` to auto-detect.
   * `kasa`: needs `ip`. Newer firmware (KLAP) also needs your TP-Link login.
2. For Kasa, create `alarm-clock.env` (also git-ignored, `chmod 600`) with:
   ```
   KASA_USERNAME=you@example.com
   KASA_PASSWORD=yourpassword
   ```
3. Install the extra packages: `pip install tinytuya "python-kasa==0.7.7" "cryptography>=42"` (0.7.7 is the newest python-kasa for Python 3.9).

## Security

Set a password so only you can use the web UI. Add it to `alarm-clock.env` (git-ignored, `chmod 600`):

```
ALARM_PASSWORD=a-long-password
```

Each browser logs in once and stays logged in for a year. The session signing key is generated on first run and saved to `secret_key` (git-ignored); delete it to log out every device. Without `ALARM_PASSWORD` the UI stays open to anyone on your network and a warning is printed at startup.

Requests that change anything must come from the alarm clock's own pages, so other websites you visit can't send commands to it. To reach it away from home, use a VPN such as Tailscale rather than forwarding the port on your router.

## Configuration

Modify default settings using environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `ALARM_PASSWORD` | *(none)* | Password for the web UI |
| `ALARM_PORT` | `8080` | Web server port |
| `ALARM_ALSA_MIXER` | `Master` | ALSA mixer control name |
| `ALARM_TONE_FREQ` | `880` | Default tone frequency (Hz) |
| `ALARM_TONE_DURATION` | `1.0` | Default tone clip length (sec) |
| `ALARM_FADE_START_PCT` | `10` | Starting volume % for fade-in ramps |

## Project layout

* `alarm_clock.py`: Flask app, scheduler, and AIY hardware integration.
* `requirements.txt`: Python dependencies.
* `alarms.json`: Saved alarm data.
* `audio/`: Folder for uploaded sounds and generated tones.
* `templates/mobile.html`: Interface for phone users.
* `templates/desktop.html`: Interface for desktop users.
