from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import requests
import os
import secrets

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

# In-memory token store: { token: raw_url }
_url_store = {}


def get_video_info(url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

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
            "raw_url": f.get("url"),
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
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if "twitter.com" not in url and "x.com" not in url:
        return jsonify({"error": "Only X / Twitter URLs are supported"}), 400

    try:
        data = get_video_info(url)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Could not extract video: {str(e)}"}), 422
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

    safe_videos = []
    for v in data["videos"]:
        token = secrets.token_urlsafe(32)
        _url_store[token] = v.pop("raw_url")
        safe_videos.append({**v, "token": token})

    data["videos"] = safe_videos
    return jsonify(data)


@app.route("/api/download", methods=["GET"])
def download():
    """
    Streams the full video through Railway using HTTP Range requests.

    How it works:
    1. Browser asks for bytes 0–end  (or a specific range if resuming)
    2. We forward that exact Range header to Twitter's CDN
    3. Twitter's CDN returns only that chunk
    4. We pipe it straight to the browser

    This means:
    - No timeout — each range request is small and fast
    - Browser can resume if connection drops
    - Works for videos of any length/size
    """
    token = request.args.get("token", "").strip()
    quality = request.args.get("quality", "video").strip()

    if not token or token not in _url_store:
        return jsonify({"error": "Invalid or expired token. Please click Fetch again."}), 400

    raw_url = _url_store[token]

    # Forward whatever Range header the browser sent (for seeking / resuming)
    range_header = request.headers.get("Range", "bytes=0-")

    upstream_headers = {
        **FAKE_HEADERS,
        "Range": range_header,
    }

    try:
        upstream = requests.get(
            raw_url,
            headers=upstream_headers,
            stream=True,
            timeout=60,          # per-chunk timeout, not total download time
        )
        # 206 = partial content (range request), 200 = full file — both are fine
        if upstream.status_code not in (200, 206):
            return jsonify({"error": f"Source returned HTTP {upstream.status_code}"}), 502

    except requests.RequestException as e:
        return jsonify({"error": f"Failed to reach video source: {str(e)}"}), 502

    filename = f"x_video_{quality}.mp4"

    # Pass through the headers Twitter gives us so the browser knows the size
    response_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type":        upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges":       "bytes",
    }

    # These headers tell the browser total file size and the byte range being served
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            response_headers[h] = upstream.headers[h]

    def generate():
        # 512 KB chunks — large enough to be fast, small enough to not block
        for chunk in upstream.iter_content(chunk_size=512 * 1024):
            if chunk:
                yield chunk

    # Return 206 Partial Content if the browser asked for a range, else 200
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
