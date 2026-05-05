# AIY Alarm Clock

Local-network alarm clock for the Google AIY Voice Kit on a Raspberry Pi 3B.
Set alarms from a phone/laptop on the same network; alarms play audio downloaded
from YouTube; the AIY button on top of the device is the only way to silence a
ringing alarm.

## Setup on the Pi

```bash
sudo apt install -y ffmpeg alsa-utils
pip3 install -r requirements.txt
```

`yt-dlp` (installed by `requirements.txt`) needs `ffmpeg` to extract audio.
`aplay` ships with `alsa-utils`.

The AIY libraries (`aiy.board`, `aiy.voice.audio`) are already on the kit's SD
card image.

## Run

```bash
python3 alarm_clock.py
```

It prints the URL on startup, e.g. `http://192.168.1.76:8080`. Open that on any
device on the same network.

## How it works

- Adding an alarm downloads the YouTube audio to `audio/<id>.wav` immediately,
  so triggering is fast and works offline.
- A scheduler thread checks every second; when local time matches an enabled
  alarm's `HH:MM` and today is in its day-of-week list, it triggers.
- During an alarm the LED blinks and the wav loops via `aplay` until the AIY
  button is pressed. The web UI has no stop button by design.
- Alarms persist in `alarms.json`.

## Run on boot (optional)

Create `/etc/systemd/system/alarm-clock.service`:

```
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

Then `sudo systemctl enable --now alarm-clock`.
