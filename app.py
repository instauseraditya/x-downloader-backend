from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import requests
import os
import secrets

app = Flask(__name__)
CORS(app)

# Headers that mimic a real browser / Twitter session so twimg.com won't block us
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
# Tokens are created fresh on every /api/info call and never sent raw to the browser
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

    # Swap raw URLs for safe proxy tokens before sending to the browser
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
    Streams the video through our Railway server.
    The browser hits /api/download?token=XXX&quality=720p
    We look up the real signed twimg.com URL, fetch it with proper headers,
    and pipe it straight to the user as a file download.
    """
    token = request.args.get("token", "").strip()
    quality = request.args.get("quality", "video").strip()

    if not token or token not in _url_store:
        return jsonify({"error": "Invalid or expired download token. Please fetch the video again."}), 400

    raw_url = _url_store[token]

    try:
        upstream = requests.get(raw_url, headers=FAKE_HEADERS, stream=True, timeout=30)
        upstream.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to fetch video from source: {str(e)}"}), 502

    filename = f"x_video_{quality}.mp4"
    content_length = upstream.headers.get("Content-Length")

    response_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "video/mp4",
    }
    if content_length:
        response_headers["Content-Length"] = content_length

    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):  # 64 KB chunks
            if chunk:
                yield chunk

    return Response(
        stream_with_context(generate()),
        status=200,
        headers=response_headers,
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
