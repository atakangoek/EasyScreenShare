"""Pretends to be the iPhone broadcast extension, for testing the PC receiver
without a phone: finds the receiver via Bonjour, then streams an H.264 test
pattern plus a 440 Hz tone, rotating "the phone" every few seconds."""

import argparse
import json
import math
import socket
import struct
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from zeroconf import ServiceBrowser, Zeroconf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from easyscreenshare import (AUDIO_HEADER, MSG_AUDIO, MSG_HEADER, MSG_HELLO,  # noqa: E402
                             MSG_REQUEST_KEYFRAME, MSG_VIDEO, SERVICE_TYPE, VIDEO_HEADER)


def discover(timeout: float) -> tuple[str, int]:
    found = threading.Event()
    result = {}

    class Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if info and info.parsed_addresses():
                result["addr"] = (info.parsed_addresses()[0], info.port)
                print(f"found {name} at {result['addr']}")
                found.set()

        def update_service(self, *a): pass
        def remove_service(self, *a): pass

    zc = Zeroconf()
    ServiceBrowser(zc, SERVICE_TYPE, Listener())
    ok = found.wait(timeout)
    zc.close()
    if not ok:
        raise SystemExit("no receiver found via Bonjour")
    return result["addr"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=float, default=20)
    p.add_argument("--rotate-every", type=float, default=5)
    p.add_argument("--size", default="590x1278")
    p.add_argument("--host", help="connect to this IP directly instead of using Bonjour")
    p.add_argument("--port", type=int, default=50505)
    args = p.parse_args()
    w, h = map(int, args.size.split("x"))

    host, port = (args.host, args.port) if args.host else discover(10)
    sock = socket.create_connection((host, port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    lock = threading.Lock()
    force_key = threading.Event()
    stop = threading.Event()

    def send(msg_type: int, payload: bytes) -> None:
        with lock:
            sock.sendall(MSG_HEADER.pack(msg_type, len(payload)) + payload)

    def reader() -> None:
        try:
            while True:
                t, n = MSG_HEADER.unpack(sock.recv(5))
                if n:
                    sock.recv(n)
                if t == MSG_REQUEST_KEYFRAME:
                    print("receiver requested keyframe")
                    force_key.set()
        except Exception:
            stop.set()

    threading.Thread(target=reader, daemon=True).start()
    send(MSG_HELLO, json.dumps({"name": "Fake iPhone", "version": 1}).encode())

    def audio_loop() -> None:
        rate, chunk, t = 44100, 1024, 0
        start = time.monotonic()
        sent = 0
        while not stop.is_set():
            n = np.arange(t, t + chunk)
            tone = (np.sin(2 * math.pi * 440 * n / rate) * 6000).astype("<i2")
            stereo = np.repeat(tone[:, None], 2, axis=1).tobytes()
            send(MSG_AUDIO, AUDIO_HEADER.pack(rate, 2, int(t * 1e6 / rate)) + stereo)
            t += chunk
            sent += chunk
            time.sleep(max(0, start + sent / rate - time.monotonic()))

    threading.Thread(target=audio_loop, daemon=True).start()

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.time_base = Fraction(1, 60)
    enc.framerate = Fraction(60)
    enc.gop_size = 120
    enc.options = {"tune": "zerolatency", "preset": "veryfast", "profile": "high"}
    enc.bit_rate = 6_000_000

    orientations = [1, 8, 3, 6]  # up, left, down, right
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.stack([xx * 255 // w, yy * 255 // h, np.full_like(xx, 90)], axis=-1).astype(np.uint8)
    start = time.monotonic()
    i = 0
    while not stop.is_set() and time.monotonic() - start < args.seconds:
        img = base.copy()
        # White "top" bar + moving square so orientation and motion are visible.
        img[: h // 12, :] = 255
        x = int((i * 7) % (w - 80))
        img[h // 2 : h // 2 + 80, x : x + 80] = (255, 60, 60)
        frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        if force_key.is_set():
            force_key.clear()
            frame.pict_type = av.video.frame.PictureType.I
        orient = orientations[int((time.monotonic() - start) // args.rotate_every) % 4]
        for pkt in enc.encode(frame):
            key = 1 if pkt.is_keyframe else 0
            send(MSG_VIDEO, VIDEO_HEADER.pack(orient, key, int(i * 1e6 / 60)) + bytes(pkt))
        i += 1
        time.sleep(max(0, start + i / 60 - time.monotonic()))
    print(f"sent {i} frames")
    stop.set()
    sock.close()


if __name__ == "__main__":
    main()
