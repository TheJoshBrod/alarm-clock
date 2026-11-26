from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent / "lights.json"
TUYA_VERSIONS = (3.3, 3.4, 3.5, 3.1)


class LightError(Exception):
    pass


class TuyaLight:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name") or cfg["id"]
        self.dp = str(cfg.get("dp", 20))
        self._version = cfg.get("version")  # None = auto-detect on first use
        self._lock = threading.Lock()

    def _device(self, version):
        import tinytuya
        d = tinytuya.OutletDevice(
            self.cfg["device_id"], self.cfg["ip"], self.cfg["local_key"],
            version=version, connection_timeout=3,
        )
        d.set_socketRetryLimit(1)
        return d

    def _status(self):
        versions = [self._version] if self._version else TUYA_VERSIONS
        last = None
        for v in versions:
            d = self._device(v)
            try:
                st = d.status()
            finally:
                d.close()
            if st and "Error" not in st and self.dp in st.get("dps", {}):
                self._version = v
                return bool(st["dps"][self.dp])
            last = (st or {}).get("Error", "no response")
        raise LightError(f"Tuya: {last}")

    def get_state(self) -> bool:
        with self._lock:
            return self._status()

    def set_state(self, on: bool) -> bool:
        with self._lock:
            if not self._version:
                self._status()
            d = self._device(self._version)
            try:
                r = d.set_status(on, switch=int(self.dp))
            finally:
                d.close()
            if r and "Error" in r:
                raise LightError(f"Tuya: {r['Error']}")
            return on


_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _run(coro, timeout=15):
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


class KasaLight:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name") or cfg["id"]

    def _connect(self):
        # The EP10 speaks IOT over KLAP v2, which python-kasa won't pick on its own.
        from kasa.credentials import Credentials
        from kasa.deviceconfig import (
            DeviceConfig, DeviceConnectionParameters, DeviceEncryptionType, DeviceFamily,
        )
        from kasa.iot import IotPlug
        from kasa.iotprotocol import IotProtocol
        from kasa.klaptransport import KlapTransportV2

        user, pw = os.environ.get("KASA_USERNAME"), os.environ.get("KASA_PASSWORD")
        if not user or not pw:
            raise LightError("Kasa login not set (KASA_USERNAME / KASA_PASSWORD)")
        host = self.cfg["ip"]
        config = DeviceConfig(
            host=host,
            credentials=Credentials(user, pw),
            connection_type=DeviceConnectionParameters(
                device_family=DeviceFamily.IotSmartPlugSwitch,
                encryption_type=DeviceEncryptionType.Klap,
                login_version=2,
            ),
        )
        return IotPlug(host=host, config=config,
                       protocol=IotProtocol(transport=KlapTransportV2(config=config)))

    async def _do(self, target: bool | None):
        dev = self._connect()
        try:
            await dev.update()
            if not self.cfg.get("name") and dev.alias:
                self.name = dev.alias
            if target is not None and target != dev.is_on:
                await (dev.turn_on() if target else dev.turn_off())
                await dev.update()
            return dev.is_on
        finally:
            await dev.disconnect()

    def get_state(self) -> bool:
        try:
            return _run(self._do(None))
        except LightError:
            raise
        except Exception as e:
            raise LightError(f"Kasa: {e}") from e

    def set_state(self, on: bool) -> bool:
        try:
            return _run(self._do(on))
        except LightError:
            raise
        except Exception as e:
            raise LightError(f"Kasa: {e}") from e



_TYPES = {"tuya": TuyaLight, "kasa": KasaLight}
_lights: dict[str, TuyaLight | KasaLight] = {}


def load():
    _lights.clear()
    if not CONFIG_FILE.exists():
        return
    with CONFIG_FILE.open() as f:
        for cfg in json.load(f).get("lights", []):
            cls = _TYPES.get(cfg.get("type"))
            if cls:
                _lights[cfg["id"]] = cls(cfg)
            else:
                print(f"lights.json: unknown type for {cfg.get('id')!r}")


def _entry(light, on=None, error=None):
    return {"id": light.id, "name": light.name, "on": on, "error": error}


def all_states() -> list[dict]:
    results: dict[str, dict] = {}

    def worker(light):
        try:
            results[light.id] = _entry(light, on=light.get_state())
        except Exception as e:
            results[light.id] = _entry(light, error=str(e))

    threads = [threading.Thread(target=worker, args=(l,)) for l in _lights.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return [results.get(l.id) or _entry(l, error="timed out") for l in _lights.values()]


def toggle(light_id: str) -> dict:
    light = _lights.get(light_id)
    if light is None:
        raise KeyError(light_id)
    try:
        return _entry(light, on=light.set_state(not light.get_state()))
    except Exception as e:
        return _entry(light, error=str(e))
