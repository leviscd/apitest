//this is my route:


const express = require('express');
const app = express();

// --- 1. Helper Function: Get YouTube Video Info & Tokens ---
async function fetchYouTubeMetadata(videoId, clientType = 'MWEB', token = null) {
    // This function handles the communication with YouTube's inner API
    // It passes the PoToken, User-Agent, and extracts the streaming data
    // (Assume this relies on a custom scraper or a library like youtubei.js / ytdl-core)
    
    const requestHeaders = {
        'User-Agent': clientType === 'MWEB' 
            ? 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
            : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': '*/*',
        'Origin': '[https://www.youtube.com](https://www.youtube.com)',
    };

    // Magic happens here: fetching stream formats, deciphering 'n' sig, etc.
    const streamData = await extractStreamData(videoId, requestHeaders, token);
    return streamData;
}

// --- 2. Helper Function: Filter Best Audio and Video Formats ---
function filterFormats(formats) {
    const video = formats.find(f => f.itag === 134 || f.itag === 313); // Example: 360p or 2160p
    const audio = formats.find(f => f.itag === 140); // Standard m4a audio

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
