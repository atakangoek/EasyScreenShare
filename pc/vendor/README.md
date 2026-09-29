# Vendored code

`ap2/` comes from [openairplay/airplay2-receiver](https://github.com/openairplay/airplay2-receiver)
(commit `6c343d3`, GPLv2). Only the FairPlay pieces that AirPlay screen mirroring needs are kept:

| File | Upstream | Changes |
| --- | --- | --- |
| `ap2/fairplay3.py` | `ap2/fairplay3.py` | Debug prints silenced; removed the process-wide `sys.stdout` redirect |
| `ap2/playfair.py` | `ap2/playfair.py` | Only the fp-setup reply tables and `fairplay_setup()`; the RSA/pycryptodome code is dropped |

The rest of the AirPlay receiver (pairing, RTSP, streams) is `../airplay.py`.

Because this code is GPLv2, distributing the PC receiver with it (e.g. the PyInstaller build)
falls under the GPLv2 as well.
