"""Recorders that wrap speech_recognition microphone interfaces.

These classes provide small adapters that push raw audio bytes into queues.
They intentionally keep a thin API so the rest of the system can mock them
in tests.
"""

import collections
import os
import subprocess
import sys
import threading
from typing import Any
import numpy as np
from speech_recognition import Recognizer, Microphone, AudioSource
try:
    from pyaudiowpatch import get_sample_size, paInt16
except ImportError:
    from pyaudio import get_sample_size, paInt16
from datetime import datetime


# ---------------------------------------------------------------------------
# PulseMonitorSource — captures desktop/speaker audio on Linux via parec
# ---------------------------------------------------------------------------

class PulseMonitorSource(AudioSource):
    """AudioSource that captures from a PulseAudio/PipeWire monitor source.

    On Linux, "speaker capture" means recording whatever audio is playing
    through an output device.  PulseAudio/PipeWire expose these as *monitor
    sources*.  This class uses ``parec`` to read raw PCM from a named monitor
    source, completely bypassing PyAudio and avoiding any env-var races.

    It implements the same interface as ``speech_recognition.Microphone`` so
    it can be used as a drop-in replacement wherever Recognizer expects an
    AudioSource.
    """

    def __init__(self, pulse_source_name: str, sample_rate: int = 48000,
                 chunk_size: int = 1024, channels: int = 2):
        self._pulse_source = pulse_source_name
        self.SAMPLE_RATE = sample_rate
        self.SAMPLE_WIDTH = 2  # 16-bit (s16le)
        self.CHUNK = chunk_size
        self.channels = channels
        self.stream = None
        self._process = None

    def __enter__(self):
        assert self.stream is None, "This audio source is already inside a context manager"
        self._process = subprocess.Popen(
            [
                "parec",
                "--device=" + self._pulse_source,
                "--format=float32le",
                "--channels=" + str(self.channels),
                "--rate=" + str(self.SAMPLE_RATE),
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.stream = PulseMonitorSource._ParecStream(self._process, self.CHUNK, self.channels)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        if self.stream is not None:
            self.stream = None

    class _ParecStream:
        """Wraps parec stdout to match the MicrophoneStream interface.

        A background reader thread continuously drains parec stdout into a
        small circular buffer (deque).  ``read()`` returns the most recent
        chunk, discarding anything older.  This prevents audio from
        accumulating in the pipe buffer while the Recognizer is busy
        processing a previous phrase — eliminating the "repeating/growing
        transcription" problem.

        speech_recognition.Recognizer accesses ``stream.pyaudio_stream``
        directly (e.g. ``get_read_available``), so this object also acts
        as its own ``pyaudio_stream`` with compatible stubs.
        """

        # Keep at most ~1 second of audio in the buffer.  Older chunks are
        # silently dropped so the Recognizer always gets fresh audio.
        _MAX_CHUNKS = 50

        # Monitor sources deliver quieter audio than direct mic input because
        # the signal passes through PipeWire's mixing/routing graph.  This
        # gain compensates so that speech_recognition's energy threshold
        # works at the same settings as for microphone input.
        _MONITOR_GAIN = 8.0

        def __init__(self, process, chunk_size, channels):
            self._process = process
            self._chunk_size = chunk_size
            self._channels = channels
            # parec outputs float32le (4 bytes/sample); we convert to s16le (2 bytes)
            self._read_size = chunk_size * channels * 4  # float32le input
            self._output_size = chunk_size * channels * 2  # s16le output
            self._silence = b"\x00" * self._output_size
            # Recognizer accesses stream.pyaudio_stream directly
            self.pyaudio_stream = self

            # Circular buffer + condition for blocking reads
            self._buf = collections.deque(maxlen=self._MAX_CHUNKS)
            self._cond = threading.Condition()
            self._stopped = False

            # Reader thread drains parec stdout continuously
            self._reader = threading.Thread(target=self._drain, daemon=True)
            self._reader.start()

        def _drain(self):
            """Continuously read from parec and push into the circular buffer.

            parec delivers float32le samples (PipeWire's native internal
            format).  We convert to s16le here so the rest of the pipeline
            (speech_recognition, Whisper) sees standard 16-bit PCM.  Using
            float32le avoids PipeWire's lossy s32le→s16le bit-depth
            conversion that crushes quiet audio signals.
            """
            try:
                while True:
                    data = self._process.stdout.read(self._read_size)
                    if not data:
                        break
                    # float32 [-1.0, 1.0] → int16 [-32768, 32767] with gain
                    f32 = np.frombuffer(data, dtype=np.float32)
                    s16 = np.clip(f32 * self._MONITOR_GAIN * 32767.0, -32768, 32767).astype(np.int16)
                    with self._cond:
                        self._buf.append(s16.tobytes())
                        self._cond.notify()
            finally:
                with self._cond:
                    self._stopped = True
                    self._cond.notify()

        def read(self, size, exception_on_overflow=False):
            with self._cond:
                # Wait until there is at least one chunk available
                while not self._buf and not self._stopped:
                    self._cond.wait(timeout=0.1)
                if self._buf:
                    return self._buf.popleft()
                return self._silence

        def get_read_available(self):
            # Always report data available so the Recognizer attempts a read
            # rather than immediately raising WaitTimeoutError.  read() blocks
            # briefly if the buffer is empty, matching real PyAudio behaviour.
            return self._chunk_size

        def is_stopped(self):
            return self._stopped

        def stop_stream(self):
            pass

        def close(self):
            pass  # cleanup handled by __exit__


def _is_linux():
    return sys.platform == "linux"


def _make_speaker_source(device: dict):
    """Create the appropriate audio source for speaker/desktop capture.

    On Linux with a ``_pulse_source`` key, returns a PulseMonitorSource
    (uses parec subprocess).  Otherwise falls back to Microphone(speaker=True)
    for Windows WASAPI loopback compatibility.
    """
    pulse_src = device.get("_pulse_source") if isinstance(device, dict) else None
    if pulse_src and _is_linux():
        sample_rate = int(device.get("defaultSampleRate", 48000))
        channels = int(device.get("maxInputChannels", 2))
        return PulseMonitorSource(
            pulse_source_name=pulse_src,
            sample_rate=sample_rate,
            channels=channels,
        )
    # Windows / fallback: use PyAudio WASAPI loopback via Microphone
    device_index = int(device.get('index', -1))
    sample_rate = int(device.get("defaultSampleRate", 16000))
    channels = int(device.get("maxInputChannels", 1))
    if device_index < 0:
        raise ValueError("invalid device index")
    return Microphone(
        speaker=True,
        device_index=device_index,
        sample_rate=sample_rate,
        chunk_size=get_sample_size(paInt16),
        channels=channels,
    )


# ---------------------------------------------------------------------------
# Recorder classes
# ---------------------------------------------------------------------------

class BaseRecorder:
    def __init__(self, source: Any, energy_threshold: int, dynamic_energy_threshold: bool, record_timeout: int) -> None:
        self.recorder = Recognizer()
        self.recorder.energy_threshold = energy_threshold
        self.recorder.dynamic_energy_threshold = dynamic_energy_threshold
        self.record_timeout = record_timeout
        self.stop = None

        if source is None:
            raise ValueError("audio source can't be None")

        self.source = source

    def adjustForNoise(self) -> None:
        with self.source:
            self.recorder.adjust_for_ambient_noise(self.source)

    def recordIntoQueue(self, audio_queue: Any) -> None:
        def record_callback(_, audio):
            audio_queue.put((audio.get_raw_data(), datetime.now()))

        self.stop, self.pause, self.resume = self.recorder.listen_in_background(self.source, record_callback, phrase_time_limit=self.record_timeout)


class SelectedMicRecorder(BaseRecorder):
    def __init__(self, device: dict, energy_threshold: int, dynamic_energy_threshold: bool, record_timeout: int) -> None:
        # Safely construct Microphone source. If device dict is missing expected keys
        # or index is out-of-range for the platform, fallback to default device (None)
        try:
            device_index = int(device.get('index', -1))
            sample_rate = int(device.get("defaultSampleRate", 16000))
            if device_index < 0:
                # invalid index -> fallback
                raise ValueError("invalid device index")
            source = Microphone(
                device_index=device_index,
                sample_rate=sample_rate,
            )
        except Exception:
            # Best-effort fallback: use system default microphone
            try:
                source = Microphone()
            except Exception:
                raise
        super().__init__(source=source, energy_threshold=energy_threshold, dynamic_energy_threshold=dynamic_energy_threshold, record_timeout=record_timeout)
        # self.adjustForNoise()


class SelectedSpeakerRecorder(BaseRecorder):
    def __init__(self, device: dict, energy_threshold: int, dynamic_energy_threshold: bool, record_timeout: int) -> None:
        try:
            source = _make_speaker_source(device)
        except Exception:
            try:
                source = Microphone(speaker=True)
            except Exception:
                raise
        super().__init__(source=source, energy_threshold=energy_threshold, dynamic_energy_threshold=dynamic_energy_threshold, record_timeout=record_timeout)
        # self.adjustForNoise()

class BaseEnergyRecorder:
    def __init__(self, source: Any) -> None:
        self.recorder = Recognizer()
        self.recorder.energy_threshold = 0
        self.recorder.dynamic_energy_threshold = False
        self.record_timeout = 0
        self.stop = None

        if source is None:
            raise ValueError("audio source can't be None")

        self.source = source

    def adjustForNoise(self) -> None:
        with self.source:
            self.recorder.adjust_for_ambient_noise(self.source)

    def recordIntoQueue(self, energy_queue: Any) -> None:
        def recordCallback(_, energy):
            energy_queue.put(energy)

        self.stop, self.pause, self.resume = self.recorder.listen_energy_in_background(self.source, recordCallback)


class SelectedMicEnergyRecorder(BaseEnergyRecorder):
    def __init__(self, device: dict) -> None:
        try:
            device_index = int(device.get('index', -1))
            sample_rate = int(device.get("defaultSampleRate", 16000))
            if device_index < 0:
                raise ValueError("invalid device index")
            source = Microphone(
                device_index=device_index,
                sample_rate=sample_rate,
            )
        except Exception:
            try:
                source = Microphone()
            except Exception:
                raise
        super().__init__(source=source)
        # self.adjustForNoise()


class SelectedSpeakerEnergyRecorder(BaseEnergyRecorder):
    def __init__(self, device: dict) -> None:
        try:
            source = _make_speaker_source(device)
        except Exception:
            try:
                source = Microphone(speaker=True)
            except Exception:
                raise
        super().__init__(source=source)
        # self.adjustForNoise()

class BaseEnergyAndAudioRecorder:
    def __init__(
        self,
        source: Any,
        energy_threshold: int,
        dynamic_energy_threshold: bool,
        phrase_time_limit: int,
        phrase_timeout: int,
        record_timeout: int,
    ) -> None:
        self.recorder = Recognizer()
        self.recorder.energy_threshold = energy_threshold
        self.recorder.dynamic_energy_threshold = dynamic_energy_threshold
        self.phrase_time_limit = phrase_time_limit
        self.phrase_timeout = phrase_timeout
        self.record_timeout = record_timeout
        self.stop = None

        if source is None:
            raise ValueError("audio source can't be None")

        self.source = source

    def adjustForNoise(self) -> None:
        with self.source:
            self.recorder.adjust_for_ambient_noise(self.source)

    def recordIntoQueue(self, audio_queue: Any, energy_queue: Any = None) -> None:
        def audioRecordCallback(_, audio):
            audio_queue.put((audio.get_raw_data(), datetime.now()))

        def energyRecordCallback(energy):
            energy_queue.put(energy)

        self.stop, self.pause, self.resume = self.recorder.listen_energy_and_audio_in_background(
            source=self.source,
            callback=audioRecordCallback,
            phrase_time_limit=self.phrase_time_limit,
            callback_energy=energyRecordCallback if energy_queue is not None else None,
            phrase_timeout=self.phrase_timeout,
            record_timeout=self.record_timeout,
        )


class SelectedMicEnergyAndAudioRecorder(BaseEnergyAndAudioRecorder):
    def __init__(
        self,
        device: dict,
        energy_threshold: int,
        dynamic_energy_threshold: bool,
        phrase_time_limit: int,
        phrase_timeout: int = 1,
        record_timeout: int = 5,
    ) -> None:
        try:
            device_index = int(device.get('index', -1))
            sample_rate = int(device.get("defaultSampleRate", 16000))
            if device_index < 0:
                raise ValueError("invalid device index")
            source = Microphone(
                device_index=device_index,
                sample_rate=sample_rate,
            )
        except Exception:
            try:
                source = Microphone()
            except Exception:
                raise
        super().__init__(
            source=source,
            energy_threshold=energy_threshold,
            dynamic_energy_threshold=dynamic_energy_threshold,
            phrase_time_limit=phrase_time_limit,
            phrase_timeout=phrase_timeout,
            record_timeout=record_timeout,
        )
        # self.adjustForNoise()


class SelectedSpeakerEnergyAndAudioRecorder(BaseEnergyAndAudioRecorder):
    def __init__(
        self,
        device: dict,
        energy_threshold: int,
        dynamic_energy_threshold: bool,
        phrase_time_limit: int,
        phrase_timeout: int = 1,
        record_timeout: int = 5,
    ) -> None:

        try:
            source = _make_speaker_source(device)
        except Exception:
            try:
                source = Microphone(speaker=True)
            except Exception:
                raise
        super().__init__(
            source=source,
            energy_threshold=energy_threshold,
            dynamic_energy_threshold=dynamic_energy_threshold,
            phrase_time_limit=phrase_time_limit,
            phrase_timeout=phrase_timeout,
            record_timeout=record_timeout,
        )
        # self.adjustForNoise()
