"""EasyScreenShare PC receiver.

Advertises itself on the local network (Bonjour/mDNS) so the EasyScreenShare
iPhone app can find it, then shows the phone's screen in a window and plays
its audio.

Keys:  F11 / double-click = fullscreen   M = mute   R = rotate 90°
       I = stats overlay                 Esc = leave fullscreen
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass

import av
import ifaddr

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402
import sounddevice as sd
from zeroconf import IPVersion, ServiceInfo, Zeroconf

log = logging.getLogger("easyscreenshare")

# --- Wire protocol (must match ios/Shared/StreamProtocol.swift) -------------
# Every message: [type: u8][payload length: u32 big-endian][payload]
SERVICE_TYPE = "_easyscreenshare._tcp.local."
DEFAULT_PORT = 50505
MSG_HELLO = 0x01  # payload: UTF-8 JSON {"name": ..., "version": 1}
MSG_VIDEO = 0x02  # payload: VIDEO_HEADER + H.264 Annex B access unit
MSG_AUDIO = 0x03  # payload: AUDIO_HEADER + int16 little-endian interleaved PCM
MSG_REQUEST_KEYFRAME = 0x10  # PC -> phone, empty payload
MSG_HEADER = struct.Struct(">BI")
VIDEO_HEADER = struct.Struct(">BBQ")  # orientation, flags (bit0 = keyframe), pts µs
AUDIO_HEADER = struct.Struct(">IBQ")  # sample rate, channels, pts µs
MAX_MESSAGE = 16 * 1024 * 1024

# RPVideoSampleOrientationKey (CGImagePropertyOrientation) -> degrees counter-
# clockwise to rotate the frame for display (pygame.transform.rotate convention).
ORIENTATION_TO_ROTATION = {1: 0, 2: 0, 3: 180, 4: 180, 5: -90, 6: 90, 7: 90, 8: -90}


VIRTUAL_ADAPTER_HINTS = ("vethernet", "hyper-v", "wsl", "virtualbox", "vmware", "loopback", "bluetooth")


def local_ipv4_addresses() -> list[str]:
    real, virtual = [], []
    for adapter in ifaddr.get_adapters():
        is_virtual = any(h in adapter.nice_name.lower() for h in VIRTUAL_ADAPTER_HINTS)
        for ip in adapter.ips:
            if ip.is_IPv4 and not ip.ip.startswith(("127.", "169.254.")):
                (virtual if is_virtual else real).append(ip.ip)
    return real or virtual


# --- Audio -------------------------------------------------------------------
class AudioPlayer:
    """Plays pushed PCM with a small jitter buffer and bounded latency."""

    PREBUFFER_MS = 60
    MAX_BUFFER_MS = 250

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._stream: sd.RawOutputStream | None = None
        self._format: tuple[int, int] | None = None
        self._frame_bytes = 4
        self._bytes_per_ms = 1.0
        self._primed = False
        self.muted = False

    @property
    def buffered_ms(self) -> float:
        with self._lock:
            return len(self._buf) / self._bytes_per_ms if self._format else 0.0

    def push(self, sample_rate: int, channels: int, pcm: bytes) -> None:
        if (sample_rate, channels) != self._format:
            self._open(sample_rate, channels)
        with self._lock:
            self._buf += pcm
            max_bytes = int(self.MAX_BUFFER_MS * self._bytes_per_ms)
            if len(self._buf) > max_bytes:
                # Fell behind (e.g. Wi-Fi hiccup): drop the oldest audio to keep latency low.
                excess = len(self._buf) - int(self.PREBUFFER_MS * self._bytes_per_ms)
                excess -= excess % self._frame_bytes
                del self._buf[:excess]

    def _open(self, sample_rate: int, channels: int) -> None:
        self.close()
        self._format = (sample_rate, channels)
        self._frame_bytes = 2 * channels
        self._bytes_per_ms = sample_rate * self._frame_bytes / 1000
        try:
            self._stream = sd.RawOutputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
                latency="low",
                callback=self._callback,
            )
            self._stream.start()
            log.info("Audio: %d Hz, %d channel(s) on %s", sample_rate, channels,
                     sd.query_devices(kind="output")["name"])
        except Exception:
            log.exception("Could not open audio output")
            self._stream = None

    def _callback(self, outdata, frames, time_info, status) -> None:
        need = frames * self._frame_bytes
        with self._lock:
            if not self._primed and len(self._buf) >= self.PREBUFFER_MS * self._bytes_per_ms:
                self._primed = True
            if self._primed:
                chunk = bytes(self._buf[:need])
                del self._buf[:need]
                if len(chunk) < need:  # underrun: rebuild the jitter buffer
                    self._primed = False
                    chunk += bytes(need - len(chunk))
            else:
                chunk = bytes(need)
        outdata[:] = bytes(need) if self.muted else chunk

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        self._format = None
        with self._lock:
            self._buf.clear()
            self._primed = False


# --- Network + decoding ------------------------------------------------------
@dataclass
class DecodedFrame:
    frame: av.VideoFrame
    rotation: int  # degrees counter-clockwise
    session: int


class Stats:
    def __init__(self) -> None:
        self.fps = 0.0
        self.kbps = 0.0
        self._frames = 0
        self._bytes = 0
        self._t0 = time.monotonic()

    def add(self, nbytes: int, frame: bool) -> None:
        self._bytes += nbytes
        self._frames += frame
        dt = time.monotonic() - self._t0
        if dt >= 1.0:
            self.fps = self._frames / dt
            self.kbps = self._bytes * 8 / 1000 / dt
            self._frames = self._bytes = 0
            self._t0 = time.monotonic()


class Receiver:
    """Accepts one phone at a time; a new connection replaces the old one."""

    def __init__(self, port: int, audio: AudioPlayer) -> None:
        self.audio = audio
        self.stats = Stats()
        self.device_name: str | None = None
        self._lock = threading.Lock()
        self._latest: DecodedFrame | None = None
        self._session = 0
        self._sock: socket.socket | None = None

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.server.bind(("0.0.0.0", port))
        except OSError:
            log.warning("Port %d busy, using a random port", port)
            self.server.bind(("0.0.0.0", 0))
        self.server.listen(2)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def take_frame(self) -> DecodedFrame | None:
        with self._lock:
            frame, self._latest = self._latest, None
            return frame

    def _accept_loop(self) -> None:
        while True:
            conn, addr = self.server.accept()
            with self._lock:
                old = self._sock
                self._session += 1
                session = self._session
                self._sock = conn
            if old is not None:
                old.close()
            log.info("Phone connected from %s", addr[0])
            threading.Thread(target=self._run_session, args=(conn, session), daemon=True).start()

    def _run_session(self, conn: socket.socket, session: int) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        decoder = av.CodecContext.create("h264", "r")
        decoder.flags |= int(av.codec.context.Flags.low_delay)
        decoder.thread_type = "SLICE"
        last_key_request = 0.0

        def request_keyframe() -> None:
            nonlocal last_key_request
            if time.monotonic() - last_key_request > 0.5:
                last_key_request = time.monotonic()
                try:
                    conn.sendall(MSG_HEADER.pack(MSG_REQUEST_KEYFRAME, 0))
                except OSError:
                    pass

        try:
            while True:
                msg_type, length = MSG_HEADER.unpack(recv_exact(conn, MSG_HEADER.size))
                if length > MAX_MESSAGE:
                    raise ConnectionError(f"message too large ({length} bytes)")
                payload = recv_exact(conn, length)

                if msg_type == MSG_VIDEO:
                    orientation, _flags, _pts = VIDEO_HEADER.unpack_from(payload)
                    self.stats.add(length, True)
                    try:
                        frames = decoder.decode(av.Packet(payload[VIDEO_HEADER.size:]))
                    except av.error.FFmpegError:
                        request_keyframe()
                        continue
                    if frames:
                        rotation = ORIENTATION_TO_ROTATION.get(orientation, 0)
                        with self._lock:
                            if session == self._session:
                                self._latest = DecodedFrame(frames[-1], rotation, session)
                elif msg_type == MSG_AUDIO:
                    sample_rate, channels, _pts = AUDIO_HEADER.unpack_from(payload)
                    self.stats.add(length, False)
                    if session == self._session and channels:
                        self.audio.push(sample_rate, channels, payload[AUDIO_HEADER.size:])
                elif msg_type == MSG_HELLO:
                    info = json.loads(payload.decode("utf-8"))
                    self.device_name = info.get("name", "iPhone")
                    log.info("Streaming from %s", self.device_name)
                    request_keyframe()
        except (OSError, ConnectionError, ValueError) as exc:
            log.info("Phone disconnected (%s)", exc)
        finally:
            conn.close()
            with self._lock:
                if session == self._session:
                    self._sock = None
                    self._latest = None
                    self.device_name = None
            if session == self._session:
                self.audio.close()


def recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = conn.recv_into(view[got:], n - got)
        if r == 0:
            raise ConnectionError("connection closed")
        got += r
    return bytes(buf)


# --- Discovery -----------------------------------------------------------------
def advertise(port: int) -> tuple[Zeroconf, ServiceInfo]:
    host = socket.gethostname()
    addrs = local_ipv4_addresses()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{host}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(a) for a in addrs],
        port=port,
        properties={"v": "1"},
        server=f"{host}.local.",
    )
    zc = Zeroconf(ip_version=IPVersion.V4Only)
    zc.register_service(info, allow_name_change=True)
    return zc, info


# --- Window --------------------------------------------------------------------
class Viewer:
    def __init__(self, receiver: Receiver, audio: AudioPlayer) -> None:
        self.receiver = receiver
        self.audio = audio
        pygame.init()
        desktop_w, desktop_h = pygame.display.get_desktop_sizes()[0]
        self.max_window = (int(desktop_w * 0.9), int(desktop_h * 0.9))
        h = int(desktop_h * 0.75)
        self.window = pygame.Window("EasyScreenShare", (h * 9 // 19, h), resizable=True)
        self.fullscreen = False
        self.show_stats = False
        self.user_rotation = 0
        self.frame: DecodedFrame | None = None
        self.last_aspect: float | None = None
        self.font = pygame.font.SysFont("segoeui", 20)
        self.small = pygame.font.SysFont("consolas", 15)
        self.dirty = True
        self._last_click = 0.0

    def run(self) -> None:
        clock = pygame.time.Clock()
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                self._handle_event(event)

            new = self.receiver.take_frame()
            if new is not None:
                self.frame = new
                self.dirty = True
            elif self.frame is not None and not self.receiver.connected:
                self.frame = None
                self.dirty = True

            if self.dirty or self.show_stats:
                self._draw()
                self.dirty = False
            clock.tick(120)

    def _handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_F11 or (event.key == pygame.K_RETURN and event.mod & pygame.KMOD_ALT):
                self._toggle_fullscreen()
            elif event.key == pygame.K_ESCAPE and self.fullscreen:
                self._toggle_fullscreen()
            elif event.key == pygame.K_m:
                self.audio.muted = not self.audio.muted
            elif event.key == pygame.K_r:
                self.user_rotation = (self.user_rotation + 90) % 360
            elif event.key == pygame.K_i:
                self.show_stats = not self.show_stats
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            now = time.monotonic()
            if now - self._last_click < 0.35:
                self._toggle_fullscreen()
            self._last_click = now
        self.dirty = True

    def _toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.window.set_fullscreen(desktop=True)
        else:
            self.window.set_windowed()

    def _fit_window(self, aspect: float) -> None:
        """Reshape the window when the phone rotates, keeping its longest side."""
        if self.fullscreen:
            return
        w, h = self.window.size
        long_side = max(w, h)
        new = (long_side, round(long_side / aspect)) if aspect >= 1 else (round(long_side * aspect), long_side)
        scale = min(1.0, self.max_window[0] / new[0], self.max_window[1] / new[1])
        self.window.size = (max(200, int(new[0] * scale)), max(200, int(new[1] * scale)))

    def _draw(self) -> None:
        screen = self.window.get_surface()
        screen.fill((0, 0, 0))
        f = self.frame
        if f is not None:
            rotation = (f.rotation + self.user_rotation) % 360
            quarter = rotation in (90, 270)
            src_w, src_h = f.frame.width, f.frame.height
            vid_w, vid_h = (src_h, src_w) if quarter else (src_w, src_h)
            aspect = vid_w / vid_h
            if self.last_aspect is None or abs(aspect - self.last_aspect) > 0.01:
                self.last_aspect = aspect
                self._fit_window(aspect)
                screen = self.window.get_surface()
                screen.fill((0, 0, 0))
            sw, sh = screen.get_size()
            scale = min(sw / vid_w, sh / vid_h)
            dw, dh = max(2, int(vid_w * scale)) & ~1, max(2, int(vid_h * scale)) & ~1
            # Scale in FFmpeg (fast, C) before rotating in pygame.
            tw, th = (dh, dw) if quarter else (dw, dh)
            plane = f.frame.reformat(width=tw, height=th, format="rgb24",
                                     interpolation="BILINEAR").planes[0]
            surf = pygame.image.frombuffer(plane, (tw, th), "RGB", pitch=plane.line_size)
            if rotation:
                surf = pygame.transform.rotate(surf, rotation)
            screen.blit(surf, ((sw - dw) // 2, (sh - dh) // 2))
        else:
            self.last_aspect = None
            self._draw_waiting(screen)

        if self.show_stats:
            s = self.receiver.stats
            lines = [
                f"{self.receiver.device_name or 'no device'}",
                f"{s.fps:5.1f} fps   {s.kbps / 1000:5.1f} Mbit/s",
                f"video {f.frame.width}x{f.frame.height}" if f else "video -",
                f"audio buffer {self.audio.buffered_ms:4.0f} ms" + ("  (muted)" if self.audio.muted else ""),
            ]
            for i, line in enumerate(lines):
                txt = self.small.render(line, True, (255, 255, 255), (0, 0, 0))
                screen.blit(txt, (8, 8 + i * 18))
        elif self.audio.muted:
            txt = self.small.render("muted (M)", True, (255, 255, 255), (0, 0, 0))
            screen.blit(txt, (8, 8))
        self.window.flip()

    def _draw_waiting(self, screen: pygame.Surface) -> None:
        sw, sh = screen.get_size()
        lines = [
            ("Waiting for your iPhone…", self.font, (240, 240, 240)),
            ("", self.small, (0, 0, 0)),
            ("On the phone: open EasyScreenShare", self.small, (170, 170, 170)),
            ("and tap “Start streaming”.", self.small, (170, 170, 170)),
            ("", self.small, (0, 0, 0)),
            (f"This PC: {socket.gethostname()}", self.small, (120, 120, 120)),
            (f"{', '.join(local_ipv4_addresses()) or 'no network'} : {self.receiver.port}",
             self.small, (120, 120, 120)),
        ]
        y = sh // 2 - len(lines) * 12
        for text, font, color in lines:
            if text:
                surf = font.render(text, True, color)
                screen.blit(surf, ((sw - surf.get_width()) // 2, y))
            y += 26


def main() -> None:
    parser = argparse.ArgumentParser(description="EasyScreenShare PC receiver")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    audio = AudioPlayer()
    receiver = Receiver(args.port, audio)
    zc, info = advertise(receiver.port)
    log.info("Listening on port %d as '%s' (%s)", receiver.port, info.name,
             ", ".join(local_ipv4_addresses()))
    try:
        Viewer(receiver, audio).run()
    finally:
        zc.unregister_service(info)
        zc.close()
        audio.close()
        pygame.quit()


if __name__ == "__main__":
    main()
