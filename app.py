from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import os

app = Flask(__name__)
CORS(app)  # Allow requests from your frontend

def get_video_info(url):
    """Extract video info using yt-dlp without downloading."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    videos = []
    seen = set()

    if 'formats' in info:
        for f in info['formats']:
            # Only MP4 video formats with both video and audio
            if f.get('vcodec') == 'none' or f.get('acodec') == 'none':
                continue
            if f.get('ext') != 'mp4':
                continue

            height = f.get('height') or 0
            quality = f"{height}p" if height else "Unknown"

            if quality in seen:
                continue
            seen.add(quality)

            videos.append({
                'url': f.get('url'),
                'quality': quality,
                'height': height,
                'ext': f.get('ext', 'mp4'),
                'filesize': f.get('filesize') or f.get('filesize_approx'),
            })

    # Sort by quality descending
    videos.sort(key=lambda x: x['height'], reverse=True)

    # Label first as HD
    for i, v in enumerate(videos):
        if i == 0 and v['height'] >= 720:
            v['label'] = 'HD'
        elif v['height'] >= 480:
            v['label'] = 'SD'
        else:
            v['label'] = 'Low'

    return {
        'author': info.get('uploader', 'Unknown'),
        'handle': info.get('uploader_id', ''),
        'text': info.get('description', ''),
        'thumbnail': info.get('thumbnail', ''),
        'duration': info.get('duration'),
        'videos': videos,
    }


@app.route('/api/info', methods=['GET'])
def info():
    url = request.args.get('url', '').strip()

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    if 'twitter.com' not in url and 'x.com' not in url:
        return jsonify({'error': 'Only X / Twitter URLs are supported'}), 400

    try:
        data = get_video_info(url)
        return jsonify(data)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({'error': f'Could not extract video: {str(e)}'}), 422
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
