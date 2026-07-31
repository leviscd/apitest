const express = require('express');
const crypto = require('crypto');
const dotenv = require('dotenv');
const fs = require('fs');
const path = require('path');
const { Innertube, UniversalCache, Platform, Constants } = require('youtubei.js');
const { BG } = require('bgutils-js');
const { JSDOM } = require('jsdom');
const { request: undiciRequest, Agent, setGlobalDispatcher } = require('undici'); // npm install undici

// v28: força IPv4 em TODA requisição de rede feita por este processo
// (isso inclui as chamadas internas da youtubei.js, que usam o fetch
// global do Node — no Node moderno, isso é undici por baixo). Ver
// changelog v28 no topo: suspeita de que endereços IPv6 temporários
// (privacy extensions, comuns em rede móvel) mudavam entre a conexão
// que resolvia a URL e a conexão que baixava de verdade, fazendo o
// `ip=` embutido na URL não bater mais na segunda ponta do CDN.
setGlobalDispatcher(new Agent({ connect: { family: 4 } }));

dotenv.config();

// ===== INTERPRETADOR JS PARA DECIFRAR URLs =====
Platform.shim.eval = async (data) => {
  return new Function(data.output)();
};

const app = express();
app.use(express.json());

// ===== CONFIGURAÇÕES =====
const PORT = process.env.PORT || 3000;
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'admin123';
const TOKEN_EXPIRY_DAYS = 30;
const TOKENS_FILE = path.join(__dirname, 'tokens.json');
const MASTER_TOKEN = process.env.MASTER_TOKEN || 'test';

// Opcional: cookie de uma conta Google real, já logada (exporte do seu
// próprio navegador com uma extensão tipo "Get cookies.txt LOCALLY" e
// cole o header Cookie inteiro aqui). NÃO é obrigatório pro código
// rodar, mas sem isso o po_token sozinho está deixando de ser
// suficiente pra passar no bot-check em boa parte dos vídeos — ver
// aviso oficial do bgutil-ytdlp-pot-provider e o wiki do yt-dlp sobre
// PO Tokens. Só é aplicado na sessão `yt` (WEB/MWEB); ANDROID/IOS/TV
// não usam cookie de plataforma Web.
const YT_COOKIE = process.env.YT_COOKIE || null;

// ===== PERSISTÊNCIA DE TOKENS (de acesso à SUA api, não confundir com po_token) =====
function loadTokens() {
  try {
    if (fs.existsSync(TOKENS_FILE)) return JSON.parse(fs.readFileSync(TOKENS_FILE, 'utf8'));
    return {};
  } catch (e) { return {}; }
}
function saveTokens(tokens) {
  try { fs.writeFileSync(TOKENS_FILE, JSON.stringify(tokens, null, 2), 'utf8'); } catch (e) {}
}
let tokens = loadTokens();

// ===== SESSÕES INNERTUBE =====
// `yt`     -> COM po_token, só pra clients de plataforma Web (WEB, MWEB)
// `ytNoPo` -> SEM po_token nenhum, pra ANDROID/IOS/TV (evita contaminar
//             esses clients com um token de plataforma errada)
let yt;
let ytNoPo;
const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo';

// Token de sessão (identifier = visitorData) — usado no Innertube.create
// da sessão `yt` e como ÚLTIMO fallback caso a geração do token vinculado
// ao vídeo falhe.
let currentPoToken = null;
let currentVisitorData = null;

// Resolve o desafio do BotGuard pra um identifier arbitrário e devolve
// { poToken, challenge } — challenge fica disponível caso precise.
async function solveBotGuardAndMint(identifier) {
  const bgConfig = {
    fetch: (input, init) => fetch(input, init),
    globalObj: globalThis,
    identifier,
    requestKey: REQUEST_KEY,
  };

  const challenge = await BG.Challenge.create(bgConfig);
  if (!challenge) throw new Error('Não foi possível obter o desafio do BotGuard');

  const interpreterJavascript = challenge.interpreterJavascript?.privateDoNotAccessOrElseSafeScriptWrappedValue;
  if (interpreterJavascript) {
    new Function(interpreterJavascript)();
  } else {
    throw new Error('Não foi possível carregar o BotGuard');
  }

  const poTokenResult = await BG.PoToken.generate({
    program: challenge.program,
    globalName: challenge.globalName,
    bgConfig,
  });

  return poTokenResult.poToken;
}

async function generatePoTokenForSession() {
  const dom = new JSDOM();
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
  });

  const tempInnertube = await Innertube.create({ retrieve_player: false });
  const visitorData = tempInnertube.session.context.client.visitorData;
  if (!visitorData) throw new Error('Não foi possível obter o visitorData');

  const poToken = await solveBotGuardAndMint(visitorData);
  return { poToken, visitorData };
}

async function initInnertube() {
  const { poToken, visitorData } = await generatePoTokenForSession();

  currentPoToken = poToken;
  currentVisitorData = visitorData;

  const useCookie = !!YT_COOKIE;

  yt = await Innertube.create({
    cache: new UniversalCache(false),
    // Quando há YT_COOKIE (sessão logada de verdade), NÃO forçamos
    // visitor_data/po_token de uma sessão anônima por cima dela. Uma
    // requisição autenticada (Cookie + Authorization: SAPISIDHASH)
    // misturada com um visitorData/po_token minerado anonimamente é
    // um sinal contraditório pro YouTube — "sou a conta X" e "sou um
    // visitante anônimo Y" ao mesmo tempo. Sem cookie, mantemos como
    // antes: é o único sinal de confiança que temos pra oferecer.
    ...(useCookie
      ? { cookie: YT_COOKIE }
      : { po_token: poToken, visitor_data: visitorData }),
  });

  // Sessão paralela SEM po_token, pra ANDROID/IOS/TV — um po_token de
  // Web ativamente atrapalha esses clients (confirmado no código do
  // Invidious), então é melhor não passar nenhum pra eles.
  ytNoPo = await Innertube.create({
    cache: new UniversalCache(false),
  });

  console.log(
    useCookie
      ? `✅ Sessão Innertube inicializada (yt AUTENTICADA via cookie, prefixo "${YT_COOKIE.slice(0, 15)}...", sem po_token/visitor_data anônimo forçado | ytNoPo sem PO Token)`
      : '✅ Sessão Innertube inicializada (yt com PO Token anônimo — sem YT_COOKIE, LOGIN_REQUIRED tende a aparecer em parte dos vídeos | ytNoPo sem PO Token)'
  );
}

const PO_TOKEN_REFRESH_MS = 60 * 60 * 1000; // 1h
function schedulePoTokenRefresh() {
  setInterval(async () => {
    try {
      await initInnertube();
      console.log('🔄 PO Token renovado');
    } catch (err) {
      console.error('⚠️ Falha ao renovar PO Token, mantendo sessão atual:', err.message);
    }
  }, PO_TOKEN_REFRESH_MS);
}

// ============================================================
// Token de mídia (GVS) vinculado ao ID DO VÍDEO (não à sessão)
// ============================================================
// Cache curto por vídeo — barato de recalcular, evita resolver o
// desafio do BotGuard de novo a cada request repetido pro mesmo
// vídeo dentro da janela de cache de resolução (RESOLVED_URL_TTL).
const videoPoTokenCache = new Map();
const VIDEO_POTOKEN_TTL = 10 * 60 * 1000; // 10 min — janela conservadora

async function generateContentBoundPoToken(videoId) {
  const cached = videoPoTokenCache.get(videoId);
  if (cached && (Date.now() - cached.timestamp) < VIDEO_POTOKEN_TTL) return cached.poToken;

  const poToken = await solveBotGuardAndMint(videoId);
  videoPoTokenCache.set(videoId, { poToken, timestamp: Date.now() });
  return poToken;
}

// ============================================================
// Qual sessão/estratégia de po_token usar por client
// ============================================================
// WEB/MWEB: plataforma Web -> usam a sessão COM po_token, e o
//           `pot` da URL de mídia tenta ser vinculado ao vídeo.
// ANDROID/IOS/TV: sessão SEM po_token nenhum — não temos
//           DroidGuard/iOSGuard implementado, então é melhor não
//           mandar um token de plataforma errada.
const CLIENT_USES_WEB_POTOKEN = { MWEB: true, WEB: true, IOS: false, ANDROID: false, TV: false };
// Substitua a linha antiga por esta:
const CLIENT_FALLBACK_ORDER = ['MWEB', 'WEB'];
function sessionForClient(client) {
  return CLIENT_USES_WEB_POTOKEN[client] ? yt : ytNoPo;
}

// Verifica playability_status.status/.reason (disponível logo após
// getBasicInfo, independente do log de parser "PlayerErrorCommand not
// found" ter aparecido ou não — esse log é sobre um nó dentro de
// error_screen, não impede status/reason de virem preenchidos).
// Retorna null se tocável (status OK), ou uma string descritiva do
// bloqueio real caso contrário.
function describePlayabilityIssue(info) {
  const status = info?.playability_status?.status;
  if (!status || status === 'OK') return null;

  const reason = info?.playability_status?.reason || '(sem motivo informado pela resposta)';
  let hint = '';
  if (status === 'LOGIN_REQUIRED' && !YT_COOKIE) {
    hint = ' [defina YT_COOKIE no .env com o cookie de uma conta Google logada — po_token sozinho não é mais suficiente pra isso na maioria dos casos]';
  }
  return `bloqueado pelo YouTube (${status}): ${reason}${hint}`;
}

function hasPlayableFormat(info) {
  const all = [
    ...(info.streaming_data?.formats || []),
    ...(info.streaming_data?.adaptive_formats || []),
  ];
  return all.some(f => f.url || f.signature_cipher);
}

// Cada tentativa é isolada e rotulada com o client, pra manter
// mensagens de erro úteis mesmo quando todas falham (AggregateError
// do Promise.any por padrão não é legível).
function withClientLabel(promise, client) {
  return promise.catch(err => {
    throw new Error(`${client}: ${err.message}`);
  });
}

async function tryGetPlayableInfo(videoId, client) {
  const session = sessionForClient(client);
  const info = await session.getBasicInfo(videoId, { client });

  const issue = describePlayabilityIssue(info);
  if (issue) throw new Error(issue);

  if (!hasPlayableFormat(info)) {
    throw new Error('SABR-only, sem URL/cifra decifrável');
  }
  return info;
}

// Usado pela rota /info. Dispara todos os clients em paralelo, usa
// a primeira que responder com formato utilizável.
async function getPlayableInfo(videoId) {
  const attempts = CLIENT_FALLBACK_ORDER.map(client =>
    withClientLabel(tryGetPlayableInfo(videoId, client), client).then(info => ({ info, clientUsed: client }))
  );
  try {
    return await Promise.any(attempts);
  } catch (aggErr) {
    const details = (aggErr.errors || [aggErr]).map(e => e.message).join(' | ');
    throw new Error(details || 'Nenhum client retornou formato decifrável para este vídeo');
  }
}

// ===== CACHE EM MEMÓRIA (troque por Redis se escalar horizontalmente) =====
const infoCache = new Map();
const CACHE_TTL = 60 * 1000; // 60s (metadados)
const RESOLVED_URL_TTL = 20 * 1000; // 20s — só pra deduplicar cliques duplos/retries rápidos do
                                     // app. NÃO é mais um cache de "economizar resolução"; ver
                                     // changelog v27 sobre URLs possivelmente de uso único/curta
                                     // duração. Cachear por horas aqui = entregar link morto.

function getCached(key) {
  const hit = infoCache.get(key);
  if (hit && (Date.now() - hit.timestamp) < hit.ttl) return hit.data;
  if (hit) infoCache.delete(key);
  return null;
}
function setCached(key, data, ttl = CACHE_TTL) {
  infoCache.set(key, { data, timestamp: Date.now(), ttl });
}

// ===== AUXILIARES =====
function isYouTubeUrl(url) {
  return typeof url === 'string' && (url.includes('youtube.com') || url.includes('youtu.be'));
}

function extractVideoId(url) {
  try {
    const u = new URL(url);
    if (u.hostname.includes('youtu.be')) return u.pathname.slice(1);
    if (u.searchParams.get('v')) return u.searchParams.get('v');
    const shorts = u.pathname.match(/\/shorts\/([^/?]+)/);
    if (shorts) return shorts[1];
    return null;
  } catch (e) { return null; }
}

function sanitizeFilename(title, fallback) {
  const base = (title || fallback || 'video')
    .replace(/[\\/:*?"<>|]/g, '')
    .replace(/[\r\n]+/g, ' ')
    .trim()
    .slice(0, 150);
  return base.length ? base : (fallback || 'video');
}

// ===== MIDDLEWARE DE AUTENTICAÇÃO =====
function authMiddleware(req, res, next) {
  let token = req.query.token || req.headers.authorization?.split(' ')[1];
  if (!token) return res.status(401).json({ error: 'Token obrigatório' });
  if (token === MASTER_TOKEN) {
    req.isAdmin = true;
    return next();
  }
  const tokenData = tokens[token];
  if (!tokenData || !tokenData.active || new Date() > new Date(tokenData.expiresAt)) {
    return res.status(401).json({ error: 'Token inválido ou expirado' });
  }
  req.isAdmin = false;
  next();
}

// ===== ROTAS ADMINISTRATIVAS =====
app.get('/api/tokens/list', authMiddleware, (req, res) => {
  if (!req.isAdmin) return res.status(403).json({ error: 'Acesso negado' });
  res.json({ status: 'sucesso', total_tokens: Object.keys(tokens).length, tokens });
});

app.get('/api/token', (req, res) => {
  if (req.query.adminPassword !== ADMIN_PASSWORD) return res.status(403).json({ error: 'Senha incorreta' });
  const token = crypto.randomBytes(16).toString('hex');
  const expiresAt = new Date(Date.now() + TOKEN_EXPIRY_DAYS * 24 * 60 * 60 * 1000);
  tokens[token] = { createdAt: new Date().toISOString(), expiresAt: expiresAt.toISOString(), active: true };
  saveTokens(tokens);
  res.json({ token, expiresAt: expiresAt.toISOString() });
});

// ============================================================
// QUALIDADES DE VÍDEO DISPONÍVEIS (pra /info)
// ============================================================
function normalizeQualityLabel(label) {
  if (!label) return null;
  const match = String(label).match(/(\d+)/);
  return match ? `${match[1]}p` : null;
}

function heightFromQualityLabel(label) {
  const match = String(label || '').match(/(\d+)/);
  return match ? parseInt(match[1], 10) : 0;
}

function extractAvailableVideoQualities(info) {
  const all = [
    ...(info.streaming_data?.formats || []),
    ...(info.streaming_data?.adaptive_formats || []),
  ];
  const heights = new Map();

  for (const f of all) {
    if (!f.has_video) continue;
    const normalized = normalizeQualityLabel(f.quality_label || f.quality);
    if (!normalized) continue;
    const h = heightFromQualityLabel(normalized);
    if (!heights.has(normalized) || heights.get(normalized) < h) {
      heights.set(normalized, h);
    }
  }

  return [...heights.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([label]) => label);
}

// ============================================================
// INFO / METADADOS
// ============================================================
app.get('/api/youtube/info', authMiddleware, async (req, res) => {
  const { url } = req.query;
  if (!url || !isYouTubeUrl(url)) return res.status(400).json({ error: 'URL do YouTube inválida' });

  const videoId = extractVideoId(url);
  if (!videoId) return res.status(400).json({ error: 'Não foi possível extrair o videoId' });

  const cacheKey = `info:${videoId}`;
  const cached = getCached(cacheKey);
  if (cached) return res.json({ status: 'sucesso', plataforma: 'youtube', cache: true, ...cached });

  try {
    const { info, clientUsed } = await getPlayableInfo(videoId);

    const result = {
      videoId,
      clientUsado: clientUsed,
      title: info.basic_info.title,
      thumbnail: info.basic_info.thumbnail?.pop()?.url || '',
      channel: info.basic_info.channel?.name || 'Desconhecido',
      duration: info.basic_info.duration || 0,
      views: info.basic_info.view_count || 0,
      qualities: extractAvailableVideoQualities(info),
    };

    setCached(cacheKey, result);
    res.json({ status: 'sucesso', plataforma: 'youtube', cache: false, ...result });
  } catch (error) {
    res.status(500).json({ error: 'Falha ao obter informações', detalhes: error.message });
  }
});

// ============================================================
// BUSCA
// ============================================================
app.get('/api/youtube/search', authMiddleware, async (req, res) => {
  const { q } = req.query;
  if (!q) return res.status(400).json({ error: 'Termo de busca ausente' });

  const cacheKey = `search:${q}`;
  const cached = getCached(cacheKey);
  if (cached) return res.json(cached);

  try {
    const results = await yt.search(q, { type: 'video' });
    const videos = (results.videos || []).slice(0, 30).map(v => ({
      videoId: v.video_id,
      title: v.title?.text || '',
      thumbnail: v.thumbnails?.[0]?.url || '',
      views: v.view_count?.text || '',
      channel: v.author?.name || '',
      duration: v.duration?.text || '',
      url: `https://www.youtube.com/watch?v=${v.video_id}`,
    }));

    setCached(cacheKey, videos);
    res.json(videos);
  } catch (e) {
    res.status(500).json({ error: 'Falha na busca', detalhes: e.message });
  }
});

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

// ============================================================
// DOWNLOAD — redirect direto (só type=mp3)
// ============================================================
app.get('/api/youtube/download', authMiddleware, async (req, res) => {
  const { url, type, quality } = req.query;
  if (!url || !type || !isYouTubeUrl(url)) return res.status(400).json({ error: 'Parâmetros inválidos' });

  if (type === 'video') {
    return res.status(400).json({
      error: 'type=video agora devolve duas trilhas (vídeo-only + áudio-only). Use /resolve e baixe/mux as duas no app.',
    });
  }

  const videoId = extractVideoId(url);
  if (!videoId) return res.status(400).json({ error: 'Não foi possível extrair o videoId' });

  try {
    const resolved = await resolvePlayableFormatUrl(videoId, type, quality);
    console.log(`✅ Redirecionando (${type}, itag ${resolved.audio.format.itag}, client ${resolved.clientUsed}): ${videoId}`);
    return res.redirect(resolved.audio.url);
  } catch (err) {
    return res.status(502).json({ error: 'Falha ao resolver URL de download válida', details: err.message });
  }
});

// ============================================================
// DOWNLOAD/FILE — proxy com nome forçado (só type=mp3)
// ============================================================
app.get('/api/youtube/download/file', authMiddleware, async (req, res) => {
  const { url, type, quality } = req.query;
  if (!url || !type || !isYouTubeUrl(url)) return res.status(400).json({ error: 'Parâmetros inválidos' });

  if (type === 'video') {
    return res.status(400).json({
      error: 'type=video agora devolve duas trilhas (vídeo-only + áudio-only). Use /resolve e baixe/mux as duas no app.',
    });
  }

  const videoId = extractVideoId(url);
  if (!videoId) return res.status(400).json({ error: 'Não foi possível extrair o videoId' });

  try {
    const resolved = await resolvePlayableFormatUrl(videoId, type, quality);
    const { filename, asciiFallback } = buildFilename(resolved.title, videoId, 'mp3');

    const upstream = await undiciRequest(resolved.audio.url, { method: 'GET', headers: Constants.STREAM_HEADERS });
    if (upstream.statusCode < 200 || upstream.statusCode >= 300) {
      return res.status(502).json({ error: 'Falha ao abrir stream de origem', status: upstream.statusCode });
    }

    res.setHeader('Content-Type', upstream.headers['content-type'] || 'audio/mp4');
    res.setHeader(
      'Content-Disposition',
      `attachment; filename="${asciiFallback}"; filename*=UTF-8''${encodeURIComponent(filename)}`
    );
    const contentLength = upstream.headers['content-length'];
    if (contentLength) res.setHeader('Content-Length', contentLength);

    console.log(`✅ Enviando (proxy) "${filename}" (${type}, itag ${resolved.audio.format.itag}, client ${resolved.clientUsed}): ${videoId}`);

    upstream.body.pipe(res);
  } catch (err) {
    return res.status(502).json({ error: 'Falha ao resolver URL de download válida', details: err.message });
  }
});

// ===== HEALTH CHECK =====
app.get('/health', (req, res) => res.send('OK'));

// ===== INICIALIZAÇÃO =====
initInnertube()
  .then(() => {
    schedulePoTokenRefresh();
    app.listen(PORT, () => {
      console.log(`🚀 API v28 (YouTube-only, youtubei.js) rodando na porta ${PORT}`);
      console.log(`⚡ Resolução de client em paralelo + dual-track vídeo/áudio para mux no app`);
      console.log(`🔁 Clients: ${CLIENT_FALLBACK_ORDER.join(' + ')} (competindo, não mais em fila)`);
      console.log(`🔐 WEB/MWEB: sessão com po_token + pot vinculado ao vídeo | ANDROID/IOS/TV: sessão sem po_token`);
      console.log(`🩺 Erros expõem playability_status real (ex.: LOGIN_REQUIRED)`);
      console.log(`🔧 v26: /resolve devolve "headers" no JSON`);
      console.log(`🔧 v27: removida a validação por fetch da URL de mídia + cache de /resolve caiu pra 20s`);
      console.log(`🔧 v28: forçado IPv4 (undici Agent family:4) em toda requisição do processo — suspeita de IPv6 instável causando 403 na segunda ponta do CDN`);
    });
  })
  .catch(err => {
    console.error('❌ Falha ao inicializar Innertube:', err);
    process.exit(1);
  });
~/onetap-api $ 
