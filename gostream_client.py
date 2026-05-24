"""
OSEE GoStream (GSP) TCP client — used by GoStream Duet / Deck switchers.
Protocol: port 19010, packets with header 0xEB 0xA6, JSON command body, CRC-16 Modbus.

This client intentionally does NOT send commands that rename inputs or change switcher UI
labels (e.g. deviceName, inputMode, per-source custom names). Only bus routing, keys,
stream output, stills, and multisource layout are controlled.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any, Callable, Optional

HEAD1 = 0xEB
HEAD2 = 0xA6
PROTO_ID = 0x00
PACKET_HEAD = bytes([HEAD1, HEAD2])
PACKET_HEADER_SIZE = 5
DEFAULT_PORT = 19010

# GoStream source IDs (SDI / HDMI inputs per GSP sourceID enum)
SOURCE_IN1 = 1
SOURCE_IN2 = 2
SOURCE_IN3 = 3  # HDMI 3
SOURCE_IN4 = 4  # HDMI 4
SOURCE_MP = 3010  # Still 1 (downstream key / Sid)
SOURCE_MP2 = 3020  # Still 2 (multisource background / Nate)

CMD_PGM_INDEX = "pgmIndex"
CMD_PVW_INDEX = "pvwIndex"
CMD_CUT = "cutTransition"
CMD_LIVE = "live"
CMD_KEY_ON_AIR = "keyOnAir"
CMD_KEY_TYPE = "keyType"
CMD_LUMA_FILL_SOURCE = "lumaFillSource"
CMD_LUMA_KEY_SOURCE = "lumaKeySource"
CMD_DSK_ON_AIR = "dskOnAir"
CMD_DSK_FILL_SOURCE = "dskFillSource"
CMD_LIVE_STREAM_OUTPUT_ENABLE = "liveStreamOutputEnable"
CMD_LIVE_STREAM_OUTPUT_STATUS = "liveStreamOutputStatus"
CMD_LIVE_STREAM_OUTPUT_KEY = "liveStreamOutputKey"
CMD_MEDIA_PLAYER = "mediaPlayer"
CMD_MULTI_SOURCE_ENABLE = "multiSourceEnable"
CMD_MULTI_SOURCE_FILL_SOURCE = "multiSourceFillSource"
CMD_MULTI_SOURCE_WINDOW_SOURCE = "multiSourceWindowSource"
CMD_TRANSITION_STYLE = "transitionStyle"
CMD_TRANSITION_MIX_RATE = "transitionMixRate"
CMD_DSK_RATE = "dskRate"
CMD_AUTO_TRANSITION = "autoTransition"

# Whitelist of GSP commands this app may send (blocks label/name/config commands).
ALLOWED_SET_CMDS = frozenset({
    CMD_PGM_INDEX,
    CMD_PVW_INDEX,
    CMD_CUT,
    CMD_LIVE,
    CMD_KEY_ON_AIR,
    CMD_KEY_TYPE,
    CMD_LUMA_FILL_SOURCE,
    CMD_LUMA_KEY_SOURCE,
    CMD_DSK_ON_AIR,
    CMD_DSK_FILL_SOURCE,
    CMD_LIVE_STREAM_OUTPUT_ENABLE,
    CMD_LIVE_STREAM_OUTPUT_KEY,
    CMD_MEDIA_PLAYER,
    CMD_MULTI_SOURCE_ENABLE,
    CMD_MULTI_SOURCE_FILL_SOURCE,
    CMD_MULTI_SOURCE_WINDOW_SOURCE,
    CMD_TRANSITION_STYLE,
    CMD_TRANSITION_MIX_RATE,
    CMD_DSK_RATE,
    CMD_AUTO_TRANSITION,
})
ALLOWED_GET_CMDS = frozenset({
    CMD_PGM_INDEX,
    CMD_PVW_INDEX,
    CMD_LIVE,
    CMD_KEY_ON_AIR,
    CMD_KEY_TYPE,
    CMD_LUMA_FILL_SOURCE,
    CMD_LUMA_KEY_SOURCE,
    CMD_DSK_ON_AIR,
    CMD_DSK_FILL_SOURCE,
    CMD_MEDIA_PLAYER,
    CMD_LIVE_STREAM_OUTPUT_KEY,
    CMD_LIVE_STREAM_OUTPUT_ENABLE,
    CMD_LIVE_STREAM_OUTPUT_STATUS,
    CMD_MULTI_SOURCE_ENABLE,
    CMD_MULTI_SOURCE_WINDOW_SOURCE,
})

SOURCE_MULTI = 5001
STREAM_1_ID = 0
STREAM_2_ID = 1
USK_1_KEY_ID = 0
USK_KEY_TYPE_LUMA = 0
DSK_1_KEY_ID = 0
DSK_FILL_STILL_1 = SOURCE_MP
MEDIA_PLAYER_1_ID = 0
# GoStream Duet: Source 1 / Source 2 (0-based window ids)
MULTISOURCE_WINDOW_1_ID = 0
MULTISOURCE_WINDOW_2_ID = 1
MULTISOURCE_BACKGROUND_FILL = SOURCE_MP2


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def pack_packet(json_bytes: bytes) -> bytes:
    packet_len = len(json_bytes) + 7
    packet = bytearray(packet_len)
    packet[0] = HEAD1
    packet[1] = HEAD2
    packet[2] = PROTO_ID
    struct.pack_into("<H", packet, 3, packet_len - 5)
    packet[5 : 5 + len(json_bytes)] = json_bytes
    crc = crc16_modbus(bytes(packet[: packet_len - 2]))
    struct.pack_into("<H", packet, packet_len - 2, crc)
    return bytes(packet)


def _unpack_one_packet(packet: bytes) -> dict[str, Any]:
    if len(packet) < 7 or packet[0] != HEAD1 or packet[1] != HEAD2:
        raise ValueError("invalid packet header")
    resp_len = struct.unpack_from("<H", packet, 3)[0]
    if resp_len != len(packet) - 5:
        raise ValueError("invalid packet length")
    recv_crc = struct.unpack_from("<H", packet, len(packet) - 2)[0]
    if recv_crc != crc16_modbus(packet[: len(packet) - 2]):
        raise ValueError("crc mismatch")
    body = packet[5 : len(packet) - 2]
    return json.loads(body.decode("utf-8"))


class GoStreamClient:
    """Thread-safe TCP client for OSEE GoStream switchers."""

    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._recv_buffer = bytearray()
        self._partial: Optional[bytearray] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = False
        self._pgm_src: Optional[int] = None
        self._pvw_src: Optional[int] = None
        self._stream_live_on = False
        self._stream1_output_live = False
        self._stream2_output_live = False
        self._stream1_output_enabled: Optional[bool] = None
        self._stream2_output_enabled: Optional[bool] = None
        self._usk1_on = False
        self._usk1_key_type: Optional[int | str] = None
        self._usk1_luma_fill: Optional[int] = None
        self._usk1_luma_key: Optional[int] = None
        self._dsk1_on = False
        self._dsk1_fill_src: Optional[int] = None
        self._mp1_still_index_1based: Optional[int] = None
        self._stream1_rtmp_key = ""
        self._multisource_enabled = False
        self._multisource_fill_src: Optional[int] = None
        self._multisource_window1_src: Optional[int] = None
        self._multisource_window2_src: Optional[int] = None
        self._on_command: Optional[Callable[[dict[str, Any]], None]] = None

    @property
    def connected(self) -> bool:
        return self._connected and self._sock is not None

    @property
    def pgm_src(self) -> Optional[int]:
        return self._pgm_src

    @property
    def pvw_src(self) -> Optional[int]:
        return self._pvw_src

    @property
    def stream_live_on(self) -> bool:
        return self._stream_live_on or self._stream1_output_live

    @property
    def usk1_on(self) -> bool:
        return self._usk1_on

    @property
    def dsk1_on(self) -> bool:
        return self._dsk1_on

    @property
    def mp1_still_index(self) -> Optional[int]:
        """1-based still slot on media player 1 (Sid=1, Nate=2, Cecil=3)."""
        if self._mp1_still_index_1based is not None:
            return self._mp1_still_index_1based
        return self.still_index_from_fill_source(self._dsk1_fill_src)

    @staticmethod
    def still_index_from_fill_source(source_id: Optional[int]) -> Optional[int]:
        if source_id is None:
            return None
        try:
            sid = int(source_id)
        except (TypeError, ValueError):
            return None
        if sid == SOURCE_MP:
            return 1
        if sid == SOURCE_MP2:
            return 2
        return None

    @property
    def stream1_rtmp_key(self) -> str:
        return self._stream1_rtmp_key

    @property
    def stream2_output_live(self) -> bool:
        return self._stream2_output_live

    @property
    def stream1_output_enabled(self) -> Optional[bool]:
        return self._stream1_output_enabled

    @property
    def stream2_output_enabled(self) -> Optional[bool]:
        return self._stream2_output_enabled

    @property
    def multisource_enabled(self) -> bool:
        return self._multisource_enabled

    @property
    def multisource_window2_src(self) -> Optional[int]:
        return self._multisource_window2_src

    def set_command_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._on_command = handler

    def connect(self, host: str, port: int = DEFAULT_PORT, timeout: float = 3.0) -> None:
        self.disconnect()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, int(port)))
        sock.settimeout(0.5)
        self._sock = sock
        self._connected = True
        self._stop.clear()
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        self._sync_bus_state()

    def disconnect(self) -> None:
        self._stop.set()
        self._connected = False
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=1.0)
        self._recv_thread = None
        self._recv_buffer.clear()
        self._partial = None

    def _send_command(self, cmd_id: str, cmd_type: str, value: Any = None) -> bool:
        if not self._sock or not self._connected:
            return False
        if cmd_type == "set" and cmd_id not in ALLOWED_SET_CMDS:
            return False
        if cmd_type == "get" and cmd_id not in ALLOWED_GET_CMDS:
            return False
        obj: dict[str, Any] = {"id": cmd_id, "type": cmd_type}
        if value is not None:
            obj["value"] = value if isinstance(value, list) else [value]
        payload = pack_packet(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
        try:
            import app_log

            app_log.gsp(f"{cmd_type} {cmd_id}", value)
        except ImportError:
            pass
        need_disconnect = False
        with self._lock:
            if not self._sock or not self._connected:
                return False
            try:
                self._sock.sendall(payload)
                return True
            except OSError:
                self._connected = False
                need_disconnect = True
        if need_disconnect:
            self.disconnect()
        return False

    def send_get(self, cmd_id: str, value: Optional[list] = None) -> bool:
        return self._send_command(cmd_id, "get", value)

    def send_set(self, cmd_id: str, value: list) -> bool:
        return self._send_command(cmd_id, "set", value)

    def set_preview_input(self, source_id: int) -> bool:
        return self.send_set(CMD_PVW_INDEX, [int(source_id)])

    def set_program_input(self, source_id: int) -> bool:
        return self.send_set(CMD_PGM_INDEX, [int(source_id)])

    def cut(self) -> bool:
        return self.send_set(CMD_CUT, [])

    def cut_to_input(self, source_id: int) -> bool:
        """Put source on preview bus, then CUT to program (standard switcher take)."""
        if not self.set_preview_input(source_id):
            return False
        time.sleep(0.08)
        return self.cut()

    def set_live(self, enable: bool) -> bool:
        return self.send_set(CMD_LIVE, [1 if enable else 0])

    def set_usk_on_air(self, key_id: int, enable: bool) -> bool:
        return self.send_set(CMD_KEY_ON_AIR, [int(key_id), 1 if enable else 0])

    def set_dsk_on_air(self, key_id: int, enable: bool) -> bool:
        return self.send_set(CMD_DSK_ON_AIR, [int(key_id), 1 if enable else 0])

    def set_transition_mix_rate(self, rate_seconds: float) -> bool:
        rate = max(0.5, min(8.0, float(rate_seconds)))
        return self.send_set(CMD_TRANSITION_MIX_RATE, [rate])

    def set_dsk_rate(self, key_id: int, rate_seconds: float) -> bool:
        rate = max(0.5, min(8.0, float(rate_seconds)))
        return self.send_set(CMD_DSK_RATE, [int(key_id), rate])

    def set_key_on_air_faded(
        self,
        key_id: int,
        enable: bool,
        *,
        dsk: bool = False,
        rate_seconds: float = 1.0,
    ) -> bool:
        """Take USK/DSK on or off using mix transition (fade) when possible."""
        kid = int(key_id)
        on = bool(enable)
        rate = max(0.5, min(8.0, float(rate_seconds)))
        if not self.set_transition_style_mix():
            if dsk:
                return self.set_dsk_on_air(kid, on)
            return self.set_usk_on_air(kid, on)
        time.sleep(0.04)
        self.set_transition_mix_rate(rate)
        if dsk:
            self.set_dsk_rate(kid, rate)
        time.sleep(0.04)
        if dsk:
            return self.set_dsk_on_air(kid, on)
        return self.set_usk_on_air(kid, on)

    def set_dsk_fill_source(self, key_id: int, source_id: int) -> bool:
        kid = int(key_id)
        sid = int(source_id)
        ok = self.send_set(CMD_DSK_FILL_SOURCE, [kid, sid])
        if ok and kid == DSK_1_KEY_ID:
            self._dsk1_fill_src = sid
        return ok

    def configure_dsk1_still1(
        self,
        key_id: int = DSK_1_KEY_ID,
        fill_source: int = DSK_FILL_STILL_1,
        still_index: int = 1,
    ) -> bool:
        """Downstream key 1 fill = Still 1; activate still 1 on media player 1."""
        if not self.activate_still(still_index):
            return False
        time.sleep(0.05)
        return self.set_dsk_fill_source(key_id, fill_source)

    def configure_usk_luma(
        self,
        key_id: int = USK_1_KEY_ID,
        fill_source: int = SOURCE_IN3,
        key_source: int = SOURCE_IN4,
    ) -> bool:
        """Set upstream key to Luma with fill/key sources (does not take key on air)."""
        kid = int(key_id)
        fill = int(fill_source)
        key = int(key_source)
        if not self.send_set(CMD_KEY_TYPE, [kid, "Luma"]):
            return False
        time.sleep(0.05)
        if not self.send_set(CMD_LUMA_FILL_SOURCE, [kid, fill]):
            return False
        time.sleep(0.05)
        ok = self.send_set(CMD_LUMA_KEY_SOURCE, [kid, key])
        if ok:
            self._usk1_key_type = "Luma"
            self._usk1_luma_fill = fill
            self._usk1_luma_key = key
        return ok

    def request_usk1_luma_state(self) -> None:
        """Poll upstream key 1 type and luma fill/key sources."""
        kid = USK_1_KEY_ID
        self.send_get(CMD_KEY_TYPE, [kid])
        self.send_get(CMD_LUMA_FILL_SOURCE, [kid])
        self.send_get(CMD_LUMA_KEY_SOURCE, [kid])

    def request_stream_ui_state(self) -> None:
        """Poll lyrics/title on-air, media player 1 still, and DSK fill."""
        self.send_get(CMD_KEY_ON_AIR, [USK_1_KEY_ID])
        self.send_get(CMD_DSK_ON_AIR, [DSK_1_KEY_ID])
        self.send_get(CMD_MEDIA_PLAYER, [MEDIA_PLAYER_1_ID])
        self.send_get(CMD_DSK_FILL_SOURCE, [DSK_1_KEY_ID])

    @staticmethod
    def _is_luma_key_type(key_type: Any) -> bool:
        if key_type is None:
            return False
        if isinstance(key_type, str):
            return key_type.strip().lower() == "luma"
        try:
            return int(key_type) == USK_KEY_TYPE_LUMA
        except (TypeError, ValueError):
            return False

    def usk1_luma_matches(self, fill_source: int, key_source: int) -> bool:
        """True when USK1 is Luma with the expected fill and key sources."""
        if not self._is_luma_key_type(self._usk1_key_type):
            return False
        try:
            return (
                self._usk1_luma_fill is not None
                and int(self._usk1_luma_fill) == int(fill_source)
                and self._usk1_luma_key is not None
                and int(self._usk1_luma_key) == int(key_source)
            )
        except (TypeError, ValueError):
            return False

    def set_stream_output_key(self, stream_id: int, key_text: str) -> bool:
        return self.send_set(CMD_LIVE_STREAM_OUTPUT_KEY, [int(stream_id), str(key_text)])

    def set_stream_output_enable(self, stream_id: int, enable: bool) -> bool:
        return self.send_set(CMD_LIVE_STREAM_OUTPUT_ENABLE, [int(stream_id), 1 if enable else 0])

    def activate_still(self, still_index: int, media_player_id: int = MEDIA_PLAYER_1_ID) -> bool:
        """Activate a still by 1-based index (1 = first still) on media player 1."""
        mp_id = int(media_player_id)
        idx = max(0, int(still_index) - 1)
        ok = self.send_set(CMD_MEDIA_PLAYER, [mp_id, "Still", idx])
        if ok and mp_id == MEDIA_PLAYER_1_ID:
            self._mp1_still_index_1based = int(still_index)
        return ok

    def set_multisource_enable(self, enable: bool) -> bool:
        ok = self.send_set(CMD_MULTI_SOURCE_ENABLE, [1 if enable else 0])
        if ok:
            self._multisource_enabled = bool(enable)
        return ok

    def set_multisource_fill_source(self, source_id: int) -> bool:
        sid = int(source_id)
        ok = self.send_set(CMD_MULTI_SOURCE_FILL_SOURCE, [sid])
        if ok:
            self._multisource_fill_src = sid
        return ok

    def set_multisource_window_source(self, window_id: int, source_id: int) -> bool:
        wid = int(window_id)
        sid = int(source_id)
        ok = self.send_set(CMD_MULTI_SOURCE_WINDOW_SOURCE, [wid, sid])
        if ok:
            if wid == MULTISOURCE_WINDOW_1_ID:
                self._multisource_window1_src = sid
            elif wid == MULTISOURCE_WINDOW_2_ID:
                self._multisource_window2_src = sid
        return ok

    def configure_multisource_layout(self, window2_source_id: int) -> bool:
        """Background Still 2, window 1 = HDMI 3, window 2 = live camera SDI."""
        if not self.set_multisource_fill_source(MULTISOURCE_BACKGROUND_FILL):
            return False
        time.sleep(0.03)
        if not self.set_multisource_window_source(MULTISOURCE_WINDOW_1_ID, SOURCE_IN3):
            return False
        time.sleep(0.03)
        return self.set_multisource_window_source(
            MULTISOURCE_WINDOW_2_ID, int(window2_source_id)
        )

    def set_transition_style_mix(self) -> bool:
        return self.send_set(CMD_TRANSITION_STYLE, ["Mix"])

    def auto_transition(self) -> bool:
        return self._send_command(CMD_AUTO_TRANSITION, "set")

    def fade_to_source(self, source_id: int, rate_seconds: float = 1.0) -> bool:
        """Mix transition: set rate, preview selected source, then AUTO to program."""
        if not self.set_transition_style_mix():
            return False
        self.set_transition_mix_rate(rate_seconds)
        time.sleep(0.04)
        if not self.set_preview_input(int(source_id)):
            return False
        time.sleep(0.04)
        return self.auto_transition()

    def splitview_on(self, window2_source_id: int, rate_seconds: float = 1.0) -> bool:
        if not self.configure_multisource_layout(window2_source_id):
            return False
        time.sleep(0.03)
        if not self.set_multisource_enable(True):
            return False
        time.sleep(0.03)
        return self.fade_to_source(SOURCE_MULTI, rate_seconds=rate_seconds)

    def splitview_off(self, target_source_id: int, rate_seconds: float = 1.0) -> bool:
        """Fade program back to camera/background input; leave multisource layout intact."""
        return self.fade_to_source(int(target_source_id), rate_seconds=rate_seconds)

    def _sync_bus_state(self) -> None:
        self.send_get(CMD_PGM_INDEX)
        self.send_get(CMD_PVW_INDEX)
        self.send_get(CMD_LIVE)
        self.send_get(CMD_LIVE_STREAM_OUTPUT_ENABLE, [STREAM_1_ID])
        self.send_get(CMD_LIVE_STREAM_OUTPUT_ENABLE, [STREAM_2_ID])
        self.send_get(CMD_LIVE_STREAM_OUTPUT_STATUS, [STREAM_1_ID])
        self.send_get(CMD_LIVE_STREAM_OUTPUT_STATUS, [STREAM_2_ID])
        self.request_stream_ui_state()
        self.request_usk1_luma_state()
        self.send_get(CMD_LIVE_STREAM_OUTPUT_KEY)
        self.send_get(CMD_MULTI_SOURCE_ENABLE)
        self.send_get(CMD_MULTI_SOURCE_WINDOW_SOURCE, [MULTISOURCE_WINDOW_1_ID])
        self.send_get(CMD_MULTI_SOURCE_WINDOW_SOURCE, [MULTISOURCE_WINDOW_2_ID])
        time.sleep(0.15)

    def _handle_command(self, cmd: dict[str, Any]) -> None:
        cmd_id = cmd.get("id")
        val = cmd.get("value")
        if not isinstance(val, list) or not val:
            return
        if cmd_id == CMD_PGM_INDEX:
            try:
                self._pgm_src = int(val[0])
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_PVW_INDEX:
            try:
                self._pvw_src = int(val[0])
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_LIVE:
            try:
                self._stream_live_on = int(val[0]) != 0
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_LIVE_STREAM_OUTPUT_ENABLE and len(val) >= 2:
            try:
                stream_id = int(val[0])
                enabled = int(val[1]) != 0
                if stream_id == STREAM_1_ID:
                    self._stream1_output_enabled = enabled
                elif stream_id == STREAM_2_ID:
                    self._stream2_output_enabled = enabled
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_LIVE_STREAM_OUTPUT_STATUS and len(val) >= 2:
            try:
                stream_id = int(val[0])
                # Companion LiveStatus: 0=Off, 1=On, 2=Abnormal
                if stream_id == STREAM_1_ID:
                    self._stream1_output_live = int(val[1]) == 1
                elif stream_id == STREAM_2_ID:
                    self._stream2_output_live = int(val[1]) == 1
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_KEY_ON_AIR and len(val) >= 2:
            try:
                key_id = int(val[0])
                on = int(val[1]) != 0
                if key_id == USK_1_KEY_ID:
                    self._usk1_on = on
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_KEY_TYPE and len(val) >= 2:
            try:
                key_id = int(val[0])
                if key_id == USK_1_KEY_ID:
                    self._usk1_key_type = val[1]
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_LUMA_FILL_SOURCE and len(val) >= 2:
            try:
                key_id = int(val[0])
                if key_id == USK_1_KEY_ID:
                    self._usk1_luma_fill = int(val[1])
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_LUMA_KEY_SOURCE and len(val) >= 2:
            try:
                key_id = int(val[0])
                if key_id == USK_1_KEY_ID:
                    self._usk1_luma_key = int(val[1])
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_DSK_ON_AIR and len(val) >= 2:
            try:
                key_id = int(val[0])
                on = int(val[1]) != 0
                if key_id == DSK_1_KEY_ID:
                    self._dsk1_on = on
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_DSK_FILL_SOURCE and len(val) >= 2:
            try:
                key_id = int(val[0])
                if key_id == DSK_1_KEY_ID:
                    self._dsk1_fill_src = int(val[1])
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_MEDIA_PLAYER and len(val) >= 3:
            try:
                mp_id = int(val[0])
                media_type = str(val[1]).strip().lower()
                if mp_id == MEDIA_PLAYER_1_ID and media_type == "still":
                    self._mp1_still_index_1based = int(val[2]) + 1
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_LIVE_STREAM_OUTPUT_KEY and len(val) >= 2:
            try:
                stream_id = int(val[0])
                if stream_id == STREAM_1_ID:
                    self._stream1_rtmp_key = str(val[1]) if val[1] is not None else ""
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_MULTI_SOURCE_ENABLE:
            try:
                self._multisource_enabled = int(val[0]) != 0
            except (TypeError, ValueError):
                pass
        elif cmd_id == CMD_MULTI_SOURCE_WINDOW_SOURCE and len(val) >= 2:
            try:
                win_id = int(val[0])
                src = int(val[1])
                if win_id == MULTISOURCE_WINDOW_1_ID:
                    self._multisource_window1_src = src
                elif win_id == MULTISOURCE_WINDOW_2_ID:
                    self._multisource_window2_src = src
            except (TypeError, ValueError):
                pass
        if self._on_command:
            try:
                self._on_command(cmd)
            except Exception:
                pass

    def _recv_loop(self) -> None:
        while not self._stop.is_set() and self._sock:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                self._connected = False
                self._sock = None
                break
            if not chunk:
                self._connected = False
                self._sock = None
                break
            self._recv_buffer.extend(chunk)
            for cmd in self._parse_buffer():
                self._handle_command(cmd)

    def _parse_buffer(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        data = bytes(self._recv_buffer)
        self._recv_buffer.clear()

        if self._partial:
            data = bytes(self._partial) + data
            self._partial = None

        index = data.find(PACKET_HEAD)
        if index < 0:
            if len(data) > 0:
                self._partial = bytearray(data)
            return out

        if index > 0:
            data = data[index:]

        offset = 0
        while offset + PACKET_HEADER_SIZE <= len(data):
            if data[offset : offset + 2] != PACKET_HEAD:
                offset += 1
                continue
            if offset + 5 > len(data):
                self._partial = bytearray(data[offset:])
                break
            pkt_size = struct.unpack_from("<H", data, offset + 3)[0]
            total = PACKET_HEADER_SIZE + pkt_size
            if offset + total > len(data):
                self._partial = bytearray(data[offset:])
                break
            try:
                out.append(_unpack_one_packet(data[offset : offset + total]))
            except (ValueError, json.JSONDecodeError):
                pass
            offset += total

        if offset < len(data) and self._partial is None:
            tail = data[offset:]
            if tail:
                self._partial = bytearray(tail)

        return out
