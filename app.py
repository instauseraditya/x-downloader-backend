from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import requests
import os
import base64
import json
import hmac
import hashlib

app = Flask(__name__)
CORS(app)

# Secret key for signing tokens — Railway sets this as an env variable
# You will add this in Railway dashboard (instructions below)
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-railway")

FAKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://x.com/",
    "Origin": "https://x.com",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}


# ── Token helpers ─────────────────────────────────────────────────────────────
# Instead of storing anything in memory, we encode the tweet URL + height
# directly inside the token and sign it with HMAC so it can't be tampered with.
# Railway restarts cannot break this because there is nothing stored in memory.

def make_token(tweet_url: str, height: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": tweet_url, "h": height}).encode()
    ).decode()
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def decode_token(token: str):
    """Returns (tweet_url, height) or raises ValueError if invalid/tampered."""
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        raise ValueError("Malformed token")
    expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("Token signature invalid")
    data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    return data["u"], data["h"]


# ── yt-dlp helpers ────────────────────────────────────────────────────────────

def get_video_info(tweet_url: str):
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(tweet_url, download=False)

    videos = []
    seen = set()
    for f in info.get("formats", []):
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue
        if f.get("ext") != "mp4":
            continue
        height = f.get("height") or 0
        quality = f"{height}p" if height else "Unknown"
        if quality in seen:
            continue
        seen.add(quality)
        videos.append({
            "quality": quality,
            "height": height,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
        })

    videos.sort(key=lambda x: x["height"], reverse=True)
    for v in videos:
        v["label"] = "HD" if v["height"] >= 720 else ("SD" if v["height"] >= 480 else "Low")

    return {
        "author":    info.get("uploader", "Unknown"),
        "handle":    info.get("uploader_id", ""),
        "text":      info.get("description", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration":  info.get("duration"),
        "videos":    videos,
    }


def get_fresh_signed_url(tweet_url: str, target_height: int) -> str:
    """Re-runs yt-dlp right now to get a brand-new signed CDN URL."""
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(tweet_url, download=False)

    best_url, best_diff = None, float("inf")
    for f in info.get("formats", []):
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue
        if f.get("ext") != "mp4":
            continue
        diff = abs((f.get("height") or 0) - target_height)
        if diff < best_diff:
            best_diff = diff
            best_url = f["url"]
    return best_url


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/info", methods=["GET"])
def info():
    tweet_url = request.args.get("url", "").strip()
    if not tweet_url:
        return jsonify({"error": "No URL provided"}), 400
    if "twitter.com" not in tweet_url and "x.com" not in tweet_url:
        return jsonify({"error": "Only X / Twitter URLs are supported"}), 400

    try:
        data = get_video_info(tweet_url)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Could not extract video: {str(e)}"}), 422
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

    # Embed tweet URL + height inside the token — nothing stored in memory
    for v in data["videos"]:
        v["token"] = make_token(tweet_url, v["height"])

    return jsonify(data)


@app.route("/api/download", methods=["GET"])
def download():
    raw_token = request.args.get("token", "").strip()
    quality   = request.args.get("quality", "video").strip()

    if not raw_token:
        return jsonify({"error": "Missing token."}), 400

    # Decode token — works even after Railway restarts because nothing is in memory
    try:
        tweet_url, target_height = decode_token(raw_token)
    except ValueError:
        return jsonify({"error": "Invalid token. Please click Fetch again."}), 400

    # Get a fresh signed URL from yt-dlp right now
    try:
        fresh_url = get_fresh_signed_url(tweet_url, target_height)
    except Exception as e:
        return jsonify({"error": f"Could not fetch fresh video URL: {str(e)}"}), 502

    if not fresh_url:
        return jsonify({"error": "No matching video format found."}), 404

    # Stream to browser with Range support (handles large files)
    range_header = request.headers.get("Range", "bytes=0-")
    upstream_headers = {**FAKE_HEADERS, "Range": range_header}

    try:
        upstream = requests.get(fresh_url, headers=upstream_headers, stream=True, timeout=60)
        if upstream.status_code not in (200, 206):
            return jsonify({"error": f"CDN returned HTTP {upstream.status_code}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Stream error: {str(e)}"}), 502

    resp_headers = {
        "Content-Disposition": f'attachment; filename="x_video_{quality}.mp4"',
        "Content-Type":        upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges":       "bytes",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            resp_headers[h] = upstream.headers[h]

    def generate():
        for chunk in upstream.iter_content(chunk_size=512 * 1024):
            if chunk:
                yield chunk

    return Response(
        stream_with_context(generate()),
        status=206 if upstream.status_code == 206 else 200,
        headers=resp_headers,
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
