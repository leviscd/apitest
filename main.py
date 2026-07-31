from flask import Flask, request, jsonify, Response
from youtubesearchpython import VideosSearch, Video
import yt_dlp
import urllib.parse
import re
import requests

app = Flask(__name__)

# Configura o Flask para aceitar Emojis e acentos nativos sem escapar para ASCII
app.config['JSON_AS_ASCII'] = False
app.json.ensure_ascii = False

YDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'skip_download': True,
    'no_cached_dir': True
}

# Mapa de extensão real -> Content-Type correto.
MIME_MAP = {
    'mp4': 'video/mp4',
    'webm': 'video/webm',
    'mkv': 'video/x-matroska',
    'mov': 'video/quicktime',
    'm4a': 'audio/mp4',
    'mp3': 'audio/mpeg',
    'opus': 'audio/opus',
    'ogg': 'audio/ogg',
    'wav': 'audio/wav',
}


# 🌐 ROTA 1: Busca Instantânea (< 500ms) - youtube-search-python
@app.route('/search', methods=['GET'])
def search():
    q = request.args.get('q')
    if not q:
        return jsonify({"success": False, "error": "O parâmetro 'q' é obrigatório."}), 400

    q_decoded = urllib.parse.unquote(q).strip()

    try:
        if re.match(r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/', q_decoded):
            video_info = Video.get(q_decoded)
            if not video_info:
                return jsonify({"success": False, "error": "nada encontrado, tente outro termo"}), 404
            return jsonify({
                "success": True,
                "results": [{
                    "title": video_info.get('title'),
                    "duration": video_info.get('duration', {}).get('accessibilityLabel', '00:00'),
                    "thumbnail": video_info.get('thumbnails', [{}])[0].get('url'),
                    "channel": video_info.get('channel', {}).get('name'),
                    "views": video_info.get('viewCount', {}).get('short'),
                    "url": video_info.get('link')
                }]
            })

        videos_search = VideosSearch(q_decoded, limit=20)
        search_result = videos_search.result()

        raw_videos = search_result.get('result', [])
        if not raw_videos:
            return jsonify({"success": False, "error": "nada encontrado, tente outro termo"}), 404

        results = []
        for video in raw_videos:
            results.append({
                "title": video.get('title'),
                "duration": video.get('duration'),
                "thumbnail": video.get('thumbnails', [{}])[0].get('url'),
                "channel": video.get('channel', {}).get('name'),
                "views": video.get('viewCount', {}).get('short'),
                "url": video.get('link')
            })

        return jsonify({"success": True, "results": results})

    except Exception as e:
        return jsonify({"success": False, "error": "nada encontrado, tente outro termo", "details": str(e)}), 404


# 🌐 ROTA 2: Informações de Qualidade Instantâneas - yt-dlp sem download
@app.route('/info', methods=['GET'])
def info():
    url = request.args.get('url')
    if not url:
        return jsonify({"success": False, "error": "O parâmetro 'url' é obrigatório."}), 400

    decoded_url = urllib.parse.unquote(url)
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': False}) as ydl:
            info_dict = ydl.extract_info(decoded_url, download=False)
            formats = info_dict.get('formats', [])

            qualities = []
            for f in formats:
                if f.get('url'):
                    vcodec = f.get('vcodec', 'none')
                    is_audio_only = vcodec == 'none' or vcodec is None
                    qualities.append({
                        "format_id": str(f.get('format_id')),
                        "ext": f.get('ext'),
                        "vcodec": f.get('vcodec'),
                        "acodec": f.get('acodec'),
                        "quality_label": f.get('format_note') or f.get('resolution') or f.get('quality'),
                        "type": "audio" if is_audio_only else "video"
                    })

            return jsonify({
                "success": True,
                "title": info_dict.get('title'),
                "thumbnail": info_dict.get('thumbnail'),
                "channel": info_dict.get('uploader'),
                "available_qualities": qualities
            })
    except Exception as e:
        return jsonify({"success": False, "error": "Erro ao ler qualidades.", "details": str(e)}), 500


# 🌐 ROTA 3: Resolve Rápido
# CORRIGIDA: força H.264 (avc1) no vídeo e AAC (m4a) no áudio sempre que
# possível, porque AV1/VP9 e Opus/WebM não são suportados nativamente pelo
# iOS (Photos/Files recusam salvar/tocar). Continua devolvendo vídeo e áudio
# como duas URLs separadas (downloadUrl + audioUrl), igual antes — sem merge.
@app.route('/resolve', methods=['GET'])
def resolve():
    url = request.args.get('url')
    type_param = request.args.get('type')
    quality = request.args.get('quality')

    if not url or type_param not in ['mp3', 'mp4']:
        return jsonify({"success": False, "error": "Parâmetros inválidos."}), 400

    decoded_url = urllib.parse.unquote(url)

    if type_param == 'mp3':
        # Prioriza áudio já em AAC/M4A (compatível universalmente).
        # Só cai pro bestaudio genérico (pode vir Opus/WebM) se nada m4a existir.
        format_selector = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio"
    else:
        quality_num = quality.replace('p', '') if quality else None
        height_filter = f"[height<={quality_num}]" if quality_num else ""
        format_selector = (
            f"bestvideo[vcodec^=avc1]{height_filter}[ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[vcodec^=avc1]{height_filter}+bestaudio[ext=m4a]/"
            f"best[vcodec^=avc1][ext=mp4]{height_filter}/"
            f"best[ext=mp4]{height_filter}/best"
        )

    try:
        with yt_dlp.YoutubeDL({**YDL_OPTS_BASE, 'format': format_selector}) as ydl:
            info_dict = ydl.extract_info(decoded_url, download=False)
            requested_formats = info_dict.get('requested_formats')
            video_title = info_dict.get('title', 'download')

            host = request.host
            protocol = 'https' if request.is_secure else 'http'

            if requested_formats and len(requested_formats) >= 2:
                v_url = requested_formats[0].get('url')
                v_ext = requested_formats[0].get('ext', 'mp4')
                a_url = requested_formats[1].get('url')
                a_ext = requested_formats[1].get('ext', 'm4a')

                proxy_video = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(v_url)}&ext={v_ext}&title={urllib.parse.quote(video_title + '_video')}"
                proxy_audio = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(a_url)}&ext={a_ext}&title={urllib.parse.quote(video_title + '_audio')}"

                return jsonify({
                    "success": True,
                    "title": video_title,
                    "type": "split",
                    "videoExt": v_ext,
                    "audioExt": a_ext,
                    "quality": info_dict.get('height'),
                    "downloadUrl": proxy_video,
                    "audioUrl": proxy_audio
                })

            # Formato único (já vem com áudio+vídeo juntos, ou é só áudio).
            direct_url = info_dict.get('url')
            direct_ext = info_dict.get('ext', type_param)
            proxy_url = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(direct_url)}&ext={direct_ext}&title={urllib.parse.quote(video_title)}"

            return jsonify({
                "success": True,
                "title": video_title,
                "type": direct_ext,
                "quality": info_dict.get('height') or info_dict.get('format_note'),
                "downloadUrl": proxy_url
            })
    except Exception as e:
        return jsonify({"success": False, "error": "Qualidade não disponível.", "details": str(e)}), 400


# 🌐 ROTA 4: Proxy Stream Turbo (Armazenamento ZERO / Buffer de Alta Velocidade de 1MB)
@app.route('/download-stream', methods=['GET'])
def download_stream():
    target_url = request.args.get('url')
    ext = request.args.get('ext', 'mp4').lower()
    title = request.args.get('title')

    if not target_url:
        return "URL de mídia ausente.", 400
    decoded_target = urllib.parse.unquote(target_url)

    try:
        client_range = request.headers.get('Range', 'bytes=0-')

        req_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.youtube.com/',
            'Origin': 'https://www.youtube.com',
            'Connection': 'keep-alive',
            'Range': client_range,
            'Accept-Encoding': 'identity'
        }

        res_googlevideo = requests.get(
            decoded_target,
            headers=req_headers,
            stream=True,
            timeout=15
        )

        if res_googlevideo.status_code not in (200, 206):
            return (
                f"Erro ao buscar mídia na origem (status {res_googlevideo.status_code}).",
                502
            )

        clean_title = re.sub(r'[^a-zA-Z0-9]', '_', urllib.parse.unquote(title or 'download'))
        content_type = MIME_MAP.get(ext, 'application/octet-stream')

        def generate_chunks():
            for chunk in res_googlevideo.iter_content(chunk_size=1048576):
                if chunk:
                    yield chunk

        response = Response(
            generate_chunks(),
            status=res_googlevideo.status_code,
            content_type=content_type
        )
        response.headers['Content-Disposition'] = f'attachment; filename="{clean_title}.{ext}"'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Accept-Ranges'] = 'bytes'

        if res_googlevideo.headers.get('Content-Length'):
            response.headers['Content-Length'] = res_googlevideo.headers.get('Content-Length')
        if res_googlevideo.headers.get('Content-Range'):
            response.headers['Content-Range'] = res_googlevideo.headers.get('Content-Range')

        return response

    except Exception as e:
        return f"Erro no tunelamento do arquivo: {str(e)}", 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000, debug=False, threaded=True)
