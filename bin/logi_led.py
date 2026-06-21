"""
Thin ctypes wrapper around Logitech's LED Illumination SDK
(LogitechLedEnginesWrapper.dll).

A single LogiLedSetTargetDevice(LOGI_DEVICETYPE_ALL) call makes every later
command apply to *all* connected Logitech G devices at once - so the G213
keyboard and a Logitech G mouse light up together and stay in sync.

Requires Logitech G HUB running with "Allow Games & Applications to control
illumination" enabled (the default). If the DLL or G HUB is unavailable, every
method degrades to a no-op so the rest of the HUD keeps working.

Colour values passed to the SDK are percentages (0-100), not 0-255.
"""

import os
import ctypes
import threading

# Device-type target masks (from LogitechLEDLib.h)
LOGI_DEVICETYPE_MONOCHROME = 1 << 0
LOGI_DEVICETYPE_RGB        = 1 << 1
LOGI_DEVICETYPE_PERKEY_RGB = 1 << 2
LOGI_DEVICETYPE_ALL = (
    LOGI_DEVICETYPE_MONOCHROME | LOGI_DEVICETYPE_RGB | LOGI_DEVICETYPE_PERKEY_RGB
)

_DLL_NAME = "LogitechLedEnginesWrapper.dll"


def _pct(v):
    """Clamp a 0-255 channel to the SDK's 0-100 percentage range."""
    return max(0, min(100, int(round(v * 100 / 255))))


class LogiLED:
    def __init__(self, dll_path=None, app_name="Claude Traffic Light"):
        self._lock = threading.Lock()
        self._ok = False
        self._lib = None
        self.app_name = app_name
        self.last_error = None

        if dll_path is None:
            dll_path = os.path.join(os.path.dirname(__file__), _DLL_NAME)

        try:
            self._lib = ctypes.CDLL(dll_path)
            self._bind()
            # InitWithName lets G HUB show a friendly integration entry.
            ok = self._lib.LogiLedInitWithName(app_name.encode("utf-8"))
            if not ok:
                ok = self._lib.LogiLedInit()
            self._ok = bool(ok)
            if self._ok:
                self._lib.LogiLedSetTargetDevice(LOGI_DEVICETYPE_ALL)
            else:
                self.last_error = "LogiLedInit returned false (G HUB not running / SDK disabled)"
        except OSError as e:
            self.last_error = f"DLL load failed: {e}"
        except Exception as e:  # noqa: BLE001
            self.last_error = f"init failed: {e}"

    def _bind(self):
        lib = self._lib
        lib.LogiLedInit.restype = ctypes.c_bool
        lib.LogiLedInitWithName.restype = ctypes.c_bool
        lib.LogiLedInitWithName.argtypes = [ctypes.c_char_p]
        lib.LogiLedSetTargetDevice.restype = ctypes.c_bool
        lib.LogiLedSetTargetDevice.argtypes = [ctypes.c_int]
        lib.LogiLedSetLighting.restype = ctypes.c_bool
        lib.LogiLedSetLighting.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.LogiLedPulseLighting.restype = ctypes.c_bool
        lib.LogiLedPulseLighting.argtypes = [ctypes.c_int] * 5
        lib.LogiLedFlashLighting.restype = ctypes.c_bool
        lib.LogiLedFlashLighting.argtypes = [ctypes.c_int] * 5
        lib.LogiLedStopEffects.restype = ctypes.c_bool
        lib.LogiLedShutdown.restype = None

    @property
    def available(self):
        return self._ok

    def set_color(self, rgb):
        """Solid colour on all devices. rgb is a 0-255 (r, g, b) tuple."""
        if not self._ok:
            return
        r, g, b = rgb
        with self._lock:
            self._lib.LogiLedStopEffects()
            self._lib.LogiLedSetLighting(_pct(r), _pct(g), _pct(b))

    def pulse(self, rgb, interval_ms=1500):
        """Infinite breathing pulse between colour and off (animated 'busy')."""
        if not self._ok:
            return
        r, g, b = rgb
        with self._lock:
            self._lib.LogiLedStopEffects()
            # duration 0 == run until stopped
            self._lib.LogiLedPulseLighting(_pct(r), _pct(g), _pct(b), 0, interval_ms)

    def flash(self, rgb, interval_ms=400):
        """Flash on/off (used optionally to grab attention)."""
        if not self._ok:
            return
        r, g, b = rgb
        with self._lock:
            self._lib.LogiLedStopEffects()
            self._lib.LogiLedFlashLighting(_pct(r), _pct(g), _pct(b), 0, interval_ms)

    def release(self):
        """Stop effects and hand lighting control back to G HUB's profile."""
        if not self._ok:
            return
        with self._lock:
            try:
                self._lib.LogiLedStopEffects()
                self._lib.LogiLedShutdown()
            finally:
                self._ok = False


if __name__ == "__main__":
    # Standalone probe: cycle the canonical states so you can watch the keyboard.
    import time
    led = LogiLED()
    print("available:", led.available, "| error:", led.last_error)
    if led.available:
        for name, fn in [
            ("GREEN  (idle)",       lambda: led.set_color((0, 255, 0))),
            ("RED    (permission)", lambda: led.set_color((255, 0, 0))),
            ("AMBER pulse (busy)",  lambda: led.pulse((255, 150, 0))),
        ]:
            print("->", name)
            fn()
            time.sleep(2.5)
        led.release()
        print("released back to G HUB")
