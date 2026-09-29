"""AirPlay screen mirroring for the EasyScreenShare PC receiver.

Makes the PC show up in an iPhone/iPad's Control Center › Screen Mirroring
list, like an Apple TV, so it can mirror without the EasyScreenShare app.
This is the legacy (non-HomeKit) mirroring protocol:

  RTSP control connection (port 7000)
    GET  /info           what we can do (binary plist)
    POST /pair-setup     swap Ed25519 public keys
    POST /pair-verify    X25519 key agreement, signed both ways
    POST /fp-setup       FairPlay handshake (vendor/ap2)
    SETUP                FairPlay-encrypted AES key, then the video (110) and audio (96) streams
    SET_PARAMETER        volume
    POST /feedback       keep-alive every 2 s
    TEARDOWN
  video: TCP, 128-byte header + AES-CTR encrypted H.264 (AVCC)
  audio: RTP over UDP, AES-CBC encrypted AAC-ELD (AAC-LC / ALAC for audio-only)

The FairPlay parts come from openairplay/airplay2-receiver (see
vendor/README.md); the protocol follows that project, UxPlay and RPiPlay.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import plistlib
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import av
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from zeroconf import ServiceInfo, Zeroconf

from vendor.ap2.fairplay3 import Fairplay3
from vendor.ap2.playfair import fairplay_setup

if TYPE_CHECKING:
    from easyscreenshare import Receiver

log = logging.getLogger("easyscreenshare.airplay")

AIRPLAY_PORT = 7000
MODEL = "AppleTV3,2"
SOURCE_VERSION = "220.68"
# Screen mirroring, audio, FairPlay and legacy pairing; no HomeKit pairing (bit 48) or HEVC.
FEATURES = 0x5A7FFEE6
STATUS_FLAGS = 0x44
DISPLAY_SIZE = (1920, 1080)  # the phone scales its screen to fit inside this
MAX_FPS = 60
STREAM_AUDIO = 96
STREAM_MIRROR = 110
SESSION_TIMEOUT = 20.0  # the phone sends /feedback every 2 s while streaming
MAX_BODY = 1024 * 1024
IDENTITY_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "EasyScreenShare" / "airplay.json"

PLIST = "application/x-apple-binary-plist"
OCTET_STREAM = "application/octet-stream"
TEXT_PARAMETERS = "text/parameters"


# --- Identity ------------------------------------------------------------------
@dataclass
class Identity:
    """Our long-term pairing key and IDs, kept across restarts so the phone sees the same device."""

    key: Ed25519PrivateKey
    device_id: str  # MAC-style, e.g. "AA:BB:CC:DD:EE:FF"
    pi: str

    @property
    def public_key(self) -> bytes:
        return self.key.public_key().public_bytes_raw()

    @classmethod
    def load(cls, path: Path) -> Identity:
        try:
            data = json.loads(path.read_text())
            return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["key"])),
                       data["device_id"], data["pi"])
        except (OSError, ValueError, KeyError):
            pass
        identity = cls(Ed25519PrivateKey.generate(),
                       ":".join(f"{b:02X}" for b in uuid.getnode().to_bytes(6, "big")),
                       str(uuid.uuid4()))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"key": identity.key.private_bytes_raw().hex(),
                                        "device_id": identity.device_id, "pi": identity.pi}))
        except OSError:
            log.warning("Could not save the AirPlay identity to %s", path)
        return identity


# --- Server --------------------------------------------------------------------
class AirPlayServer:
    """Accepts AirPlay control connections and advertises the receiver over Bonjour."""

    def __init__(self, receiver: Receiver, name: str, port: int = AIRPLAY_PORT) -> None:
        self.receiver = receiver
        self.name = name
        self.identity = Identity.load(IDENTITY_PATH)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.server.bind(("0.0.0.0", port))
        except OSError:
            log.warning("AirPlay port %d busy, using a random port", port)
            self.server.bind(("0.0.0.0", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def advertise(self, zc: Zeroconf, addresses: list[str]) -> None:
        """Registers the Bonjour services in the background (name probing takes a few seconds)."""
        threading.Thread(target=self._register, args=(zc, addresses), daemon=True).start()

    def _register(self, zc: Zeroconf, addresses: list[str]) -> None:
        host = socket.gethostname()
        packed = [socket.inet_aton(a) for a in addresses]
        mac = self.identity.device_id.replace(":", "")
        for service_type, instance, txt in (
            ("_airplay._tcp.local.", self.name, self.airplay_txt()),
            ("_raop._tcp.local.", f"{mac}@{self.name}", self.raop_txt()),
        ):
            zc.register_service(ServiceInfo(service_type, f"{instance}.{service_type}", addresses=packed,
                                            port=self.port, properties=txt, server=f"{host}.local."),
                                allow_name_change=True)
        log.info("AirPlay: '%s' is in the Screen Mirroring list (port %d)", self.name, self.port)

    def airplay_txt(self) -> dict[str, str]:
        return {
            "deviceid": self.identity.device_id,
            "features": f"0x{FEATURES:X},0x0",
            "flags": "0x4",
            "model": MODEL,
            "pk": self.identity.public_key.hex(),
            "pi": self.identity.pi,
            "srcvers": SOURCE_VERSION,
            "vv": "2",
        }

    def raop_txt(self) -> dict[str, str]:
        return {
            "ch": "2", "cn": "0,1,2,3", "da": "true", "et": "0,3,5", "vv": "2",
            "ft": f"0x{FEATURES:X},0x0", "am": MODEL, "md": "0,1,2", "rhd": "5.6.0.0",
            "pw": "false", "sf": "0x4", "sr": "44100", "ss": "16", "sv": "false", "tp": "UDP",
            "txtvers": "1", "vs": SOURCE_VERSION, "vn": "65537", "pk": self.identity.public_key.hex(),
        }

    def info(self) -> dict:
        width, height = DISPLAY_SIZE
        audio_formats = [{"type": t, "audioInputFormats": 0x3FFFFFC, "audioOutputFormats": 0x3FFFFFC}
                         for t in (100, 101)]
        audio_latencies = [{"type": t, "audioType": "default", "inputLatencyMicros": 0,
                            "outputLatencyMicros": 0} for t in (100, 101)]
        return {
            "deviceID": self.identity.device_id,
            "macAddress": self.identity.device_id,
            "features": FEATURES,
            "model": MODEL,
            "name": self.name,
            "pi": self.identity.pi,
            "pk": self.identity.public_key,
            "sourceVersion": SOURCE_VERSION,
            "statusFlags": STATUS_FLAGS,
            "vv": 2,
            "keepAliveLowPower": True,
            "keepAliveSendStatsAsBody": True,
            "txtAirPlay": txt_record(self.airplay_txt()),
            "txtRAOP": txt_record(self.raop_txt()),
            "audioFormats": audio_formats,
            "audioLatencies": audio_latencies,
            "displays": [{
                "uuid": str(uuid.uuid5(uuid.NAMESPACE_OID, self.identity.pi)),
                "width": width, "height": height, "widthPixels": width, "heightPixels": height,
                "widthPhysical": 0, "heightPhysical": 0, "rotation": False, "refreshRate": 60,
                "maxFPS": MAX_FPS, "overscanned": False, "features": 14,
            }],
        }

    def _accept_loop(self) -> None:
        while True:
            conn, addr = self.server.accept()
            threading.Thread(target=Connection(self, conn, addr[0]).run, daemon=True).start()


def txt_record(txt: dict[str, str]) -> bytes:
    """Raw DNS TXT record bytes, as /info reports them."""
    entries = (f"{k}={v}".encode() for k, v in txt.items())
    return b"".join(bytes([len(e)]) + e for e in entries)


# --- RTSP control connection ---------------------------------------------------
@dataclass
class Request:
    method: str
    path: str
    protocol: str
    headers: dict[str, str]  # lower-case names
    body: bytes

    def plist(self) -> dict:
        return plistlib.loads(self.body) if self.body else {}


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def plist_reply(obj: dict) -> Reply:
    return Reply(body=plistlib.dumps(obj, fmt=plistlib.FMT_BINARY), content_type=PLIST)


REASONS = {200: "OK", 400: "Bad Request", 470: "Connection Authorization Required",
           500: "Internal Server Error"}


class Connection:
    """One control connection from an iPhone/iPad; carries at most one mirroring session."""

    def __init__(self, server: AirPlayServer, sock: socket.socket, address: str) -> None:
        self.server = server
        self.receiver = server.receiver
        self.sock = sock
        self.address = address
        self.device_name = "iPhone"
        self.session: int | None = None
        self.streams: dict[int, MirrorStream | AudioStream] = {}
        self.timing: TimingClient | None = None
        self.volume_db = 0.0
        # Pairing and FairPlay state, in the order the phone sets it up.
        self._their_ed_key = b""
        self._their_ecdh_key = b""
        self._our_ecdh_key = b""
        self.ecdh_secret: bytes | None = None
        self.fp_key_message: bytes | None = None
        self.aes_key: bytes | None = None
        self.aes_iv: bytes | None = None

    def run(self) -> None:
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = SocketReader(self.sock)
        try:
            while True:
                self.sock.settimeout(SESSION_TIMEOUT if self.streams else None)
                request = read_request(reader)
                self._send(request, self._dispatch(request))
        except (OSError, ValueError) as exc:
            log.debug("AirPlay connection from %s ended (%s)", self.address, exc)
        finally:
            for stream_type in list(self.streams):
                self._close_stream(stream_type)
            self._end_session()
            if self.timing is not None:
                self.timing.close()
            self.sock.close()

    def close(self) -> None:
        """Ends the connection from another thread (e.g. another phone took over the window)."""
        close_socket(self.sock)

    def _send(self, request: Request, reply: Reply) -> None:
        lines = [f"{request.protocol} {reply.status} {REASONS.get(reply.status, 'Error')}",
                 f"Server: AirTunes/{SOURCE_VERSION}",
                 f"Content-Length: {len(reply.body)}"]
        if "cseq" in request.headers:
            lines.append(f"CSeq: {request.headers['cseq']}")
        if reply.content_type:
            lines.append(f"Content-Type: {reply.content_type}")
        lines += [f"{k}: {v}" for k, v in reply.headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + reply.body)

    def _dispatch(self, request: Request) -> Reply:
        if request.method in ("GET", "POST"):
            handler = self.PATHS.get(request.path)
        else:
            handler = self.METHODS.get(request.method)
        if handler is None:  # /feedback, /command, /audioMode, FLUSH, ...
            log.debug("AirPlay: %s %s", request.method, request.path)
            return Reply()
        try:
            return handler(self, request)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("AirPlay: %s %s failed: %s", request.method, request.path, exc)
            return Reply(status=400)

    # --- Handshake ---
    def _info(self, request: Request) -> Reply:
        return plist_reply(self.server.info())

    def _pair_setup(self, request: Request) -> Reply:
        if len(request.body) != 32:
            log.warning("AirPlay: %s asked for HomeKit pairing, which isn't supported", self.address)
            return Reply(status=470)
        return Reply(body=self.server.identity.public_key, content_type=OCTET_STREAM)

    def _pair_verify(self, request: Request) -> Reply:
        body = request.body
        if len(body) != 68:
            raise ValueError(f"pair-verify message of {len(body)} bytes")
        if body[0] == 1:  # [1,0,0,0] + their X25519 key + their Ed25519 key
            self._their_ecdh_key, self._their_ed_key = body[4:36], body[36:68]
            ours = X25519PrivateKey.generate()
            self._our_ecdh_key = ours.public_key().public_bytes_raw()
            self.ecdh_secret = ours.exchange(X25519PublicKey.from_public_bytes(self._their_ecdh_key))
            signature = self.server.identity.key.sign(self._our_ecdh_key + self._their_ecdh_key)
            return Reply(body=self._our_ecdh_key + self._verify_cipher().update(signature),
                         content_type=OCTET_STREAM)
        # [0,0,0,0] + their signature, encrypted with the keystream after ours.
        if self.ecdh_secret is None:
            raise ValueError("pair-verify finished before it started")
        cipher = self._verify_cipher()
        cipher.update(bytes(64))
        signature = cipher.update(body[4:])
        try:
            Ed25519PublicKey.from_public_bytes(self._their_ed_key).verify(
                signature, self._their_ecdh_key + self._our_ecdh_key)
        except InvalidSignature:
            log.warning("AirPlay: pairing with %s failed", self.address)
            self.ecdh_secret = None
            return Reply(status=470)
        return Reply()

    def _verify_cipher(self):
        key = hashlib.sha512(b"Pair-Verify-AES-Key" + self.ecdh_secret).digest()[:16]
        iv = hashlib.sha512(b"Pair-Verify-AES-IV" + self.ecdh_secret).digest()[:16]
        return Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()

    def _fp_setup(self, request: Request) -> Reply:
        response = fairplay_setup(request.body)
        if response is None:
            raise ValueError("unsupported FairPlay request")
        if len(request.body) == 164:
            self.fp_key_message = request.body  # needed to decrypt the AES key in SETUP
        return Reply(body=response, content_type=OCTET_STREAM)

    # --- Streams ---
    def _setup(self, request: Request) -> Reply:
        body = request.plist()
        reply = {}
        if "ekey" in body:
            self._setup_session(body)
            reply.update(timingPort=self.timing.port, eventPort=self.server.port)
        if "streams" in body:
            reply["streams"] = [self._setup_stream(s) for s in body["streams"]]
        return plist_reply(reply)

    def _setup_session(self, body: dict) -> None:
        if self.ecdh_secret is None or self.fp_key_message is None:
            raise ValueError("SETUP before pairing")
        ekey = body["ekey"]
        if len(ekey) != 72:
            raise ValueError(f"FairPlay key of {len(ekey)} bytes")
        key = Fairplay3().decryptAESKey(self.fp_key_message, ekey)
        self.aes_key = hashlib.sha512(key + self.ecdh_secret).digest()[:16]
        self.aes_iv = body["eiv"]
        self.device_name = body.get("name") or self.device_name
        if self.timing is not None:
            self.timing.close()
        self.timing = TimingClient(self.address, body.get("timingPort"))

    def _setup_stream(self, stream: dict) -> dict:
        stream_type = stream.get("type")
        if self.aes_key is None:
            raise ValueError("stream SETUP before the session SETUP")
        if stream_type not in (STREAM_MIRROR, STREAM_AUDIO):
            log.warning("AirPlay: unsupported stream type %s", stream_type)
            return {"type": stream_type}
        self._close_stream(stream_type)
        if stream_type == STREAM_MIRROR:
            new = MirrorStream(self.receiver, self._begin_session(), self.aes_key,
                               stream["streamConnectionID"], on_lost=self.close)
            reply = {"type": STREAM_MIRROR, "dataPort": new.port}
        else:
            decoder = audio_decoder(stream.get("ct", 8), stream.get("spf", 480))
            new = AudioStream(self.receiver, self._begin_session(), self.aes_key, self.aes_iv, decoder)
            reply = {"type": STREAM_AUDIO, "dataPort": new.data_port, "controlPort": new.control_port}
        self.streams[stream_type] = new
        return reply

    def _teardown(self, request: Request) -> Reply:
        types = [s.get("type") for s in request.plist().get("streams", [])]
        for stream_type in types or list(self.streams):
            self._close_stream(stream_type)
        if not self.streams:
            self._end_session()
        return Reply()

    def _close_stream(self, stream_type: int) -> None:
        stream = self.streams.pop(stream_type, None)
        if stream is not None:
            stream.close()

    def _begin_session(self) -> int:
        if self.session is None or not self.receiver.is_current(self.session):
            self.session = self.receiver.begin_session(self.device_name, self.close)
            self._apply_volume()
            log.info("AirPlay: mirroring from %s (%s)", self.device_name, self.address)
        return self.session

    def _end_session(self) -> None:
        if self.session is not None:
            self.receiver.end_session(self.session)
            self.session = None
            log.info("AirPlay: %s stopped mirroring", self.device_name)

    # --- Parameters ---
    def _record(self, request: Request) -> Reply:
        return Reply(headers={"Audio-Latency": "11025", "Audio-Jack-Status": "connected; type=analog"})

    def _options(self, request: Request) -> Reply:
        return Reply(headers={"Public": "SETUP, RECORD, PAUSE, FLUSH, TEARDOWN, OPTIONS, "
                                        "GET_PARAMETER, SET_PARAMETER, POST, GET"})

    def _get_parameter(self, request: Request) -> Reply:
        if b"volume" in request.body:
            return Reply(body=f"volume: {self.volume_db:.6f}\r\n".encode(), content_type=TEXT_PARAMETERS)
        return Reply()

    def _set_parameter(self, request: Request) -> Reply:
        if request.headers.get("content-type") == TEXT_PARAMETERS:
            for line in request.body.decode("utf-8", "replace").splitlines():
                name, _, value = line.partition(":")
                if name.strip() == "volume":
                    self.volume_db = float(value)
                    self._apply_volume()
        return Reply()

    def _apply_volume(self) -> None:
        # -144 dB = muted; otherwise the phone's slider maps to -30..0 dB.
        if self.session is not None and self.receiver.is_current(self.session):
            self.receiver.audio.gain = 0.0 if self.volume_db <= -30 else min(1.0, 10 ** (self.volume_db / 20))

    PATHS: dict[str, Callable[[Connection, Request], Reply]] = {
        "/info": _info,
        "/pair-setup": _pair_setup,
        "/pair-verify": _pair_verify,
        "/fp-setup": _fp_setup,
    }
    METHODS: dict[str, Callable[[Connection, Request], Reply]] = {
        "SETUP": _setup,
        "TEARDOWN": _teardown,
        "RECORD": _record,
        "OPTIONS": _options,
        "GET_PARAMETER": _get_parameter,
        "SET_PARAMETER": _set_parameter,
    }


def read_request(reader: SocketReader) -> Request:
    while not (line := reader.readline().strip()):
        pass  # tolerate blank lines between requests
    method, uri, protocol = line.decode("latin-1").split()
    headers = {}
    while (line := reader.readline().decode("latin-1").strip()):
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", 0))
    if length > MAX_BODY:
        raise ValueError(f"request body too large ({length} bytes)")
    return Request(method, uri.split("?")[0], protocol, headers, reader.read(length))


class SocketReader:
    """Buffered reads straight from a socket.

    Unlike socket.makefile(), this lets another thread end a blocked read by
    closing the socket: makefile() keeps the socket open, and on Windows
    shutdown() doesn't wake a pending recv."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = bytearray()

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def readline(self, limit: int = 8192) -> bytes:
        while (end := self._buf.find(b"\n")) < 0:
            if len(self._buf) > limit:
                raise ValueError("line too long")
            self._fill()
        return self.read(end + 1)

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("connection closed")
        self._buf += chunk


def close_socket(sock: socket.socket | None) -> None:
    """Closes a socket so that a read blocked on it in another thread returns."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


# --- Video ---------------------------------------------------------------------
class MirrorStream:
    """The screen: a TCP stream of 128-byte headers + AES-CTR encrypted H.264."""

    HEADER_SIZE = 128
    MAX_PAYLOAD = 16 * 1024 * 1024
    VIDEO, CODEC_CONFIG = 0, 1  # payload types; others (heartbeats, reports) are skipped

    def __init__(self, receiver: Receiver, session: int, aes_key: bytes, stream_id: int,
                 on_lost: Callable[[], None]) -> None:
        self.receiver = receiver
        self.session = session
        self.on_lost = on_lost
        sid = str(stream_id % 2**64).encode()  # the plist holds it as a signed 64-bit int
        key = hashlib.sha512(b"AirPlayStreamKey" + sid + aes_key).digest()[:16]
        iv = hashlib.sha512(b"AirPlayStreamIV" + sid + aes_key).digest()[:16]
        self._decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
        self._closed = False
        self._conn: socket.socket | None = None
        self._listener = socket.create_server(("0.0.0.0", 0))
        self._listener.settimeout(SESSION_TIMEOUT)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def close(self) -> None:
        self._closed = True
        close_socket(self._listener)
        close_socket(self._conn)

    def _run(self) -> None:
        try:
            self._conn, _ = self._listener.accept()
            self._listener.close()
            self._conn.settimeout(SESSION_TIMEOUT)
            self._receive(SocketReader(self._conn))
        except (OSError, ConnectionError) as exc:
            if not self._closed:
                log.info("AirPlay: video stream lost (%s)", exc)
                self.on_lost()
        finally:
            self.close()

    def _receive(self, reader: SocketReader) -> None:
        decoder = av.CodecContext.create("h264", "r")
        decoder.flags |= int(av.codec.context.Flags.low_delay)
        decoder.thread_type = "SLICE"
        parameter_sets = b""
        while True:
            header = reader.read(self.HEADER_SIZE)
            size, payload_type = struct.unpack_from("<IB", header)
            if size > self.MAX_PAYLOAD:
                raise ConnectionError(f"video packet too large ({size} bytes)")
            payload = reader.read(size)
            if payload_type == self.VIDEO:
                # Decrypt every video payload, even ones we drop: the CTR stream spans all of them.
                data = parameter_sets + avcc_to_annexb(self._decryptor.update(payload))
                parameter_sets = b""
                self.receiver.stats.add(size, True)
                try:
                    frames = decoder.decode(av.Packet(data))
                except av.error.FFmpegError:
                    continue
                if frames:
                    self.receiver.publish_frame(self.session, frames[-1])
            elif payload_type == self.CODEC_CONFIG:  # new SPS/PPS: stream start or the phone rotated
                parameter_sets = avc_config_to_annexb(payload)
                width, height = struct.unpack_from("<ff", header, 56)
                log.info("AirPlay: video %dx%d", width, height)


START_CODE = b"\x00\x00\x00\x01"


def avcc_to_annexb(data: bytes) -> bytes:
    """[u32 length][NAL]... -> [start code][NAL]..."""
    out = bytearray()
    pos = 0
    while pos + 4 <= len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        pos += 4
        if n == 0 or pos + n > len(data):
            break
        out += START_CODE + data[pos:pos + n]
        pos += n
    return bytes(out)


def avc_config_to_annexb(config: bytes) -> bytes:
    """AVCDecoderConfigurationRecord (avcC) -> Annex B SPS + PPS."""
    out = bytearray()
    try:
        pos = 5
        for count_mask in (0x1F, 0xFF):  # SPS count (low 5 bits), then PPS count
            count = config[pos] & count_mask
            pos += 1
            for _ in range(count):
                n = int.from_bytes(config[pos:pos + 2], "big")
                out += START_CODE + config[pos + 2:pos + 2 + n]
                pos += 2 + n
    except IndexError:
        log.warning("AirPlay: malformed video codec config")
    return bytes(out)


# --- Audio ---------------------------------------------------------------------
AAC_ELD_CONFIG = bytes.fromhex("f8e85000")  # AudioSpecificConfig: ER AAC-ELD, 44.1 kHz, stereo, 480 frames
AAC_LC_CONFIG = bytes.fromhex("1210")  # AAC-LC, 44.1 kHz, stereo


def alac_config(frames_per_packet: int) -> bytes:
    """ALAC magic cookie ('alac' atom) for 16-bit stereo 44.1 kHz."""
    return (struct.pack(">I4sI", 36, b"alac", 0)
            + struct.pack(">IBBBBBBHIII", frames_per_packet, 0, 16, 40, 10, 14, 2, 255, 0, 0, 44100))


def audio_decoder(compression_type: int, frames_per_packet: int) -> av.AudioCodecContext:
    if compression_type == 8:
        name, config = "aac", AAC_ELD_CONFIG  # what screen mirroring uses
    elif compression_type == 4:
        name, config = "aac", AAC_LC_CONFIG
    elif compression_type == 2:
        name, config = "alac", alac_config(frames_per_packet)
    else:
        raise ValueError(f"unsupported audio compression type {compression_type}")
    decoder = av.CodecContext.create(name, "r")
    decoder.extradata = config
    return decoder


class AudioStream:
    """App/system sound: RTP over UDP, AES-CBC encrypted, decoded to 16-bit PCM for AudioPlayer."""

    RTP_HEADER_SIZE = 12

    def __init__(self, receiver: Receiver, session: int, aes_key: bytes, aes_iv: bytes,
                 decoder: av.AudioCodecContext) -> None:
        self.receiver = receiver
        self.session = session
        self._key = aes_key
        self._iv = aes_iv
        self._decoder = decoder
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)
        self._data = udp_socket()
        self._control = udp_socket()  # resend requests and sync packets land here; not needed
        self.data_port = self._data.getsockname()[1]
        self.control_port = self._control.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def close(self) -> None:
        self._data.close()
        self._control.close()

    def _run(self) -> None:
        last_seq = None
        try:
            while True:
                packet = self._data.recv(4096)
                if len(packet) <= self.RTP_HEADER_SIZE:
                    continue
                seq = struct.unpack_from(">H", packet, 2)[0]
                if last_seq is not None and not 0 < (seq - last_seq) & 0xFFFF < 0x8000:
                    continue  # duplicate or late
                last_seq = seq
                self.receiver.stats.add(len(packet), False)
                if self.receiver.is_current(self.session):
                    self._play(packet[self.RTP_HEADER_SIZE:])
        except OSError:
            pass  # closed

    def _play(self, payload: bytes) -> None:
        # Whole 16-byte blocks are encrypted, each packet from the same IV; the tail is plain.
        n = len(payload) & ~15
        decryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).decryptor()
        data = decryptor.update(payload[:n]) + payload[n:]
        try:
            frames = self._decoder.decode(av.Packet(data))
        except av.error.FFmpegError:
            return
        for frame in frames:
            for out in self._resampler.resample(frame):
                self.receiver.audio.push(out.sample_rate, 2, out.to_ndarray().tobytes())


def udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    sock.bind(("0.0.0.0", 0))
    return sock


# --- Timing --------------------------------------------------------------------
NTP_EPOCH_OFFSET = 2208988800  # 1900-01-01 -> 1970-01-01


class TimingClient:
    """Asks the phone for its clock every few seconds, as AirPlay receivers do.

    Only the requests matter to the phone; we don't sync audio to video, so
    the answers are ignored."""

    INTERVAL = 3.0

    def __init__(self, address: str, port: int | None) -> None:
        self._sock = udp_socket()
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        if port:
            threading.Thread(target=self._run, args=((address, port),), daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self._sock.close()

    def _run(self, target: tuple[str, int]) -> None:
        while not self._stop.is_set():
            now = time.time() + NTP_EPOCH_OFFSET
            request = struct.pack(">BBH20xII", 0x80, 0xD2, 0x0007, int(now), int(now % 1 * 2**32))
            try:
                self._sock.sendto(request, target)
            except OSError:
                return
            self._stop.wait(self.INTERVAL)
