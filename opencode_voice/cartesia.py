from __future__ import annotations

import urllib.parse


def build_tts_url(version: str) -> str:
    # Model, voice, and output format travel per-request in the JSON body
    # (unlike Deepgram's query-string form), so the socket URL only carries
    # the API version.
    return f"wss://api.cartesia.ai/tts/websocket?{urllib.parse.urlencode({'cartesia_version': version})}"
