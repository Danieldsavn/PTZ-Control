"""
ProPresenter service cues (Pre Service, Worship, Sermon, End of Service) — multi-step sequences
with deduplication, cancellable execution, and UI progress events.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import app_log

SERVICE_SCENE_PART: dict[str, str] = {
    "pre_service_transition": "pre_service",
    "worship_transition": "worship",
    "sermon_transition": "sermon",
    "end_service_transition": "end",
}

SERVICE_SCENE_LABEL: dict[str, str] = {
    "pre_service_transition": "Pre Service",
    "worship_transition": "Worship",
    "sermon_transition": "Sermon",
    "end_service_transition": "End of Service",
}

SERVICE_CUE_COOLDOWN_S = 30.0


class ServiceCueCancelled(Exception):
    pass


class ServiceCueController:
    def __init__(
        self,
        push_ui_event: Callable[[dict[str, Any]], None],
        get_switcher: Callable[[], Any],
        non_live_cam: Callable[[Any], str],
        tracking_set: Callable[[str, bool], tuple[bool, str]],
        recall_preset: Callable[[str, int], tuple[bool, str]],
    ) -> None:
        self._push_ui = push_ui_event
        self._get_switcher = get_switcher
        self._non_live_cam = non_live_cam
        self._tracking_set = tracking_set
        self._recall_preset = recall_preset
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_part: str | None = None
        self._last_started_at: dict[str, float] = {}
        self._ends_at: float | None = None
        self._started_at: float | None = None
        self._total_sec: float = 0.0
        self._active = False

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._active or (self._thread and self._thread.is_alive()))

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            return {
                "running": running,
                "part": self._current_part,
            }

    def cancel(self) -> tuple[bool, str]:
        self._cancel.set()
        return True, "Service cue cancel requested"

    def try_start(self, scene_id: str) -> tuple[bool, str]:
        part = SERVICE_SCENE_PART.get(scene_id)
        if not part:
            return False, "Not a service cue"

        now = time.monotonic()
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False, "Service cue already running"
            if self._current_part == part:
                return False, f"Already in {SERVICE_SCENE_LABEL.get(scene_id, part)}"
            last = self._last_started_at.get(scene_id, 0.0)
            if now - last < SERVICE_CUE_COOLDOWN_S:
                return False, "Service cue blocked (30s cooldown)"
            self._cancel.clear()
            self._last_started_at[scene_id] = now
            self._current_part = part

        runner = {
            "pre_service_transition": self._run_pre_service,
            "worship_transition": self._run_worship,
            "sermon_transition": self._run_sermon,
            "end_service_transition": self._run_end,
        }.get(scene_id)
        if not runner:
            return False, "Unknown service cue"

        thread = threading.Thread(
            target=self._run_wrapper,
            args=(scene_id, runner),
            name=f"ServiceCue-{part}",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
            self._active = True
        thread.start()
        return True, "Service cue started"

    def _run_wrapper(self, scene_id: str, runner: Callable[[], None]) -> None:
        try:
            runner()
            self._push_ui({"type": "service_cue_end", "cue": scene_id})
            app_log.midi("Service cue complete", {"cue": scene_id})
        except ServiceCueCancelled:
            self._push_ui({"type": "service_cue_cancelled", "cue": scene_id})
            app_log.midi("Service cue cancelled", {"cue": scene_id})
        except Exception as e:
            app_log.midi("Service cue failed", {"cue": scene_id, "error": str(e)})
            self._push_ui({"type": "service_cue_end", "cue": scene_id, "error": str(e)})
        finally:
            self._ends_at = None
            self._started_at = None
            self._total_sec = 0.0
            with self._lock:
                self._active = False
                if self._thread is threading.current_thread():
                    self._thread = None

    def _remaining(self) -> float:
        if self._ends_at is None:
            return 0.0
        return max(0.0, self._ends_at - time.monotonic())

    def _sleep(self, seconds: float, scene_id: str, step_index: int, steps: list[str]) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        if self._ends_at is not None:
            deadline = min(deadline, self._ends_at)
        while True:
            if self._cancel.is_set():
                raise ServiceCueCancelled()
            self._progress(scene_id, step_index, steps)
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, deadline - time.monotonic()))

    def _wait_until_end(self, scene_id: str, step_index: int, steps: list[str]) -> None:
        """Hold until the cue-wide countdown finishes."""
        while self._remaining() > 0:
            if self._cancel.is_set():
                raise ServiceCueCancelled()
            self._progress(scene_id, step_index, steps)
            time.sleep(min(0.25, self._remaining()))

    def _progress(
        self,
        scene_id: str,
        step_index: int,
        steps: list[str],
    ) -> None:
        self._push_ui(
            {
                "type": "service_cue_progress",
                "cue": scene_id,
                "part": SERVICE_SCENE_PART.get(scene_id),
                "label": SERVICE_SCENE_LABEL.get(scene_id, scene_id),
                "step_index": step_index,
                "steps": steps,
                "total_sec": int(max(1, round(self._total_sec))),
            }
        )

    def _begin(self, scene_id: str, steps: list[str], total_sec: float) -> None:
        self._total_sec = max(0.0, total_sec)
        self._started_at = time.monotonic()
        self._ends_at = self._started_at + self._total_sec
        self._push_ui(
            {
                "type": "service_cue_start",
                "cue": scene_id,
                "part": SERVICE_SCENE_PART.get(scene_id),
                "label": SERVICE_SCENE_LABEL.get(scene_id, scene_id),
                "steps": steps,
                "total_sec": int(max(1, round(self._total_sec))),
                "step_index": 0,
            }
        )
        app_log.midi("Service cue started", {"cue": scene_id, "label": SERVICE_SCENE_LABEL.get(scene_id)})

    def _step(
        self,
        scene_id: str,
        step_index: int,
        steps: list[str],
    ) -> None:
        self._progress(scene_id, step_index, steps)
        if self._cancel.is_set():
            raise ServiceCueCancelled()

    @staticmethod
    def _other_cam(cam: str) -> str:
        return "cam2" if cam == "cam1" else "cam1"

    def _tracking_off_both(self, scene_id: str, step_index: int, steps: list[str]) -> None:
        self._step(scene_id, step_index, steps)
        for cam in ("cam1", "cam2"):
            self._tracking_set(cam, False)
            self._push_ui({"type": "tracking", "cam": cam, "on": False})

    def _recall(self, cam: str, preset: int) -> None:
        self._recall_preset(cam, preset)
        self._push_ui({"type": "preset", "cam": cam, "preset": preset})

    def _lyrics(self, on: bool) -> None:
        sm = self._get_switcher()
        sm.usk1_set(on)
        self._push_ui({"type": "cue", "cue": "lyrics_on" if on else "lyrics_off"})

    def _title(self, on: bool) -> None:
        sm = self._get_switcher()
        sm.dsk1_set(on)
        self._push_ui({"type": "cue", "cue": "title_on" if on else "title_off"})

    def _fade(self, cam: str) -> None:
        sm = self._get_switcher()
        ok, msg = sm.fade_camera(cam)
        if not ok:
            raise RuntimeError(msg or "Auto transition failed")
        self._push_ui({"type": "camera_take", "cam": cam, "method": "fade"})

    def _cut(self, cam: str) -> None:
        sm = self._get_switcher()
        ok, msg = sm.cut_camera(cam)
        if not ok:
            raise RuntimeError(msg or "Cut failed")
        self._push_ui({"type": "camera_take", "cam": cam, "method": "cut"})

    def _full_screen_slide(self) -> None:
        sm = self._get_switcher()
        ok, msg = sm.full_screen_slide_on()
        if not ok:
            raise RuntimeError(msg or "Full screen slide failed")
        self._push_ui({"type": "cue", "cue": "full_screen_slide_auto"})

    def _run_pre_service(self) -> None:
        scene_id = "pre_service_transition"
        steps = [
            "Turn title off",
            "Turn ProPresenter layer off",
            "Go to full screen slide",
        ]
        total = 5.0
        self._begin(scene_id, steps, total)

        self._step(scene_id, 0, steps)
        self._title(False)
        self._step(scene_id, 1, steps)
        self._lyrics(False)
        self._step(scene_id, 2, steps)
        self._full_screen_slide()

        self._wait_until_end(scene_id, 2, steps)

    def _run_worship(self) -> None:
        scene_id = "worship_transition"
        steps = [
            "Turn off tracking on both cameras",
            "Standby camera → preset 11, lyrics on",
            "Auto transition to standby camera",
        ]
        # 3s preset wait + ~3s for recall, lyrics, and AUTO fade
        total = 6.0
        self._begin(scene_id, steps, total)
        sm = self._get_switcher()
        standby = self._non_live_cam(sm)

        self._tracking_off_both(scene_id, 0, steps)
        self._step(scene_id, 1, steps)
        self._recall(standby, 11)
        self._lyrics(True)

        self._sleep(3.0, scene_id, 1, steps)
        self._step(scene_id, 2, steps)
        self._fade(standby)
        self._wait_until_end(scene_id, 2, steps)

    def _run_sermon(self) -> None:
        scene_id = "sermon_transition"
        steps = [
            "Turn off tracking on both cameras",
            "Standby camera → preset 3, lyrics off",
            "Auto transition to standby camera",
            "Other camera → preset 1, tracking on",
            "Cut to tracking camera",
            "Title on, then off",
        ]
        # 20s of waits + ~5s for switcher/camera actions
        total = 25.0
        self._begin(scene_id, steps, total)
        sm = self._get_switcher()
        standby = self._non_live_cam(sm)

        self._tracking_off_both(scene_id, 0, steps)
        self._step(scene_id, 1, steps)
        self._recall(standby, 3)
        self._lyrics(False)

        self._sleep(3.0, scene_id, 1, steps)
        self._step(scene_id, 2, steps)
        self._fade(standby)

        self._sleep(3.0, scene_id, 2, steps)
        tracking_cam = self._other_cam(standby)
        self._step(scene_id, 3, steps)
        self._recall(tracking_cam, 1)

        self._sleep(3.0, scene_id, 3, steps)
        self._tracking_set(tracking_cam, True)
        self._push_ui({"type": "tracking", "cam": tracking_cam, "on": True})

        self._sleep(2.0, scene_id, 3, steps)
        self._step(scene_id, 4, steps)
        self._cut(tracking_cam)

        self._sleep(2.0, scene_id, 4, steps)
        self._step(scene_id, 5, steps)
        self._title(True)

        self._sleep(7.0, scene_id, 5, steps)
        self._title(False)
        self._wait_until_end(scene_id, 5, steps)

    def _run_end(self) -> None:
        scene_id = "end_service_transition"
        steps = [
            "Turn off tracking on both cameras",
            "Standby camera → preset 11, lyrics off",
            "Auto transition to standby camera",
        ]
        total = 6.0
        self._begin(scene_id, steps, total)
        sm = self._get_switcher()
        standby = self._non_live_cam(sm)

        self._tracking_off_both(scene_id, 0, steps)
        self._step(scene_id, 1, steps)
        self._recall(standby, 11)
        self._lyrics(False)

        self._sleep(3.0, scene_id, 1, steps)
        self._step(scene_id, 2, steps)
        self._fade(standby)
        self._wait_until_end(scene_id, 2, steps)
