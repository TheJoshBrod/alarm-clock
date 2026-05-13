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
ExecStart=/usr/bin/python3 /home/pi/alarm_clock/alarm_clock.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable the service:

```bash
sudo systemctl enable --now alarm-clock
```

## Configuration

Modify default settings using environment variables:

| Variable | Default | Description |
| --- | --- | --- |
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
