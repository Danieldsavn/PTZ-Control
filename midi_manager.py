"""
MIDI input (receive-only) — map note-on messages to switcher scene actions.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

try:
    import mido
except ImportError:
    mido = None  # type: ignore

SCENES: dict[str, dict[str, str]] = {
    "full_screen_camera": {
        "label": "Full Screen Camera",
        "description": "Splitview off, Lyrics off",
    },
    "splitview": {
        "label": "Splitview",
        "description": "Splitview on",
    },
    "camera_plus_lyrics": {
        "label": "Camera plus Lyrics",
        "description": "Lyrics on, Splitview off",
    },
}

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

LEARN_TIMEOUT_SEC = 15.0


def midi_available() -> bool:
    return mido is not None


def list_input_devices() -> list[dict[str, str]]:
    if not mido:
        return []
    try:
        return [{"name": n, "id": n} for n in mido.get_input_names()]
    except Exception:
        return []


def note_label(note: Optional[int]) -> str:
    if note is None or note < 0 or note > 127:
        return "—"
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1} ({note})"


def _default_midi_config() -> dict[str, Any]:
    return {
        "device": "",
        "notes": {scene_id: None for scene_id in SCENES},
    }


def normalize_midi_config(raw: Any) -> dict[str, Any]:
    base = _default_midi_config()
    if not isinstance(raw, dict):
        return base
    device = str(raw.get("device") or "").strip()
    notes_in = raw.get("notes")
    notes: dict[str, Optional[int]] = dict(base["notes"])
    if isinstance(notes_in, dict):
        for scene_id in SCENES:
            val = notes_in.get(scene_id)
            if val is None:
                notes[scene_id] = None
            else:
                try:
                    n = int(val)
                    notes[scene_id] = n if 0 <= n <= 127 else None
                except (TypeError, ValueError):
                    notes[scene_id] = None
    return {"device": device, "notes": notes}


class MidiManager:
    """Listen for MIDI note-on and trigger mapped switcher scenes."""

    def __init__(
        self,
        load_config: Callable[[], dict],
        save_midi_config: Callable[[dict], None],
        trigger_scene: Callable[[str], None],
    ):
        self._load_config = load_config
        self._save_midi_config = save_midi_config
        self._trigger_scene = trigger_scene
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listen_thread: Optional[threading.Thread] = None
        self._port: Any = None
        self._learn_scene: Optional[str] = None
        self._learn_timer: Optional[threading.Timer] = None
        self._learn_message: str = ""
        self._last_learned: Optional[dict[str, Any]] = None
        self._midi_cfg = normalize_midi_config({})
        self.reload_config()
        self.start()

    def reload_config(self) -> None:
        cfg = self._load_config()
        midi_raw = cfg.get("midi") if isinstance(cfg, dict) else None
        with self._lock:
            self._midi_cfg = normalize_midi_config(midi_raw)
        self._restart_listener()

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            notes = dict(self._midi_cfg["notes"])
            device = self._midi_cfg["device"]
            learn = self._learn_scene
            learn_message = self._learn_message
            last = dict(self._last_learned) if self._last_learned else None
        return {
            "available": midi_available(),
            "device": device,
            "notes": notes,
            "note_labels": {sid: note_label(n) for sid, n in notes.items()},
            "scenes": [
                {
                    "id": sid,
                    "label": SCENES[sid]["label"],
                    "description": SCENES[sid]["description"],
                    "note": notes.get(sid),
                    "note_label": note_label(notes.get(sid)),
                }
                for sid in SCENES
            ],
            "learning": learn,
            "learn_message": learn_message,
            "last_learned": last,
            "listening": self._listen_thread is not None and self._listen_thread.is_alive(),
        }

    def set_device(self, device_name: str) -> tuple[bool, str]:
        name = (device_name or "").strip()
        with self._lock:
            self._midi_cfg["device"] = name
            self._persist_locked()
        self._restart_listener()
        if name and not self._device_exists(name):
            return False, f"MIDI device not found: {name}"
        return True, "MIDI device saved"

    def set_note(self, scene_id: str, note: Optional[int]) -> tuple[bool, str]:
        if scene_id not in SCENES:
            return False, f"Unknown scene: {scene_id}"
        if note is not None and not (0 <= int(note) <= 127):
            return False, "Note must be 0–127"
        with self._lock:
            self._midi_cfg["notes"][scene_id] = int(note) if note is not None else None
            self._persist_locked()
        return True, "Note saved"

    def start_learn(self, scene_id: str) -> tuple[bool, str]:
        if not midi_available():
            return False, "MIDI not available (install mido and python-rtmidi)"
        if scene_id not in SCENES:
            return False, f"Unknown scene: {scene_id}"
        with self._lock:
            if self._learn_scene == scene_id:
                self._learn_scene = None
                self._learn_message = "Learn cancelled"
                cancel_only = True
            else:
                self._learn_scene = scene_id
                self._last_learned = None
                self._learn_message = ""
                cancel_only = False
        self._disarm_learn_timer()
        if cancel_only:
            return True, "Learn cancelled"
        self._arm_learn_timer()
        label = SCENES[scene_id]["label"]
        return (
            True,
            f"Learning MIDI note for {label}… (play a note within {int(LEARN_TIMEOUT_SEC)}s)",
        )

    def clear_note(self, scene_id: str) -> tuple[bool, str]:
        return self.set_note(scene_id, None)

    def cancel_learn(self) -> tuple[bool, str]:
        with self._lock:
            was_learning = self._learn_scene is not None
            self._learn_scene = None
            if was_learning:
                self._learn_message = "Learn cancelled"
        self._disarm_learn_timer()
        return True, "Learn cancelled" if was_learning else ""

    def _arm_learn_timer(self) -> None:
        self._disarm_learn_timer()

        def on_timeout() -> None:
            with self._lock:
                scene = self._learn_scene
                self._learn_scene = None
                self._learn_timer = None
                if scene:
                    label = SCENES[scene]["label"]
                    self._learn_message = (
                        f"No MIDI note received in {int(LEARN_TIMEOUT_SEC)}s ({label})"
                    )

        timer = threading.Timer(LEARN_TIMEOUT_SEC, on_timeout)
        timer.daemon = True
        with self._lock:
            self._learn_timer = timer
        timer.start()

    def _disarm_learn_timer(self) -> None:
        with self._lock:
            timer = self._learn_timer
            self._learn_timer = None
        if timer is not None:
            timer.cancel()

    def _persist_locked(self) -> None:
        self._save_midi_config(
            {
                "device": self._midi_cfg["device"],
                "notes": dict(self._midi_cfg["notes"]),
            }
        )

    def _device_exists(self, name: str) -> bool:
        return any(d["name"] == name for d in list_input_devices())

    def start(self) -> None:
        self._stop.clear()
        self._restart_listener()

    def shutdown(self) -> None:
        self._stop.set()
        self._disarm_learn_timer()
        with self._lock:
            self._learn_scene = None
        self._close_port()
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=2.0)
        self._listen_thread = None

    def _restart_listener(self) -> None:
        self._close_port()
        if self._stop.is_set() or not midi_available():
            return
        with self._lock:
            device = self._midi_cfg.get("device") or ""
        if not device:
            return
        self._listen_thread = threading.Thread(
            target=self._listen_loop, args=(device,), daemon=True
        )
        self._listen_thread.start()

    def _close_port(self) -> None:
        port = self._port
        self._port = None
        if port:
            try:
                port.close()
            except Exception:
                pass

    def _listen_loop(self, device_name: str) -> None:
        try:
            port = mido.open_input(device_name)
        except Exception:
            return
        self._port = port
        try:
            while not self._stop.is_set():
                try:
                    for msg in port.iter_pending():
                        self._handle_message(msg)
                    msg = port.poll()
                    if msg is not None:
                        self._handle_message(msg)
                except Exception:
                    break
                time.sleep(0.005)
        finally:
            try:
                port.close()
            except Exception:
                pass
            if self._port is port:
                self._port = None

    def _handle_message(self, msg: Any) -> None:
        if not hasattr(msg, "type") or msg.type != "note_on":
            return
        vel = getattr(msg, "velocity", 0)
        if vel <= 0:
            return
        note = int(msg.note)

        learn_scene: Optional[str] = None
        with self._lock:
            if self._learn_scene:
                learn_scene = self._learn_scene
                self._midi_cfg["notes"][learn_scene] = note
                self._learn_scene = None
                self._learn_message = (
                    f"Mapped {note_label(note)} to {SCENES[learn_scene]['label']}"
                )
                self._last_learned = {"scene": learn_scene, "note": note}
                self._persist_locked()
        if learn_scene:
            self._disarm_learn_timer()
            return

        scene_to_run: Optional[str] = None
        with self._lock:
            for scene_id, mapped in self._midi_cfg["notes"].items():
                if mapped is not None and int(mapped) == note:
                    scene_to_run = scene_id
                    break
        if scene_to_run:
            threading.Thread(
                target=self._run_scene_safe,
                args=(scene_to_run,),
                daemon=True,
            ).start()

    def _run_scene_safe(self, scene_id: str) -> None:
        try:
            self._trigger_scene(scene_id)
        except Exception:
            pass
