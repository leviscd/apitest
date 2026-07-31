//this is my route:

// ============================================================
// DOWNLOAD — seleção de formatos
// ============================================================
function getAdaptiveVideoFormats(info) {
  return (info.streaming_data?.adaptive_formats || []).filter(f => f.has_video && !f.has_audio);
}

function pickVideoFormatByQuality(info, quality) {
  const withHeight = getAdaptiveVideoFormats(info)
    .map(f => ({ f, height: heightFromQualityLabel(f.quality_label || f.quality) }))
    .filter(x => x.height > 0)
    .sort((a, b) => b.height - a.height);

  if (!withHeight.length) return null;
  if (!quality || quality === 'best') return withHeight[0].f;
  if (quality === 'worst') return withHeight[withHeight.length - 1].f;

  const wanted = heightFromQualityLabel(quality);
  if (!wanted) return withHeight[0].f;

  const exact = withHeight.find(x => x.height === wanted);
  if (exact) return exact.f;

  const below = withHeight.filter(x => x.height < wanted);
  if (below.length) return below[0].f;

  return withHeight[withHeight.length - 1].f;
}

function chooseFormatForRequest(info, type, quality) {
  let audioFormat;
  try {
    audioFormat = info.chooseFormat({ type: 'audio', quality: 'best' });
  } catch (e) { /* nada */ }

  if (type === 'mp3') {
    return { video: null, audio: audioFormat || null };
  }

  const videoFormat = pickVideoFormatByQuality(info, quality);
  return { video: videoFormat || null, audio: audioFormat || null };
}

// ============================================================
// FIX DE THROTTLING (INTOCADO)
// ============================================================
function withFullRange(mediaUrl) {
  try {
    const u = new URL(mediaUrl);
    if (u.searchParams.has('range')) return mediaUrl;
    const clen = u.searchParams.get('clen');
    if (clen) u.searchParams.set('range', `0-${clen}`);
    return u.toString();
  } catch (e) {
    return mediaUrl;
  }
}

// Cola o `pot` na URL, se um token foi passado. Se poToken for null
// (ex.: clients ANDROID/IOS/TV, que não usam po_token de Web), a URL
// sai igual entrou.
function withPoToken(mediaUrl, poToken) {
  if (!poToken) return mediaUrl;
  try {
    const u = new URL(mediaUrl);
    if (u.searchParams.has('pot')) return mediaUrl;
    u.searchParams.set('pot', poToken);
    return u.toString();
  } catch (e) {
    return mediaUrl;
  }
}

// Decifra e aplica withFullRange + withPoToken (quando aplicável).
//
// v27: NÃO faz mais nenhuma requisição de rede pra "validar" a URL.
// Ver changelog v27 no topo do arquivo — o GET de validação que
// existia aqui era o suspeito principal de estar consumindo o uso
// único/janela curta da URL antes do app conseguir baixar de verdade.
async function resolveAndValidateFormat(format, player, poToken) {
  const rawUrl = format.url || await format.decipher(player);
  let url = withFullRange(rawUrl);
  url = withPoToken(url, poToken);
  return { url, format };
}

// Tentativa isolada de UM client — agora escolhe a SESSÃO certa
// (com ou sem po_token) e, quando aplicável, gera o token vinculado
// ao vídeo específico pra usar no `pot` da URL de mídia.
async function tryResolveWithClient(videoId, client, type, quality) {
  const session = sessionForClient(client);
  const info = await session.getBasicInfo(videoId, { client });

  const issue = describePlayabilityIssue(info);
  if (issue) throw new Error(issue);

  if (!hasPlayableFormat(info)) throw new Error('SABR-only, sem URL/cifra decifrável');

  const { video, audio } = chooseFormatForRequest(info, type, quality);

  // Só tenta o token vinculado ao vídeo pros clients de plataforma Web.
  // Pra ANDROID/IOS/TV, poToken fica null (withPoToken não mexe na URL).
  let poTokenForMedia = null;
  if (CLIENT_USES_WEB_POTOKEN[client]) {
    try {
      poTokenForMedia = await generateContentBoundPoToken(videoId);
    } catch (e) {
      poTokenForMedia = currentPoToken; // fallback: token de sessão
    }
  }

  if (type === 'mp3') {
    if (!audio) throw new Error('nenhum formato de áudio compatível com o pedido');
    const resolvedAudio = await resolveAndValidateFormat(audio, session.session.player, poTokenForMedia);
    return { type: 'mp3', audio: resolvedAudio, clientUsed: client, title: info.basic_info?.title };
  }

  if (!video) throw new Error('nenhum formato de vídeo compatível com o pedido');
  if (!audio) throw new Error('nenhum formato de áudio compatível com o pedido');

  const [resolvedVideo, resolvedAudio] = await Promise.all([
    resolveAndValidateFormat(video, session.session.player, poTokenForMedia),
    resolveAndValidateFormat(audio, session.session.player, poTokenForMedia),
  ]);

  return { type: 'video', video: resolvedVideo, audio: resolvedAudio, clientUsed: client, title: info.basic_info?.title };
}

async function resolvePlayableFormatUrl(videoId, type, quality) {
  const cacheKey = `resolved:${videoId}:${type}:${quality || 'default'}`;
  const cached = getCached(cacheKey);
  if (cached) return cached;

  const attempts = CLIENT_FALLBACK_ORDER.map(client =>
    withClientLabel(tryResolveWithClient(videoId, client, type, quality), client)
  );

  let result;
  try {
    result = await Promise.any(attempts);
  } catch (aggErr) {
    const details = (aggErr.errors || [aggErr]).map(e => e.message).join(' | ');
    throw new Error(details || 'Nenhum client conseguiu resolver uma URL de mídia válida');
  }

  setCached(cacheKey, result, RESOLVED_URL_TTL);
  return result;
}

function buildFilename(title, videoId, type) {
  const ext = type === 'mp3' ? 'mp3' : 'mp4';
  const filename = `${sanitizeFilename(title, videoId)}.${ext}`;
  const asciiFallback = filename.replace(/[^\x20-\x7E]/g, '_');
  return { filename, asciiFallback };
}

// ============================================================
// RESOLVE
// ============================================================
app.get('/api/youtube/resolve', authMiddleware, async (req, res) => {
  const { url, type, quality } = req.query;
  if (!url || !type || !isYouTubeUrl(url)) return res.status(400).json({ error: 'Parâmetros inválidos' });

  const videoId = extractVideoId(url);
  if (!videoId) return res.status(400).json({ error: 'Não foi possível extrair o videoId' });

  try {
    const resolved = await resolvePlayableFormatUrl(videoId, type, quality);

    if (resolved.type === 'mp3') {
      const { filename } = buildFilename(resolved.title, videoId, 'mp3');
      return res.json({
        status: 'sucesso',
        videoId,
        title: resolved.title || null,
        filename,
        url: resolved.audio.url,
        itag: resolved.audio.format.itag,
        mime: resolved.audio.format.mime_type,
        client: resolved.clientUsed,
        // v26: headers obrigatórios pra baixar a URL direto no device.
        // Sem isso o googlevideo.com responde 403 (texto puro) pra quem
        // não for o servidor que resolveu/validou a URL.
        headers: Constants.STREAM_HEADERS,
      });
    }

    const { filename } = buildFilename(resolved.title, videoId, 'video');
    return res.json({
      status: 'sucesso',
      videoId,
      title: resolved.title || null,
      filename,
      client: resolved.clientUsed,
      // v26: mesmos headers pra usar no ffmpeg-kit (-headers) ao baixar
      // TANTO a trilha de vídeo quanto a de áudio abaixo.
      headers: Constants.STREAM_HEADERS,
      video: {
        url: resolved.video.url,
        itag: resolved.video.format.itag,
        mime: resolved.video.format.mime_type,
        quality: normalizeQualityLabel(resolved.video.format.quality_label || resolved.video.format.quality) || null,
      },
      audio: {
        url: resolved.audio.url,
        itag: resolved.audio.format.itag,
        mime: resolved.audio.format.mime_type,
      },
    });
  } catch (err) {
    res.status(502).json({ error: 'Falha ao resolver URL de download válida', details: err.message });
  }
});

 = formats.find(f => f.itag === 140); // Standard m4a audio

    return { video, audio };
}

// --- 3. Main Resolve Route ---
app.get('/api/youtube/resolve', async (req, res) => {
    try {
        const { url, type, token } = req.query;
        if (!url) return res.status(400).json({ error: 'Missing URL' });

        // Extract video ID from URL
        const videoId = extractId(url);

        // Fetch metadata explicitly using MWEB to get mobile-friendly streams
        const metadata = await fetchYouTubeMetadata(videoId, 'MWEB', token);
        const { video, audio } = filterFormats(metadata.formats);

        if (!video || !audio) {
            return res.status(404).json({ error: 'Media streams not found' });
        }

        // Return the clean JSON with the raw googlevideo.com URLs
        res.json({
            status: "sucesso",
            videoId: videoId,
            title: metadata.title,
            filename: `${metadata.title}.mp4`,
            client: "MWEB",
            headers: {
                accept: "*/*",
                origin: "[https://www.youtube.com](https://www.youtube.com)",
                referer: "[https://www.youtube.com](https://www.youtube.com)",
                DNT: "?1"
            },
            video: {
                url: video.url,
                itag: video.itag,
                mime: video.mimeType,
                quality: video.qualityLabel || '360p'
            },
            audio: {
                url: audio.url,
                itag: audio.itag,
                mime: audio.mimeType
            }
        });

    } catch (error) {
        console.error('Resolve Error:', error);
        res.status(500).json({ error: error.message });
    }
});
