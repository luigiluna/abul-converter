"""Check what the Deezer account behind DEFAULT_ARL can stream.

    python -m app.diagnose [deezer_track_id]

Prints the account's plan flags and asks Deezer directly for FLAC / MP3_320 /
MP3_128 URLs for one track (bypassing deezer-py's own license check).
Never prints the ARL or the license token.
"""

from __future__ import annotations

import json
import sys

import deezer

from app.backend.settings import default_arl

DEFAULT_TRACK = "3135556"  # Daft Punk - Harder Better Faster Stronger
FORMATS = ("FLAC", "MP3_320", "MP3_128")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    arl = default_arl()
    if not arl:
        print("DEFAULT_ARL is empty in .env")
        return 1
    track_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRACK

    dz = deezer.Deezer()
    if not dz.login_via_arl(arl):
        print("Login failed: the ARL is invalid or expired.")
        return 1

    user = dz.gw.get_user_data()["USER"]
    options = user.get("OPTIONS", {})
    multi = user.get("MULTI_ACCOUNT", {})
    print(f"Account: {user.get('BLOG_NAME')} (id {user.get('USER_ID')})")
    print(f"Country: {options.get('license_country')}  Offer: {user.get('OFFER_NAME') or options.get('offer_name') or '?'}")
    print(f"Family plan: {multi.get('ENABLED')}  sub-account: {multi.get('IS_SUB_ACCOUNT')}")
    for flag in ("web_hq", "mobile_hq", "web_lossless", "mobile_lossless", "web_streaming", "ads_display"):
        print(f"  {flag:16} {options.get(flag)}")
    print(f"deezer-py sees: can_stream_hq={dz.current_user.get('can_stream_hq')} "
          f"can_stream_lossless={dz.current_user.get('can_stream_lossless')}")

    token = dz.gw.get_track(track_id).get("TRACK_TOKEN")
    print(f"\nAsking Deezer directly for track {track_id}:")
    for fmt in FORMATS:
        resp = dz.session.post("https://media.deezer.com/v1/get_url", json={
            "license_token": dz.current_user["license_token"],
            "media": [{"type": "FULL", "formats": [{"cipher": "BF_CBC_STRIPE", "format": fmt}]}],
            "track_tokens": [token],
        }, headers=dz.http_headers).json()
        data = (resp.get("data") or [{}])[0]
        if data.get("media"):
            print(f"  {fmt:8} OK ({data['media'][0].get('format')})")
        else:
            print(f"  {fmt:8} refused: {json.dumps(data.get('errors') or resp.get('errors'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
