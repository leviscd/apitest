from flask import Flask, request, jsonify, Response
import yt_dlp
import innertube
import urllib.parse
import re
import requests
import os
import sys

app = Flask(__name__)

app.config['JSON_AS_ASCII'] = False
app.json.ensure_ascii = False

@app.after_request
def force_utf8_charset(response):
    if response.mimetype == 'application/json':
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
    return response

# ============================================================================
# CONFIGURAÇÃO DE COOKIES COM CAMINHO ABSOLUTO
# ============================================================================
# Obtém o diretório onde este script está localizado
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Caminho absoluto para o cookies.txt
COOKIES_FILE = os.environ.get('COOKIES_FILE', os.path.join(BASE_DIR, 'cookies.txt'))
COOKIES_AVAILABLE = os.path.isfile(COOKIES_FILE)

# LOGS DE DEPURAÇÃO (aparecerão no pm2 logs)
print(f"[DEBUG] BASE_DIR = {BASE_DIR}")
print(f"[DEBUG] COOKIES_FILE = {COOKIES_FILE}")
print(f"[DEBUG] COOKIES_AVAILABLE = {COOKIES_AVAILABLE}")
print(f"[DEBUG] PATH = {os.environ.get('PATH')}")

# Verifica se o Node.js está acessível
node_available = os.system('which node > /dev/null 2>&1') == 0
print(f"[DEBUG] Node disponível? {node_available}")

if not COOKIES_AVAILABLE:
    print("[AVISO] cookies.txt não encontrado — seguindo sem autenticação.")

# ============================================================================
# OPÇÕES BASE DO YT-DLP
# ============================================================================
YDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'skip_download': True,
    'no_cached_dir': True,
    'js_runtimes': {'node': {}},
    'remote_components': {'ejs:github'},
}

if COOKIES_AVAILABLE:
    YDL_OPTS_BASE['cookiefile'] = COOKIES_FILE

# Mapa de extensões
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

# ============================================================================
# INNERTUBE
# ============================================================================
client_yt = innertube.InnerTube("WEB")
client_yt_android = innertube.InnerTube("ANDROID")

def find_keys(obj, key_name):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key_name:
                yield v
            yield from find_keys(v, key_name)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_keys(item, key_name)

def extract_video_id(text):
    match = re.search(
        r'(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|embed/|v/)|[?&]v=)([\w-]{11})',
        text
    )
    return match.group(1) if match else None

def format_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "AO VIVO"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def best_thumbnail(thumbnails, video_id=None):
    if thumbnails:
        return thumbnails[-1].get('url')
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None

def parse_video_renderer(vr):
    video_id = vr.get('videoId')
    if not video_id:
        return None
    title = None
    if vr.get('title', {}).get('runs'):
        title = vr['title']['runs'][0].get('text')
    elif vr.get('title', {}).get('simpleText'):
        title = vr['title']['simpleText']
    channel = None
    if vr.get('ownerText', {}).get('runs'):
        channel = vr['ownerText']['runs'][0].get('text')
    elif vr.get('longBylineText', {}).get('runs'):
        channel = vr['longBylineText']['runs'][0].get('text')
    duration = vr.get('lengthText', {}).get('simpleText', 'AO VIVO')
    return {
        "title": title,
        "channel": channel,
        "duration": duration,
        "thumbnail": best_thumbnail(vr.get('thumbnail', {}).get('thumbnails', []), video_id),
        "url": f"https://www.youtube.com/watch?v={video_id}"
    }

def parse_video_details(video_details):
    video_id = video_details.get('videoId')
    return {
        "title": video_details.get('title'),
        "channel": video_details.get('author'),
        "duration": format_duration(video_details.get('lengthSeconds')),
        "thumbnail": best_thumbnail(video_details.get('thumbnail', {}).get('thumbnails', []), video_id),
        "url": f"https://www.youtube.com/watch?v={video_id}"
    }

def get_player_data(video_id):
    for client in (client_yt_android, client_yt):
        try:
            data = client.player(video_id)
        except Exception:
            continue
        video_details = next(find_keys(data, 'videoDetails'), None)
        if video_details:
            return data, video_details
    return None, None

MAX_SEARCH_RESULTS = 200
MAX_SEARCH_PAGES = 10

def innertube_search_all(query, max_pages=1):
    results = []
    seen_ids = set()
    data = client_yt.search(query)
    for _ in range(max(1, max_pages)):
        for vr in find_keys(data, 'videoRenderer'):
            parsed = parse_video_renderer(vr)
            if not parsed:
                continue
            vid = parsed['url'].rsplit('=', 1)[-1]
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            results.append(parsed)
            if len(results) >= MAX_SEARCH_RESULTS:
                return results
        token = next((cc.get('token') for cc in find_keys(data, 'continuationCommand') if cc.get('token')), None)
        if not token:
            break
        data = client_yt.search(continuation=token)
    return results

# ============================================================================
# ROTAS
# ============================================================================

@app.route('/search', methods=['GET'])
def search():
    q = request.args.get('q')
    if not q:
        return jsonify({"success": False, "error": "O parâmetro 'q' é obrigatório."}), 400

    q_decoded = urllib.parse.unquote(q).strip()

    try:
        pages_param = request.args.get('pages')
        try:
            max_pages = max(1, min(int(pages_param), MAX_SEARCH_PAGES)) if pages_param else 1
        except ValueError:
            max_pages = 1

        is_link = bool(re.match(r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/', q_decoded))

        if is_link:
            video_id = extract_video_id(q_decoded)
            if not video_id:
                return jsonify({"success": False, "error": "nada encontrado, tente outro termo"}), 404

            _, video_details = get_player_data(video_id)
            if not video_details:
                return jsonify({"success": False, "error": "nada encontrado, tente outro termo"}), 404

            return jsonify({"success": True, "results": [parse_video_details(video_details)]})

        results = innertube_search_all(q_decoded, max_pages=max_pages)
        if not results:
            return jsonify({"success": False, "error": "nada encontrado, tente outro termo"}), 404

        return jsonify({"success": True, "count": len(results), "results": results})

    except Exception as e:
        return jsonify({"success": False, "error": "nada encontrado, tente outro termo", "details": str(e)}), 404

@app.route('/info', methods=['GET'])
def info():
    url = request.args.get('url')
    if not url:
        return jsonify({"success": False, "error": "O parâmetro 'url' é obrigatório."}), 400

    decoded_url = urllib.parse.unquote(url)
    video_id = extract_video_id(decoded_url)
    if not video_id:
        return jsonify({"success": False, "error": "URL do YouTube inválida."}), 400

    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    base_info = None
    qualities = []
    source = "innertube"

    try:
        data, video_details = get_player_data(video_id)
        if video_details:
            base_info = parse_video_details(video_details)
            streaming_data = next(find_keys(data, 'streamingData'), {}) or {}
            raw_formats = streaming_data.get('formats', []) + streaming_data.get('adaptiveFormats', [])
            for f in raw_formats:
                if not f.get('url'):
                    continue
                mime = f.get('mimeType', '')
                ext_match = re.search(r'/(\w+)', mime)
                ext = ext_match.group(1) if ext_match else None
                is_video = bool(f.get('qualityLabel'))
                qualities.append({
                    "format_id": str(f.get('itag')),
                    "ext": ext,
                    "mime_type": mime,
                    "quality_label": f.get('qualityLabel') or f.get('audioQuality'),
                    "type": "video" if is_video else "audio"
                })
    except Exception:
        pass

    if not qualities:
        try:
            with yt_dlp.YoutubeDL({**YDL_OPTS_BASE, 'format': 'best'}) as ydl:
                info_dict = ydl.extract_info(watch_url, download=False)
            source = "yt-dlp"
            if not base_info:
                base_info = {
                    "title": info_dict.get('title'),
                    "channel": info_dict.get('uploader'),
                    "duration": format_duration(info_dict.get('duration')),
                    "thumbnail": info_dict.get('thumbnail'),
                    "url": watch_url
                }
            for f in info_dict.get('formats', []):
                if not f.get('url'):
                    continue
                vcodec = f.get('vcodec', 'none')
                is_audio_only = vcodec == 'none' or vcodec is None
                qualities.append({
                    "format_id": str(f.get('format_id')),
                    "ext": f.get('ext'),
                    "mime_type": None,
                    "quality_label": f.get('format_note') or f.get('resolution') or f.get('quality'),
                    "type": "audio" if is_audio_only else "video"
                })
        except Exception as e:
            if not base_info:
                return jsonify({"success": False, "error": "Erro ao ler qualidades.", "details": str(e)}), 500

    if not base_info:
        return jsonify({"success": False, "error": "Erro ao ler qualidades.", "details": "vídeo indisponível"}), 404

    return jsonify({
        **base_info,
        "success": True,
        "source": source,
        "available_qualities": qualities
    })

@app.route('/resolve', methods=['GET'])
def resolve():
    url = request.args.get('url')
    type_param = request.args.get('type')
    quality = request.args.get('quality')

    if not url or type_param not in ['mp3', 'mp4']:
        return jsonify({"success": False, "error": "Parâmetros inválidos."}), 400

    decoded_url = urllib.parse.unquote(url)
    video_id = extract_video_id(decoded_url)
    if not video_id:
        return jsonify({"success": False, "error": "URL inválida."}), 400

    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    # --- 1) TENTATIVA RÁPIDA: INNERTUBE ANDROID ---
    try:
        data, _ = get_player_data(video_id)
        streaming_data = next(find_keys(data, 'streamingData'), {})
        formats = streaming_data.get('formats', [])
        adaptive = streaming_data.get('adaptiveFormats', [])

        target_height = None
        if type_param == 'mp4' and quality and quality.endswith('p'):
            try:
                target_height = int(quality[:-1])
            except:
                pass

        best_video = None
        best_video_height = -1
        for f in adaptive + formats:
            if f.get('url') and f.get('qualityLabel'):
                height_str = f['qualityLabel'].replace('p', '')
                if height_str.isdigit():
                    height = int(height_str)
                    if (target_height is None or height <= target_height) and height > best_video_height:
                        best_video = f
                        best_video_height = height

        best_audio = None
        for f in adaptive + formats:
            if f.get('url') and f.get('audioQuality'):
                mime = f.get('mimeType', '')
                if 'mp4a' in mime:
                    best_audio = f
                    break
                elif not best_audio:
                    best_audio = f

        if best_video and best_audio:
            v_url = best_video['url']
            a_url = best_audio['url']
            v_ext = re.search(r'/(\w+)', best_video.get('mimeType', '')).group(1) if 'mimeType' in best_video else 'mp4'
            a_ext = re.search(r'/(\w+)', best_audio.get('mimeType', '')).group(1) if 'mimeType' in best_audio else 'm4a'
            title = "download"
            try:
                video_details = next(find_keys(data, 'videoDetails'), {})
                if video_details:
                    title = video_details.get('title', 'download')
            except:
                pass
            host = request.host
            protocol = 'https' if request.is_secure else 'http'
            proxy_video = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(v_url)}&ext={v_ext}&title={urllib.parse.quote(title + '_video')}"
            proxy_audio = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(a_url)}&ext={a_ext}&title={urllib.parse.quote(title + '_audio')}"
            return jsonify({
                "success": True,
                "title": title,
                "type": "split",
                "videoExt": v_ext,
                "audioExt": a_ext,
                "quality": best_video_height if best_video_height > 0 else None,
                "downloadUrl": proxy_video,
                "audioUrl": proxy_audio
            })

        if best_video:
            v_url = best_video['url']
            v_ext = re.search(r'/(\w+)', best_video.get('mimeType', '')).group(1) if 'mimeType' in best_video else 'mp4'
            title = "download"
            try:
                video_details = next(find_keys(data, 'videoDetails'), {})
                if video_details:
                    title = video_details.get('title', 'download')
            except:
                pass
            host = request.host
            protocol = 'https' if request.is_secure else 'http'
            proxy_url = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(v_url)}&ext={v_ext}&title={urllib.parse.quote(title)}"
            return jsonify({
                "success": True,
                "title": title,
                "type": "mp4",
                "quality": best_video_height if best_video_height > 0 else None,
                "downloadUrl": proxy_url
            })

        if type_param == 'mp3' and best_audio:
            a_url = best_audio['url']
            a_ext = re.search(r'/(\w+)', best_audio.get('mimeType', '')).group(1) if 'mimeType' in best_audio else 'm4a'
            title = "download"
            try:
                video_details = next(find_keys(data, 'videoDetails'), {})
                if video_details:
                    title = video_details.get('title', 'download')
            except:
                pass
            host = request.host
            protocol = 'https' if request.is_secure else 'http'
            proxy_url = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(a_url)}&ext={a_ext}&title={urllib.parse.quote(title + '_audio')}"
            return jsonify({
                "success": True,
                "title": title,
                "type": "audio",
                "quality": None,
                "downloadUrl": proxy_url
            })

    except Exception:
        pass

    # --- 2) FALLBACK: YT-DLP COM SELETOR PRIORIZANDO QUALIDADE ---
    if type_param == 'mp3':
        format_selector = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio"
    else:
        quality_num = quality.replace('p', '') if quality else None
        height_filter = f"[height<={quality_num}]" if quality_num else ""
        # Prioriza qualidade máxima, depois MP4, depois qualquer
        format_selector = (
            f"bestvideo{height_filter}+bestaudio/best{height_filter}/"
            f"bestvideo[ext=mp4]{height_filter}+bestaudio[ext=m4a]/"
            f"best[ext=mp4]{height_filter}/"
            "best"
        )

    tentativas = [
        {**YDL_OPTS_BASE, 'format': format_selector},
        {**YDL_OPTS_BASE, 'format': format_selector, 'cookiefile': None},
        {**YDL_OPTS_BASE, 'format': format_selector, 'extractor_args': {'youtube': {'player_client': ['android']}}},
        {**YDL_OPTS_BASE, 'format': format_selector, 'cookiefile': None, 'extractor_args': {'youtube': {'player_client': ['android']}}},
        {**YDL_OPTS_BASE, 'format': 'best'},  # último recurso
    ]

    last_error = None
    for opts in tentativas:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info_dict = ydl.extract_info(watch_url, download=False)
            requested_formats = info_dict.get('requested_formats')
            video_title = info_dict.get('title', 'download')
            host = request.host
            protocol = 'https' if request.is_secure else 'http'

            if requested_formats and len(requested_formats) >= 2:
                v_url = requested_formats[0].get('url')
                v_ext = requested_formats[0].get('ext', 'mp4')
                a_url = requested_formats[1].get('url')
                a_ext = requested_formats[1].get('ext', 'm4a')
                quality_val = requested_formats[0].get('height')
                proxy_video = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(v_url)}&ext={v_ext}&title={urllib.parse.quote(video_title + '_video')}"
                proxy_audio = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(a_url)}&ext={a_ext}&title={urllib.parse.quote(video_title + '_audio')}"
                return jsonify({
                    "success": True,
                    "title": video_title,
                    "type": "split",
                    "videoExt": v_ext,
                    "audioExt": a_ext,
                    "quality": quality_val,
                    "downloadUrl": proxy_video,
                    "audioUrl": proxy_audio
                })

            direct_url = info_dict.get('url')
            direct_ext = info_dict.get('ext', type_param)
            quality_val = info_dict.get('height') or info_dict.get('format_note')
            proxy_url = f"{protocol}://{host}/download-stream?url={urllib.parse.quote(direct_url)}&ext={direct_ext}&title={urllib.parse.quote(video_title)}"
            return jsonify({
                "success": True,
                "title": video_title,
                "type": direct_ext,
                "quality": quality_val,
                "downloadUrl": proxy_url
            })

        except Exception as e:
            last_error = e
            continue

    return jsonify({"success": False, "error": "Qualidade não disponível.", "details": str(last_error)}), 400

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
