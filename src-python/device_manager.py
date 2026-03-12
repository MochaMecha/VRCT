from typing import Callable, Dict, List, Optional, Any
from time import sleep
from threading import Thread
import sys
import os
from contextlib import contextmanager

@contextmanager
def _suppress_alsa_errors():
    """Suppress ALSA/JACK/PulseAudio stderr noise during PyAudio device enumeration on Linux."""
    if sys.platform != "linux":
        yield
        return
    try:
        import ctypes
        ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                               ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
        def _null_handler(filename, line, function, err, fmt):
            pass
        c_null_handler = ERROR_HANDLER_FUNC(_null_handler)
        try:
            asound = ctypes.cdll.LoadLibrary("libasound.so.2")
            asound.snd_lib_error_set_handler(c_null_handler)
        except OSError:
            pass
        # Also redirect stderr to suppress JACK/PipeWire messages
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(2)
        os.dup2(devnull, 2)
        try:
            yield
        finally:
            os.dup2(old_stderr, 2)
            os.close(old_stderr)
            os.close(devnull)
            try:
                asound.snd_lib_error_set_handler(None)
            except Exception:
                pass
    except Exception:
        yield

# Optional, Windows-specific dependencies. Guard imports so module can be imported on non-Windows systems.
try:
    import comtypes
except Exception:  # pragma: no cover - optional runtime
    comtypes = None  # type: ignore

try:
    from pyaudiowpatch import PyAudio, paWASAPI
except Exception:  # pragma: no cover - optional runtime
    paWASAPI = None  # type: ignore
    try:
        from pyaudio import PyAudio
    except Exception:
        PyAudio = None  # type: ignore

try:
    from pycaw.callbacks import MMNotificationClient
    from pycaw.utils import AudioUtilities
except Exception:  # pragma: no cover - optional runtime
    MMNotificationClient = object  # type: ignore
    AudioUtilities = None  # type: ignore

from utils import errorLogging


def _get_pulse_monitor_sources() -> List[Dict[str, Any]]:
    """Return PulseAudio/PipeWire monitor sources as dicts compatible with the speaker device list.

    Each dict contains: index (PyAudio device index for 'pulse'), name (human-readable),
    defaultSampleRate, maxInputChannels, and _pulse_source (the PA source name to set via
    PULSE_SOURCE before opening the stream).
    """
    if sys.platform != "linux":
        return []
    import subprocess, json as _json
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", "sources"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        sources = _json.loads(result.stdout)
    except Exception:
        return []

    # Find the PyAudio device index for 'pulse' (fallback to 'default')
    pulse_device_index = None
    try:
        with _suppress_alsa_errors():
            p = PyAudio()
            try:
                for i in range(p.get_device_count()):
                    dev = p.get_device_info_by_index(i)
                    if dev.get("name") == "pulse" and dev.get("maxInputChannels", 0) > 0:
                        pulse_device_index = i
                        break
                if pulse_device_index is None:
                    for i in range(p.get_device_count()):
                        dev = p.get_device_info_by_index(i)
                        if dev.get("name") == "default" and dev.get("maxInputChannels", 0) > 0:
                            pulse_device_index = i
                            break
            finally:
                p.terminate()
    except Exception:
        return []

    if pulse_device_index is None:
        return []

    monitors = []
    for src in sources:
        name = src.get("name", "")
        if ".monitor" not in name:
            continue
        desc = src.get("description", name)
        # Parse sample rate from the format spec
        sample_rate = 48000  # sensible default
        try:
            fmt = src.get("sample_specification", {})
            if isinstance(fmt, dict):
                sample_rate = int(fmt.get("sample_rate", 48000))
            elif isinstance(fmt, str):
                # e.g. "s32le 2ch 48000Hz"
                for part in fmt.split():
                    if part.endswith("Hz"):
                        sample_rate = int(part[:-2])
        except Exception:
            pass
        channels = 2
        try:
            ch = src.get("channel_map", [])
            if isinstance(ch, list):
                channels = len(ch) if ch else 2
            elif isinstance(ch, str):
                channels = ch.count(",") + 1
        except Exception:
            pass

        monitors.append({
            "index": pulse_device_index,
            "name": desc,
            "defaultSampleRate": float(sample_rate),
            "maxInputChannels": channels,
            "maxOutputChannels": 0,
            "isLoopbackDevice": False,
            "_pulse_source": name,
        })
    return monitors

class Client(MMNotificationClient):
    """Callback client used by pycaw to detect device changes.

    This subclass is lightweight: it flips a flag when events arrive so the
    monitoring loop can break and refresh device lists.
    """

    def __init__(self) -> None:
        # If MMNotificationClient is the placeholder object (non-windows), avoid calling super
        try:
            super().__init__()
        except Exception:
            pass
        self.loop: bool = True

    def on_default_device_changed(self, *args: Any, **kwargs: Any) -> None:
        self.loop = False

    def on_device_added(self, *args: Any, **kwargs: Any) -> None:
        self.loop = False

    def on_device_removed(self, *args: Any, **kwargs: Any) -> None:
        self.loop = False

    def on_device_state_changed(self, *args: Any, **kwargs: Any) -> None:
        self.loop = False

    # def on_property_value_changed(self, device_id, key):
    #     self.loop = False

class DeviceManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DeviceManager, cls).__new__(cls)
            # do NOT auto-init monitoring-heavy resources on import; require explicit init
            # Still perform a light-weight init so that callers observing the singleton
            # do not see uninitialized internal structures (which caused NoDevice to
            # be seen when import order differed).
            cls._instance._initialized = False
            try:
                # Call init() to populate internal containers. This will NOT start
                # the monitoring thread (startMonitoring must be called explicitly).
                cls._instance.init()
            except Exception:
                # Avoid import-time crashes; log and continue.
                try:
                    errorLogging()
                except Exception:
                    pass
        return cls._instance

    def init(self) -> None:
        """Initialize internal state. This is intentionally separate from object
        creation so importing the module won't start threads or access OS
        audio APIs. Call `device_manager.init()` and then
        `device_manager.startMonitoring()` explicitly when ready.
        """
        if getattr(self, "_initialized", False):
            return

        self.mic_devices: Dict[str, List[Dict[str, Any]]] = {"NoHost": [{"index": -1, "name": "NoDevice"}]}
        self.default_mic_device: Dict[str, Any] = {"host": {"index": -1, "name": "NoHost"}, "device": {"index": -1, "name": "NoDevice"}}
        self.speaker_devices: List[Dict[str, Any]] = [{"index": -1, "name": "NoDevice"}]
        self.default_speaker_device: Dict[str, Any] = {"device": {"index": -1, "name": "NoDevice"}}

        # Initialize previous state trackers
        self.prev_mic_host: List[str] = [host for host in self.mic_devices]
        self.prev_mic_devices: Dict[str, List[Dict[str, Any]]] = self.mic_devices
        self.prev_default_mic_device: Dict[str, Any] = self.default_mic_device
        self.prev_speaker_devices: List[Dict[str, Any]] = self.speaker_devices
        self.prev_default_speaker_device: Dict[str, Any] = self.default_speaker_device

        # Update flags
        self.update_flag_default_mic_device: bool = False
        self.update_flag_default_speaker_device: bool = False
        self.update_flag_host_list: bool = False
        self.update_flag_mic_device_list: bool = False
        self.update_flag_speaker_device_list: bool = False

        # Callbacks
        self.callback_default_mic_device: Optional[Callable[..., None]] = None
        self.callback_default_speaker_device: Optional[Callable[..., None]] = None
        self.callback_host_list: Optional[Callable[..., None]] = None
        self.callback_mic_device_list: Optional[Callable[..., None]] = None
        self.callback_speaker_device_list: Optional[Callable[..., None]] = None
        self.callback_process_before_update_mic_devices: Optional[Callable[..., None]] = None
        self.callback_process_after_update_mic_devices: Optional[Callable[..., None]] = None
        self.callback_process_before_update_speaker_devices: Optional[Callable[..., None]] = None
        self.callback_process_after_update_speaker_devices: Optional[Callable[..., None]] = None

        # Monitoring control
        self.monitoring_flag: bool = False
        self.th_monitoring: Optional[Thread] = None

        self._initialized = True

        # Best-effort single update: if PyAudio is available, attempt to populate
        # real device lists. Keep this short and ignore errors to avoid import-time
        # failures.
        try:
            if PyAudio is not None:
                try:
                    # update() is robust and will fall back to defaults if audio libs
                    # are missing or fail; do not let exceptions bubble up.
                    self.update()
                except Exception:
                    errorLogging()
        except Exception:
            # defensive: if errorLogging isn't available or other issues occur,
            # swallow to avoid breaking initialization
            pass

    def update(self):
        buffer_mic_devices: Dict[str, List[Dict[str, Any]]] = {}
        buffer_default_mic_device: Dict[str, Any] = {"host": {"index": -1, "name": "NoHost"}, "device": {"index": -1, "name": "NoDevice"}}
        buffer_speaker_devices: List[Dict[str, Any]] = []
        buffer_default_speaker_device: Dict[str, Any] = {"device": {"index": -1, "name": "NoDevice"}}

        if PyAudio is None:
            # PyAudio not available; leave defaults in place
            self.mic_devices = buffer_mic_devices or {"NoHost": [{"index": -1, "name": "NoDevice"}]}
            self.default_mic_device = buffer_default_mic_device
            self.speaker_devices = buffer_speaker_devices or [{"index": -1, "name": "NoDevice"}]
            self.default_speaker_device = buffer_default_speaker_device
            return

        try:
            with _suppress_alsa_errors():
                p = PyAudio()
                try:
                    # gather input devices grouped by host
                    for host_index in range(p.get_host_api_count()):
                        host = p.get_host_api_info_by_index(host_index)
                        device_count = host.get('deviceCount', 0)
                        for device_index in range(device_count):
                            device = p.get_device_info_by_host_api_device_index(host_index, device_index)
                            if device.get("maxInputChannels", 0) > 0 and not device.get("isLoopbackDevice", False):
                                buffer_mic_devices.setdefault(host["name"], []).append(device)
                    if not buffer_mic_devices:
                        buffer_mic_devices = {"NoHost": [{"index": -1, "name": "NoDevice"}]}

                    api_info = p.get_default_host_api_info()
                    default_mic_device = api_info.get("defaultInputDevice", -1)

                    for host_index in range(p.get_host_api_count()):
                        host = p.get_host_api_info_by_index(host_index)
                        device_count = host.get('deviceCount', 0)
                        for device_index in range(device_count):
                            device = p.get_device_info_by_host_api_device_index(host_index, device_index)
                            if device.get("index") == default_mic_device:
                                buffer_default_mic_device = {"host": host, "device": device}
                                break
                        else:
                            continue
                        break

                    # collect speaker/output devices
                    speaker_devices: List[Dict[str, Any]] = []
                    if paWASAPI is not None:
                        # Windows: use WASAPI loopback devices
                        try:
                            wasapi_info = p.get_host_api_info_by_type(paWASAPI)
                            wasapi_name = wasapi_info.get("name")
                            for host_index in range(p.get_host_api_count()):
                                host = p.get_host_api_info_by_index(host_index)
                                if host.get("name") == wasapi_name:
                                    device_count = host.get('deviceCount', 0)
                                    for device_index in range(device_count):
                                        device = p.get_device_info_by_host_api_device_index(host_index, device_index)
                                        if not device.get("isLoopbackDevice", True):
                                            for loopback in p.get_loopback_device_info_generator():
                                                if device.get("name") in loopback.get("name", ""):
                                                    speaker_devices.append(loopback)
                        except Exception:
                            pass
                    else:
                        # Linux: use PulseAudio/PipeWire monitor sources to capture
                        # desktop audio (equivalent of WASAPI loopback on Windows).
                        speaker_devices = _get_pulse_monitor_sources()

                    # deduplicate and sort
                    speaker_devices = [dict(t) for t in {tuple(d.items()) for d in speaker_devices}] or [{"index": -1, "name": "NoDevice"}]
                    buffer_speaker_devices = sorted(speaker_devices, key=lambda d: d.get('index', -1))

                    # default speaker
                    if paWASAPI is not None:
                        # Windows: find WASAPI default output loopback
                        try:
                            wasapi_info = p.get_host_api_info_by_type(paWASAPI)
                            default_speaker_device_index = wasapi_info.get("defaultOutputDevice", -1)
                            for host_index in range(p.get_host_api_count()):
                                host_info = p.get_host_api_info_by_index(host_index)
                                device_count = host_info.get('deviceCount', 0)
                                for device_index in range(0, device_count):
                                    device = p.get_device_info_by_host_api_device_index(host_index, device_index)
                                    if device.get("index") == default_speaker_device_index:
                                        default_speakers = device
                                        if not default_speakers.get("isLoopbackDevice", True):
                                            for loopback in p.get_loopback_device_info_generator():
                                                if default_speakers.get("name") in loopback.get("name", ""):
                                                    buffer_default_speaker_device = {"device": loopback}
                                                    break
                                        break

                                if buffer_default_speaker_device["device"].get("name") != "NoDevice":
                                    break
                        except Exception:
                            pass
                    else:
                        # Linux: pick the monitor source for the default PulseAudio sink
                        try:
                            import subprocess as _sp, json as _json
                            _res = _sp.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5)
                            default_sink = _res.stdout.strip()
                            default_monitor_name = default_sink + ".monitor"
                            for dev in buffer_speaker_devices:
                                if dev.get("_pulse_source") == default_monitor_name:
                                    buffer_default_speaker_device = {"device": dev}
                                    break
                            else:
                                # fallback: pick the first monitor source
                                if buffer_speaker_devices and buffer_speaker_devices[0].get("name") != "NoDevice":
                                    buffer_default_speaker_device = {"device": buffer_speaker_devices[0]}
                        except Exception:
                            pass
                finally:
                    p.terminate()
        except Exception:
            errorLogging()

        self.mic_devices = buffer_mic_devices
        self.default_mic_device = buffer_default_mic_device
        self.speaker_devices = buffer_speaker_devices
        self.default_speaker_device = buffer_default_speaker_device

    def checkUpdate(self):
        if self.prev_default_mic_device["device"]["name"] != self.default_mic_device["device"]["name"]:
            self.update_flag_default_mic_device = True
            self.prev_default_mic_device = self.default_mic_device
        if self.prev_default_speaker_device["device"]["name"] != self.default_speaker_device["device"]["name"]:
            self.update_flag_default_speaker_device = True
            self.prev_default_speaker_device = self.default_speaker_device
        if self.prev_mic_host != [host for host in self.mic_devices]:
            self.update_flag_host_list = True
            self.prev_mic_host = [host for host in self.mic_devices]
        if {key: [device['name'] for device in devices] for key, devices in self.prev_mic_devices.items()} != {key: [device['name'] for device in devices] for key, devices in self.mic_devices.items()}:
            self.update_flag_mic_device_list = True
            self.prev_mic_devices = self.mic_devices
        if [device['name'] for device in self.prev_speaker_devices] != [device['name'] for device in self.speaker_devices]:
            self.update_flag_speaker_device_list = True
            self.prev_speaker_devices = self.speaker_devices

        update_flag = (
            self.update_flag_default_mic_device or
            self.update_flag_default_speaker_device or
            self.update_flag_host_list or
            self.update_flag_mic_device_list or
            self.update_flag_speaker_device_list
        )
        return update_flag

    def monitoring(self):
        try:
            while self.monitoring_flag is True:
                try:
                    # Use COM only when available (Windows). If comtypes is not present,
                    # fall back to periodic polling using PyAudio only.
                    if comtypes is not None and AudioUtilities is not None:
                        try:
                            comtypes.CoInitialize()
                            cb = Client()
                            enumerator = AudioUtilities.GetDeviceEnumerator()
                            enumerator.RegisterEndpointNotificationCallback(cb)
                            while cb.loop is True and self.monitoring_flag is True:
                                sleep(1)
                            try:
                                enumerator.UnregisterEndpointNotificationCallback(cb)
                            except Exception:
                                # best-effort unregister
                                pass
                            comtypes.CoUninitialize()
                        except Exception:
                            # if COM monitoring fails, log and fall through to polling
                            errorLogging()

                    # polling and update cycle
                    self.runProcessBeforeUpdateMicDevices()
                    self.runProcessBeforeUpdateSpeakerDevices()
                    sleep(2)
                    for _ in range(10):
                        self.update()
                        if self.checkUpdate():
                            break
                        sleep(2)
                    self.noticeUpdateDevices()
                    self.runProcessAfterUpdateMicDevices()
                    self.runProcessAfterUpdateSpeakerDevices()
                except Exception:
                    errorLogging()
        except Exception:
            errorLogging()

    def startMonitoring(self):
        if self.monitoring_flag:
            return
        self.monitoring_flag = True
        self.th_monitoring = Thread(target=self.monitoring)
        self.th_monitoring.daemon = True
        self.th_monitoring.start()

    def stopMonitoring(self):
        self.monitoring_flag = False
        if getattr(self, "th_monitoring", None) is not None:
            try:
                self.th_monitoring.join(timeout=5)
            except Exception:
                # If join fails or thread is not joinable, ignore - it's a best-effort stop
                pass

    def setCallbackDefaultMicDevice(self, callback):
        self.callback_default_mic_device = callback

    def clearCallbackDefaultMicDevice(self):
        self.callback_default_mic_device = None

    def setCallbackDefaultSpeakerDevice(self, callback):
        self.callback_default_speaker_device = callback

    def clearCallbackDefaultSpeakerDevice(self):
        self.callback_default_speaker_device = None

    def setCallbackHostList(self, callback):
        self.callback_host_list = callback

    def clearCallbackHostList(self):
        self.callback_host_list = None

    def setCallbackMicDeviceList(self, callback):
        self.callback_mic_device_list = callback

    def clearCallbackMicDeviceList(self):
        self.callback_mic_device_list = None

    def setCallbackSpeakerDeviceList(self, callback):
        self.callback_speaker_device_list = callback

    def clearCallbackSpeakerDeviceList(self):
        self.callback_speaker_device_list = None

    def setCallbackProcessBeforeUpdateMicDevices(self, callback):
        self.callback_process_before_update_mic_devices = callback

    def clearCallbackProcessBeforeUpdateMicDevices(self):
        self.callback_process_before_update_mic_devices = None

    def runProcessBeforeUpdateMicDevices(self):
        if isinstance(self.callback_process_before_update_mic_devices, Callable):
            try:
                self.callback_process_before_update_mic_devices()
            except Exception:
                errorLogging()

    def setCallbackProcessAfterUpdateMicDevices(self, callback):
        self.callback_process_after_update_mic_devices = callback

    def clearCallbackProcessAfterUpdateMicDevices(self):
        self.callback_process_after_update_mic_devices = None

    def runProcessAfterUpdateMicDevices(self):
        if isinstance(self.callback_process_after_update_mic_devices, Callable):
            try:
                self.callback_process_after_update_mic_devices()
            except Exception:
                errorLogging()

    def setCallbackProcessBeforeUpdateSpeakerDevices(self, callback):
        self.callback_process_before_update_speaker_devices = callback

    def clearCallbackProcessBeforeUpdateSpeakerDevices(self):
        self.callback_process_before_update_speaker_devices = None

    def runProcessBeforeUpdateSpeakerDevices(self):
        if isinstance(self.callback_process_before_update_speaker_devices, Callable):
            try:
                self.callback_process_before_update_speaker_devices()
            except Exception:
                errorLogging()

    def setCallbackProcessAfterUpdateSpeakerDevices(self, callback):
        self.callback_process_after_update_speaker_devices = callback

    def clearCallbackProcessAfterUpdateSpeakerDevices(self):
        self.callback_process_after_update_speaker_devices = None

    def runProcessAfterUpdateSpeakerDevices(self):
        if isinstance(self.callback_process_after_update_speaker_devices, Callable):
            try:
                self.callback_process_after_update_speaker_devices()
            except Exception:
                errorLogging()

    def noticeUpdateDevices(self):
        if self.update_flag_default_mic_device is True:
            self.setMicDefaultDevice()
        if self.update_flag_default_speaker_device is True:
            self.setSpeakerDefaultDevice()
        if self.update_flag_host_list is True:
            self.setMicHostList()
        if self.update_flag_mic_device_list is True:
            self.setMicDeviceList()
        if self.update_flag_speaker_device_list is True:
            self.setSpeakerDeviceList()

        self.update_flag_default_mic_device = False
        self.update_flag_default_speaker_device = False
        self.update_flag_host_list = False
        self.update_flag_mic_device_list = False
        self.update_flag_speaker_device_list = False

    def setMicDefaultDevice(self):
        if isinstance(self.callback_default_mic_device, Callable):
            try:
                self.callback_default_mic_device(self.default_mic_device["host"]["name"], self.default_mic_device["device"]["name"])
            except Exception:
                errorLogging()

    def setSpeakerDefaultDevice(self):
        if isinstance(self.callback_default_speaker_device, Callable):
            try:
                self.callback_default_speaker_device(self.default_speaker_device["device"]["name"])
            except Exception:
                errorLogging()

    def setMicHostList(self):
        if isinstance(self.callback_host_list, Callable):
            try:
                self.callback_host_list()
            except Exception:
                errorLogging()

    def setMicDeviceList(self):
        if isinstance(self.callback_mic_device_list, Callable):
            try:
                self.callback_mic_device_list()
            except Exception:
                errorLogging()

    def setSpeakerDeviceList(self):
        if isinstance(self.callback_speaker_device_list, Callable):
            try:
                self.callback_speaker_device_list()
            except Exception:
                errorLogging()

    def getMicDevices(self):
        # Ensure initialized and return devices (safe default if still not populated)
        if not getattr(self, '_initialized', False):
            try:
                self.init()
            except Exception:
                try:
                    errorLogging()
                except Exception:
                    pass
        return getattr(self, 'mic_devices', {"NoHost": [{"index": -1, "name": "NoDevice"}]})

    def getDefaultMicDevice(self):
        # Ensure initialized and return default mic device (safe default if still not populated)
        if not getattr(self, '_initialized', False):
            try:
                self.init()
            except Exception:
                try:
                    errorLogging()
                except Exception:
                    pass
        return getattr(self, 'default_mic_device', {"host": {"index": -1, "name": "NoHost"}, "device": {"index": -1, "name": "NoDevice"}})

    def getSpeakerDevices(self):
        # Ensure initialized and return speaker devices (safe default if still not populated)
        if not getattr(self, '_initialized', False):
            try:
                self.init()
            except Exception:
                try:
                    errorLogging()
                except Exception:
                    pass
        return getattr(self, 'speaker_devices', [{"index": -1, "name": "NoDevice"}])

    def getDefaultSpeakerDevice(self):
        # Ensure initialized and return default speaker device (safe default if still not populated)
        if not getattr(self, '_initialized', False):
            try:
                self.init()
            except Exception:
                try:
                    errorLogging()
                except Exception:
                    pass
        return getattr(self, 'default_speaker_device', {"device": {"index": -1, "name": "NoDevice"}})

    def forceUpdateAndSetMicDevices(self):
        self.update()
        self.setMicHostList()
        self.setMicDeviceList()
        self.setMicDefaultDevice()

    def forceUpdateAndSetSpeakerDevices(self):
        self.update()
        self.setSpeakerDeviceList()
        self.setSpeakerDefaultDevice()

# Provide a module-level singleton. Call `device_manager.init()` explicitly to
# initialize audio resources and `device_manager.startMonitoring()` to begin
# background monitoring. This avoids side-effects during simple imports.
device_manager = DeviceManager()

if __name__ == "__main__":
    print("DeviceManager demo. Call device_manager.init() and device_manager.startMonitoring() to run live monitoring.")
    try:
        while True:
            sleep(1)
    except KeyboardInterrupt:
        print("exiting")