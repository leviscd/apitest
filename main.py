from flask import Flask, request, jsonify, Response
import yt_dlp
import innertube
import urllib.parse
import re
import requests

app = Flask(__name__)

# Configura o Flask para aceitar Emojis e acentos nativos sem escapar para ASCII
app.config['JSON_AS_ASCII'] = False
app.json.ensure_ascii = False


@app.after_request
def force_utf8_charset(response):
    # Sem isso, o header vem só "Content-Type: application/json" (sem charset).
    # O corpo já está em UTF-8 correto, mas alguns clientes (curl antigo, certas
    # libs HTTP) assumem Latin-1/ISO-8859-1 quando o charset não vem explícito,
    # e é isso que produz o "parÃ¢metro" em vez de "parâmetro" na exibição.
    if response.mimetype == 'application/json':
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
    return response

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

# ============================================================================
# INNERTUBE — API interna do YouTube (a mesma que o app/site usam), acessada
# via pacote innertube. Usada nas rotas /search e /info no lugar de
# youtube-search-python (que estava corrompendo ç/acentos/emojis) e do
# yt-dlp (mais lento pra só listar qualidades). Os clientes ficam abertos
# em memória (instanciados uma vez, no carregamento do módulo) pra resposta
# em milissegundos, sem reconectar a cada request.
# ============================================================================

client_yt = innertube.InnerTube("WEB")          # melhor pra busca (mais completo + paginação)
client_yt_android = innertube.InnerTube("ANDROID")  # formatos com URL direta, sem "signatureCipher"


def find_keys(obj, key_name):
    """Varre recursivamente a resposta do innertube procurando por uma chave,
    em vez de depender de um caminho fixo (o Google muda a estrutura com
    frequência, então isso deixa o parser bem mais resistente a mudanças)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key_name:
                yield v
            yield from find_keys(v, key_name)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_keys(item, key_name)


def extract_video_id(text):
    """Extrai o ID de 11 caracteres de qualquer formato de link do YouTube."""
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
    """Converte um videoRenderer (item de resultado de busca) pro formato de saída da API."""
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
    """Converte o videoDetails (retornado pela rota 'player') no mesmo formato do /search."""
    video_id = video_details.get('videoId')
    return {
        "title": video_details.get('title'),
        "channel": video_details.get('author'),
        "duration": format_duration(video_details.get('lengthSeconds')),
        "thumbnail": best_thumbnail(video_details.get('thumbnail', {}).get('thumbnails', []), video_id),
        "url": f"https://www.youtube.com/watch?v={video_id}"
    }


def get_player_data(video_id):
    """Chama client.player() do innertube. Tenta ANDROID primeiro (URLs diretas),
    e cai pro WEB se o vídeo não vier disponível nesse cliente."""
    for client in (client_yt_android, client_yt):
        try:
            data = client.player(video_id)
        except Exception:
            continue
        video_details = next(find_keys(data, 'videoDetails'), None)
        if video_details:
            return data, video_details
    return None, None


def yt_dlp_qualities_fallback(video_url):
    """Fallback pra quando o Innertube não devolve NENHUMA qualidade com URL
    direta. Isso acontece porque, hoje em dia, o YouTube exige um PO Token
    (proof-of-origin) pra maioria dos formatos — sem ele, os formatos vêm com
    'signatureCipher' (URL criptografada) em vez de 'url'. O yt-dlp tem lógica
    própria mais robusta pra contornar isso, então é usado como plano B.
    É bem mais lento que o Innertube (por isso não é o caminho padrão)."""
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'skip_download': True}) as ydl:
        info_dict = ydl.extract_info(video_url, download=False)

    qualities = []
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

    base_info = {
        "title": info_dict.get('title'),
        "channel": info_dict.get('uploader'),
        "duration": format_duration(info_dict.get('duration')),
        "thumbnail": info_dict.get('thumbnail'),
        "url": info_dict.get('webpage_url') or video_url
    }
    return base_info, qualities


MAX_SEARCH_RESULTS = 200   # teto de segurança pra não deixar a resposta gigante/lenta
MAX_SEARCH_PAGES = 10      # cada página de continuation costuma trazer ~20 vídeos


def innertube_search_all(query, max_pages=1):
    """Busca via innertube. Por padrão faz UMA chamada só (max_pages=1), que é o
    que deixa a rota instantânea — igual client_yt.search(query) sozinho.
    Cada página extra (continuation) exige uma nova requisição sequencial ao
    YouTube (o token da página N só existe depois da resposta da página N-1,
    então não dá pra paralelizar), então passar max_pages > 1 troca velocidade
    por mais resultados."""
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


# 🌐 ROTA 1: Busca Instantânea — Innertube (API interna do YouTube)
# Por padrão retorna só a 1ª página (~20 resultados, resposta em milissegundos).
# Pra trazer mais, passe ?pages=N (N até 10) — cada página a mais soma uma
# requisição sequencial extra ao YouTube, então a resposta fica mais lenta.
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



# 🌐 ROTA 2: Informações de Qualidade — Innertube (rápido) com fallback pro
# yt-dlp (mais lento, porém confiável) quando o YouTube exige PO Token e o
# Innertube não devolve nenhum formato com URL direta.
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

    # 1) Tentativa rápida via Innertube
    try:
        data, video_details = get_player_data(video_id)
        if video_details:
            base_info = parse_video_details(video_details)

            streaming_data = next(find_keys(data, 'streamingData'), {}) or {}
            raw_formats = streaming_data.get('formats', []) + streaming_data.get('adaptiveFormats', [])

            for f in raw_formats:
                if not f.get('url'):
                    # "signatureCipher" — precisa de PO Token/decifra pra virar URL
                    # utilizável. Fica de fora daqui; se sobrar nada, cai no yt-dlp.
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
        pass  # segue pro fallback abaixo

    # 2) Se o Innertube não trouxe NENHUMA qualidade usável (comum hoje em dia
    # por causa do PO Token), cai pro yt-dlp — mais lento, porém mais confiável.
    if not qualities:
        try:
            fallback_base, qualities = yt_dlp_qualities_fallback(watch_url)
            source = "yt-dlp"
            if not base_info:
                base_info = fallback_base
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
~/yt2dl $ 
