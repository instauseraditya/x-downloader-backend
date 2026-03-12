from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import requests
import os
import secrets
import time

app = Flask(__name__)
CORS(app)

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

# Store the TWEET URL (not the signed video URL) against each token.
# Tweet URLs never expire, so even if Railway restarts we can re-extract.
# Structure: { token: { "tweet_url": str, "height": int, "created": float } }
_token_store = {}

# Clean up tokens older than 2 hours to avoid memory leak
def _cleanup():
    cutoff = time.time() - 7200
    expired = [k for k, v in _token_store.items() if v["created"] < cutoff]
    for k in expired:
        del _token_store[k]


def get_fresh_video_url(tweet_url, target_height):
    """
    Re-runs yt-dlp at download time to get a brand-new signed URL.
    This guarantees the URL is always fresh and never expired.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(tweet_url, download=False)

    best_url = None
    best_diff = float("inf")

    for f in info.get("formats", []):
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue
        if f.get("ext") != "mp4":
            continue
        h = f.get("height") or 0
        diff = abs(h - target_height)
        if diff < best_diff:
            best_diff = diff
            best_url = f.get("url")

    return best_url


def get_video_info(tweet_url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
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
            "ext": "mp4",
            "filesize": f.get("filesize") or f.get("filesize_approx"),
        })

    videos.sort(key=lambda x: x["height"], reverse=True)

    for v in videos:
        if v["height"] >= 720:
            v["label"] = "HD"
        elif v["height"] >= 480:
            v["label"] = "SD"
        else:
            v["label"] = "Low"

    return {
        "author": info.get("uploader", "Unknown"),
        "handle": info.get("uploader_id", ""),
        "text": info.get("description", ""),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration"),
        "videos": videos,
    }


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

    _cleanup()

    # Store tweet URL + height in the token — NOT the signed video URL
    safe_videos = []
    for v in data["videos"]:
        token = secrets.token_urlsafe(32)
        _token_store[token] = {
            "tweet_url": tweet_url,
            "height": v["height"],
            "created": time.time(),
        }
        safe_videos.append({**v, "token": token})

    data["videos"] = safe_videos
    return jsonify(data)


@app.route("/api/download", methods=["GET"])
def download():
    """
    1. Look up the tweet URL + target height from the token
    2. Re-run yt-dlp RIGHT NOW to get a fresh signed video URL
    3. Stream it to the browser using Range requests (handles large files)
    """
    token = request.args.get("token", "").strip()
    quality = request.args.get("quality", "video").strip()

    if not token or token not in _token_store:
        return jsonify({
            "error": "Download link expired. Please click Fetch again on the page."
        }), 400

    entry = _token_store[token]
    tweet_url = entry["tweet_url"]
    target_height = entry["height"]

    # Get a brand-new signed URL right now
    try:
        fresh_video_url = get_fresh_video_url(tweet_url, target_height)
    except Exception as e:
        return jsonify({"error": f"Could not refresh video URL: {str(e)}"}), 502

    if not fresh_video_url:
        return jsonify({"error": "No matching video format found."}), 404

    # Forward browser's Range header so large videos stream correctly
    range_header = request.headers.get("Range", "bytes=0-")
    upstream_headers = {**FAKE_HEADERS, "Range": range_header}

    try:
        upstream = requests.get(
            fresh_video_url,
            headers=upstream_headers,
            stream=True,
            timeout=60,
        )
        if upstream.status_code not in (200, 206):
            return jsonify({"error": f"Source returned HTTP {upstream.status_code}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to reach video source: {str(e)}"}), 502

    filename = f"x_video_{quality}.mp4"
    response_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": "bytes",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            response_headers[h] = upstream.headers[h]

    def generate():
        for chunk in upstream.iter_content(chunk_size=512 * 1024):
            if chunk:
                yield chunk

    status_code = 206 if upstream.status_code == 206 else 200
    return Response(
        stream_with_context(generate()),
        status=status_code,
        headers=response_headers,
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
