"""
MIDI input (receive-only) — map note-on messages to switcher scene actions.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Optional

import app_log

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
    "god_bless_screen": {
        "label": "God Bless Screen",
        "description": "Still 5 full screen; Cut/Stills/Title restore prior still",
    },
}

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

LEARN_TIMEOUT_SEC = 15.0

_mido_backend_ready = False


def _ensure_mido_backend() -> None:
    global _mido_backend_ready
    if _mido_backend_ready or not mido:
        return
    if sys.platform == "win32":
        try:
            mido.set_backend("mido.backends.rtmidi")
        except Exception:
            pass
    _mido_backend_ready = True


def midi_available() -> bool:
    if mido is None:
        return False
    _ensure_mido_backend()
    return True


def list_input_devices() -> list[dict[str, str]]:
    if not midi_available():
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


def _note_press(msg: Any, active_notes: set[int]) -> Optional[int]:
    """
    Return note number when a key/pad is pressed.

    Handles normal note_on (velocity > 0) and controllers that only send
    note_on with velocity 0 for a press. Ignores release (note_off or
    note_on velocity 0 after the note was already active).
    """
    if not hasattr(msg, "type"):
        return None
    if msg.type == "note_on":
        note = int(msg.note)
        vel = int(getattr(msg, "velocity", 0) or 0)
        if vel > 0:
            active_notes.add(note)
            return note
        was_active = note in active_notes
        active_notes.discard(note)
        if not was_active:
            return note
        return None
    if msg.type == "note_off":
        active_notes.discard(int(msg.note))
    return None


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
        self._learn_deadline: Optional[float] = None
        self._learn_message: str = ""
        self._last_learned: Optional[dict[str, Any]] = None
        self._midi_cfg = normalize_midi_config({})
        self._active_notes: set[int] = set()
        self._last_trigger_at: dict[int, float] = {}
        self.reload_config()
        self.start()

    def reload_config(self) -> None:
        cfg = self._load_config()
        midi_raw = cfg.get("midi") if isinstance(cfg, dict) else None
        with self._lock:
            old_device = (self._midi_cfg.get("device") or "").strip()
            self._midi_cfg = normalize_midi_config(midi_raw)
            new_device = (self._midi_cfg.get("device") or "").strip()
        if old_device != new_device:
            self._restart_listener()
        elif new_device and not self._listener_alive():
            self._restart_listener()

    def _listener_alive(self) -> bool:
        return self._listen_thread is not None and self._listen_thread.is_alive()

    def _sync_learn_timeout_locked(self) -> None:
        if (
            self._learn_scene
            and self._learn_deadline is not None
            and time.time() >= self._learn_deadline
        ):
            self._finish_learn_timeout_locked()

    def _finish_learn_timeout_locked(self) -> None:
        scene = self._learn_scene
        self._learn_scene = None
        self._learn_deadline = None
        if scene:
            label = SCENES[scene]["label"]
            self._learn_message = (
                f"No MIDI note received in {int(LEARN_TIMEOUT_SEC)}s ({label})"
            )
            app_log.midi("Learn timeout", {"scene": scene, "label": label})

    def get_config(self) -> dict[str, Any]:
        device = ""
        with self._lock:
            self._sync_learn_timeout_locked()
            notes = dict(self._midi_cfg["notes"])
            device = (self._midi_cfg.get("device") or "").strip()
            learn = self._learn_scene
            learn_message = self._learn_message
            last = dict(self._last_learned) if self._last_learned else None
            learn_expires_at = self._learn_deadline
        if device and not self._listener_alive():
            self._restart_listener()
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
            "learn_expires_at": learn_expires_at,
            "learn_timeout_sec": int(LEARN_TIMEOUT_SEC),
            "last_learned": last,
            "listening": self._listener_alive(),
        }

    def set_device(self, device_name: str) -> tuple[bool, str]:
        name = (device_name or "").strip()
        with self._lock:
            self._midi_cfg["device"] = name
            self._persist_locked()
        self._restart_listener()
        app_log.midi("Device saved", {"device": name or "(none)"})
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
        self._disarm_learn_timer()
        with self._lock:
            if self._learn_scene == scene_id:
                self._learn_scene = None
                self._learn_deadline = None
                self._learn_message = "Learn cancelled"
                cancel_only = True
            else:
                self._learn_scene = scene_id
                self._learn_deadline = time.time() + LEARN_TIMEOUT_SEC
                self._last_learned = None
                self._learn_message = ""
                cancel_only = False
        if cancel_only:
            return True, "Learn cancelled"
        if not self._listener_alive():
            self._restart_listener()
        self._arm_learn_timer()
        label = SCENES[scene_id]["label"]
        app_log.midi("Learn started", {"scene": scene_id, "label": label})
        return (
            True,
            f"Learning MIDI note for {label}… (play a note within {int(LEARN_TIMEOUT_SEC)}s)",
        )

    def clear_note(self, scene_id: str) -> tuple[bool, str]:
        return self.set_note(scene_id, None)

    def cancel_learn(self) -> tuple[bool, str]:
        self._disarm_learn_timer()
        with self._lock:
            was_learning = self._learn_scene is not None
            self._learn_scene = None
            self._learn_deadline = None
            if was_learning:
                self._learn_message = "Learn cancelled"
        return True, "Learn cancelled" if was_learning else ""

    def _arm_learn_timer(self) -> None:
        self._disarm_learn_timer()

        def on_timeout() -> None:
            with self._lock:
                self._finish_learn_timeout_locked()
                self._learn_timer = None

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
            self._learn_deadline = None
        self._close_port()
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=2.0)
        self._listen_thread = None

    def _restart_listener(self) -> None:
        old_thread = self._listen_thread
        self._close_port()
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=1.0)
        if self._stop.is_set() or not midi_available():
            return
        with self._lock:
            device = (self._midi_cfg.get("device") or "").strip()
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
        _ensure_mido_backend()
        try:
            port = mido.open_input(device_name)
            app_log.midi("Listener started", {"device": device_name})
        except Exception as e:
            app_log.midi("Listener open failed", {"device": device_name, "error": str(e)})
            return
        self._port = port
        try:
            while not self._stop.is_set():
                try:
                    msg = port.receive(block=True)
                except Exception:
                    break
                if self._stop.is_set():
                    break
                self._handle_message(msg)
        finally:
            try:
                port.close()
            except Exception:
                pass
            if self._port is port:
                self._port = None

    def _handle_message(self, msg: Any) -> None:
        note = _note_press(msg, self._active_notes)
        if note is None:
            return

        learn_scene: Optional[str] = None
        with self._lock:
            learn_scene = self._learn_scene

        if learn_scene:
            with self._lock:
                if self._learn_scene != learn_scene:
                    return
                self._midi_cfg["notes"][learn_scene] = note
                self._learn_scene = None
                self._learn_deadline = None
                self._learn_message = (
                    f"Mapped {note_label(note)} to {SCENES[learn_scene]['label']}"
                )
                self._last_learned = {"scene": learn_scene, "note": note}
                self._persist_locked()
            app_log.midi(
                "Learn mapped",
                {"scene": learn_scene, "note": note, "label": note_label(note)},
            )
            self._disarm_learn_timer()
            return

        now = time.monotonic()
        last = self._last_trigger_at.get(note, 0.0)
        if now - last < 0.12:
            return
        self._last_trigger_at[note] = now

        scene_to_run: Optional[str] = None
        with self._lock:
            if self._learn_scene is not None:
                return
            for scene_id, mapped in self._midi_cfg["notes"].items():
                if mapped is not None and int(mapped) == note:
                    scene_to_run = scene_id
                    break
        if scene_to_run:
            app_log.midi(
                "Note press → scene",
                {"note": note, "scene": scene_to_run},
            )
            threading.Thread(
                target=self._run_scene_safe,
                args=(scene_to_run, note),
                daemon=True,
            ).start()

    def _run_scene_safe(self, scene_id: str, note: int) -> None:
        try:
            self._trigger_scene(scene_id)
        except Exception as exc:
            app_log.exception(
                "MIDI",
                f"Scene {scene_id} (note {note}) failed",
                exc,
            )
