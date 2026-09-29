"""Pretends to be an iPhone mirroring over AirPlay, for testing airplay.py
without a phone: pairs, runs the FairPlay handshake with a known key (one of
airplay2-receiver's test vectors), then streams an encrypted H.264 test
pattern plus an AAC 440 Hz tone the way iOS does, rotating "the phone" every
few seconds."""

import argparse
import base64
import hashlib
import math
import os
import plistlib
import re
import socket
import struct
import threading
import time
from fractions import Fraction

import av
import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# FairPlay mode 0 test vector: the 164-byte key message, the encrypted AES key, and what it decrypts to.
FP_MODE = 0
FP_KEY_MESSAGE = bytes.fromhex(
    "46504c590301030000000098008f1a9ca548fdd57560a52926ff399f2eb154d0a7a0fffc997f58e27e00499eb9f310110d019e"
    "550e328047aea54308ab71b647041406878af96e06cf74127ae35941dceb58931b5543b39903f9f76a376248ee52e3656b561e"
    "1c1a0106ec6608df0ab4f2df528e65db6d622d3892d5b49c6c025606a574f19ebea7d93500bdd69db23333f22edcb3ccf7a6ac"
    "de7389f2facabfa61b0b50")
FP_EKEY = base64.b64decode(
    "RlBMWQECAQAAAAA8AAAAAG1EuhK5H0jgYesjD8U6v6IAAAAQihBgRl1RuAjfES0ItgRQH54+opzgkC88Q7gdUxnQV194UX4B")
FP_AES_KEY = bytes.fromhex("0496a612172f41e0fd71912acc33fc54")

STREAM_CONNECTION_ID = -1234567890123456789  # iOS sends a signed 64-bit value; exercise the unsigned conversion
START_CODE = re.compile(b"\x00\x00\x00\x01|\x00\x00\x01")


class Rtsp:
    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port))
        self.rfile = self.sock.makefile("rb")
        self.cseq = 0
        self.lock = threading.Lock()

    def request(self, method: str, uri: str, body: bytes = b"", content_type: str | None = None) -> bytes:
        with self.lock:
            self.cseq += 1
            head = [f"{method} {uri} RTSP/1.0", f"CSeq: {self.cseq}", f"Content-Length: {len(body)}",
                    "User-Agent: AirPlay/550.10"]
            if content_type:
                head.append(f"Content-Type: {content_type}")
            self.sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
            status = self.rfile.readline().decode().split()
            headers = {}
            while (line := self.rfile.readline().decode().strip()):
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            reply = self.rfile.read(int(headers.get("content-length", 0)))
        if status[1] != "200":
            raise SystemExit(f"{method} {uri} -> {' '.join(status[1:])}")
        return reply

    def plist(self, method: str, uri: str, obj: dict) -> dict:
        reply = self.request(method, uri, plistlib.dumps(obj, fmt=plistlib.FMT_BINARY),
                             "application/x-apple-binary-plist")
        return plistlib.loads(reply) if reply else {}


def verify_cipher(secret: bytes):
    key = hashlib.sha512(b"Pair-Verify-AES-Key" + secret).digest()[:16]
    iv = hashlib.sha512(b"Pair-Verify-AES-IV" + secret).digest()[:16]
    return Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()


def handshake(rtsp: Rtsp) -> bytes:
    """Pairs and runs FairPlay; returns the X25519 shared secret."""
    info = plistlib.loads(rtsp.request("GET", "/info"))
    print(f"receiver: {info['name']} ({info['model']}, features 0x{info['features']:X})")

    ed = Ed25519PrivateKey.generate()
    ed_pub = ed.public_key().public_bytes_raw()
    their_ed = rtsp.request("POST", "/pair-setup", ed_pub, "application/octet-stream")
    assert their_ed == info["pk"], "pair-setup key differs from /info pk"

    ecdh = X25519PrivateKey.generate()
    ecdh_pub = ecdh.public_key().public_bytes_raw()
    reply = rtsp.request("POST", "/pair-verify", b"\x01\x00\x00\x00" + ecdh_pub + ed_pub,
                         "application/octet-stream")
    their_ecdh, encrypted = reply[:32], reply[32:]
    secret = ecdh.exchange(X25519PublicKey.from_public_bytes(their_ecdh))
    cipher = verify_cipher(secret)
    Ed25519PublicKey.from_public_bytes(their_ed).verify(cipher.update(encrypted), their_ecdh + ecdh_pub)
    signature = cipher.update(ed.sign(ecdh_pub + their_ecdh))
    rtsp.request("POST", "/pair-verify", b"\x00\x00\x00\x00" + signature, "application/octet-stream")
    print("paired (receiver signature verified)")

    setup1 = b"FPLY" + bytes([3, 1, 1, 0, 0, 0, 0, 4, 2, 0, FP_MODE, 0xBB])
    assert len(rtsp.request("POST", "/fp-setup", setup1, "application/octet-stream")) == 142
    reply = rtsp.request("POST", "/fp-setup", FP_KEY_MESSAGE, "application/octet-stream")
    assert reply[12:] == FP_KEY_MESSAGE[-20:], "bad fp-setup reply"
    print("FairPlay handshake done")
    return secret


def to_avcc(nals: list[bytes]) -> bytes:
    return b"".join(len(n).to_bytes(4, "big") + n for n in nals)


def avc_config(sps: bytes, pps: bytes) -> bytes:
    return (bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]) + len(sps).to_bytes(2, "big") + sps
            + b"\x01" + len(pps).to_bytes(2, "big") + pps)


def video_loop(conn: socket.socket, aes_key: bytes, size: tuple[int, int], seconds: float,
               rotate_every: float) -> int:
    sid = str(STREAM_CONNECTION_ID % 2**64).encode()
    key = hashlib.sha512(b"AirPlayStreamKey" + sid + aes_key).digest()[:16]
    iv = hashlib.sha512(b"AirPlayStreamIV" + sid + aes_key).digest()[:16]
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()

    def send(payload_type: int, payload: bytes, dims: tuple[int, int] | None = None) -> None:
        header = bytearray(128)
        struct.pack_into("<IB", header, 0, len(payload), payload_type)
        if dims:
            struct.pack_into("<ff", header, 40, *dims)
            struct.pack_into("<ff", header, 56, *dims)
        conn.sendall(bytes(header) + payload)

    start = time.monotonic()
    frames = 0
    encoder = None
    orientation = -1
    config = b""
    while (elapsed := time.monotonic() - start) < seconds:
        if int(elapsed // rotate_every) != orientation:  # the phone "rotates": new resolution, new SPS/PPS
            orientation = int(elapsed // rotate_every)
            w, h = size if orientation % 2 == 0 else size[::-1]
            encoder = av.CodecContext.create("libx264", "w")
            encoder.width, encoder.height, encoder.pix_fmt = w, h, "yuv420p"
            encoder.time_base = Fraction(1, 60)
            encoder.options = {"tune": "zerolatency", "preset": "veryfast"}
            encoder.bit_rate = 4_000_000
            yy, xx = np.mgrid[0:h, 0:w]
            base = np.stack([xx * 255 // w, yy * 255 // h, np.full_like(xx, 90)], axis=-1).astype(np.uint8)
            base[: h // 12, :] = 255
            pts = 0
        img = base.copy()
        x = (frames * 7) % (w - 80)
        img[h // 2: h // 2 + 80, x: x + 80] = (255, 60, 60)
        frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        frame.pts = pts
        pts += 1
        for packet in encoder.encode(frame):
            nals = [n for n in START_CODE.split(bytes(packet)) if n]
            sps = next((n for n in nals if n[0] & 0x1F == 7), None)
            pps = next((n for n in nals if n[0] & 0x1F == 8), None)
            if sps and pps and avc_config(sps, pps) != config:
                config = avc_config(sps, pps)
                send(1, config, (w, h))
            slices = [n for n in nals if n[0] & 0x1F not in (7, 8)]
            send(0, encryptor.update(to_avcc(slices)))
        frames += 1
        time.sleep(max(0, start + frames / 60 - time.monotonic()))
    return frames


def audio_loop(target: tuple[str, int], aes_key: bytes, aes_iv: bytes, stop: threading.Event) -> int:
    encoder = av.CodecContext.create("aac", "w")
    encoder.sample_rate, encoder.layout, encoder.format = 44100, "stereo", "fltp"
    encoder.bit_rate = 128_000
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq, t, sent, start = 0, 0, 0, time.monotonic()
    while not stop.is_set():
        n = np.arange(t, t + 1024)
        tone = (np.sin(2 * math.pi * 440 * n / 44100) * 0.2).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
        frame.sample_rate, frame.pts = 44100, t
        t += 1024
        for packet in encoder.encode(frame):
            payload = bytes(packet)
            n_enc = len(payload) & ~15
            encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv)).encryptor()
            payload = encryptor.update(payload[:n_enc]) + payload[n_enc:]
            rtp = struct.pack(">BBHII", 0x80, 0x60, seq & 0xFFFF, t & 0xFFFFFFFF, 0x1234)
            sock.sendto(rtp + payload, target)
            seq += 1
            sent += 1
        time.sleep(max(0, start + t / 44100 - time.monotonic()))
    sock.close()
    return sent


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7000)
    p.add_argument("--seconds", type=float, default=12)
    p.add_argument("--rotate-every", type=float, default=5)
    p.add_argument("--size", default="590x1278")
    p.add_argument("--volume", type=float, default=-15.0, help="dB: -30..0, or -144 to mute")
    args = p.parse_args()
    size = tuple(map(int, args.size.split("x")))

    rtsp = Rtsp(args.host, args.port)
    secret = handshake(rtsp)

    timing = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    timing.bind(("0.0.0.0", 0))
    timing.settimeout(0.5)
    timing_requests = []

    def timing_listener() -> None:
        while not stop.is_set():
            try:
                timing_requests.append(timing.recv(64))
            except OSError:
                pass

    stop = threading.Event()
    threading.Thread(target=timing_listener, daemon=True).start()

    aes_iv = os.urandom(16)
    base = f"rtsp://{args.host}/{STREAM_CONNECTION_ID & 0xFFFFFFFF}"
    reply = rtsp.plist("SETUP", base, {"ekey": FP_EKEY, "eiv": aes_iv, "et": 32, "name": "Fake iPhone",
                                       "timingProtocol": "NTP", "timingPort": timing.getsockname()[1],
                                       "isScreenMirroringSession": True, "model": "iPhone15,2"})
    print(f"session: timingPort {reply['timingPort']}, eventPort {reply['eventPort']}")
    aes_key = hashlib.sha512(FP_AES_KEY + secret).digest()[:16]

    video = rtsp.plist("SETUP", base, {"streams": [{"type": 110, "streamConnectionID": STREAM_CONNECTION_ID}]})
    audio = rtsp.plist("SETUP", base, {"streams": [{"type": 96, "ct": 4, "spf": 1024, "sr": 44100,
                                                    "controlPort": 0, "audioFormat": 0x400000}]})
    rtsp.request("RECORD", base)
    rtsp.request("SET_PARAMETER", base, f"volume: {args.volume:.6f}\r\n".encode(), "text/parameters")

    def feedback() -> None:
        while not stop.wait(2):
            rtsp.request("POST", "/feedback")

    threading.Thread(target=feedback, daemon=True).start()
    audio_packets = []
    audio_thread = threading.Thread(target=lambda: audio_packets.append(audio_loop(
        (args.host, audio["streams"][0]["dataPort"]), aes_key, aes_iv, stop)), daemon=True)
    audio_thread.start()

    conn = socket.create_connection((args.host, video["streams"][0]["dataPort"]))
    frames = video_loop(conn, aes_key, size, args.seconds, args.rotate_every)
    stop.set()
    audio_thread.join()
    rtsp.plist("TEARDOWN", base, {"streams": [{"type": 110}]})
    rtsp.plist("TEARDOWN", base, {"streams": [{"type": 96}]})
    conn.close()
    rtsp.sock.close()
    print(f"sent {frames} video frames, {audio_packets[0]} audio packets; "
          f"receiver sent {len(timing_requests)} timing requests")


if __name__ == "__main__":
    main()
