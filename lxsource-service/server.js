'use strict';
/**
 * lxsource-service —— 洛雪 (LX Music) JS 音源脚本订阅服务。
 *
 * 定位：补齐 lxmusic-service 缺失的「可订阅第三方解析脚本」能力。
 * LX 脚本只实现 musicUrl（解析直链），不实现 search/lyric，
 * 所以搜索与歌词仍由 lxmusic-service 的官方接口负责。
 *
 * 接口（零依赖，node:http + 内置 fetch）：
 *   GET  /healthz
 *   GET  /api/v1/subscriptions            订阅与加载状态
 *   POST /api/v1/subscriptions/refresh    手动刷新全部订阅
 *   GET  /api/v1/capabilities             可用平台/音质
 *   GET  /api/v1/track/url?source=&id=&quality=[&title=&artist=&info=<json>]
 *   POST /api/v1/track/url                body: {source,id,quality,title,artist,info}
 *
 * 环境变量：
 *   LX_SUBSCRIPTIONS     订阅列表（逗号/换行分隔，支持 name|url 与 file://）
 *   LXSOURCE_PORT        监听端口，默认 8774
 *   LXSOURCE_HOST        监听地址，默认 127.0.0.1
 *   LXSOURCE_REFRESH_S   自动刷新间隔秒，默认 3600；0 关闭
 *   LX_TOKEN             可选：设置后所有 /api/v1/* 需带 X-Api-Token
 */
const http = require('node:http');
const { URL } = require('node:url');
const { SubscriptionManager } = require('./subscription');

const PORT = Number(process.env.LXSOURCE_PORT || 8774);
const HOST = process.env.LXSOURCE_HOST || '127.0.0.1';
const REFRESH_S = Number(process.env.LXSOURCE_REFRESH_S ?? 3600);
const TOKEN = (process.env.LX_TOKEN || '').trim();
const MAX_REQUEST_BYTES = 256 * 1024;

const manager = new SubscriptionManager(process.env.LX_SUBSCRIPTIONS || '');

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function authorized(req) {
  if (!TOKEN) return true;
  const provided = req.headers['x-api-token'] || '';
  if (provided.length !== TOKEN.length) return false;
  // 常量时间比较，避免通过响应时间泄露 token
  return require('node:crypto').timingSafeEqual(Buffer.from(provided), Buffer.from(TOKEN));
}

async function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_REQUEST_BYTES) {
        reject(new Error('request body too large'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

/** 构造传给脚本的 musicInfo：兼容 hash / songmid 等多种字段名。 */
function buildMusicInfo(id, info, title, artist) {
  const musicInfo = {};
  if (id) {
    musicInfo.hash = id;
    musicInfo.songmid = id;
    musicInfo.musicId = id;
    musicInfo.id = id;
    musicInfo.copyrightId = id;
  }
  if (title) {
    musicInfo.name = title;
    musicInfo.title = title;
  }
  if (artist) {
    musicInfo.singer = artist;
    musicInfo.artist = artist;
  }
  if (info && typeof info === 'object') Object.assign(musicInfo, info);
  return musicInfo;
}

async function handleResolve(params) {
  const source = String(params.get('source') || '').trim();
  const id = String(params.get('id') || params.get('musicId') || '').trim();
  const quality = String(params.get('quality') || '320k').trim();
  const title = String(params.get('title') || '').trim();
  const artist = String(params.get('artist') || '').trim();
  let info = null;
  const rawInfo = params.get('info');
  if (rawInfo) {
    try { info = JSON.parse(rawInfo); } catch (_) { throw new Error('invalid info json'); }
  }
  if (!source) throw new Error('source is required');
  if (!id && !info) throw new Error('id is required');

  const musicInfo = buildMusicInfo(id, info, title, artist);
  const result = await manager.resolve(source, musicInfo, quality);
  if (!result.ok) {
    return { ok: false, error: 'no subscription resolved this track', attempts: result.attempts };
  }
  return {
    ok: true,
    data: { id, source, quality, url: result.url, subscription: result.subscription },
    attempts: result.attempts,
  };
}

const server = http.createServer(async (req, res) => {
  let parsed;
  try {
    parsed = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  } catch (_) {
    return sendJson(res, 400, { ok: false, error: 'bad request url' });
  }
  const route = parsed.pathname.replace(/\/+$/, '') || '/';

  try {
    if (route === '/healthz') {
      const s = manager.summary();
      // 未配置任何订阅 = 主动停用，不算故障（避免容器长期 unhealthy）；
      // 配了订阅却一个都没加载成功才是真正的降级。
      const degraded = s.total > 0 && s.ready === 0;
      return sendJson(res, degraded ? 503 : 200, {
        ok: !degraded,
        service: 'fnmusic-lxsource',
        version: '1.0.0',
        enabled: s.total > 0,
        subscriptions: { total: s.total, ready: s.ready },
        platforms: s.platforms,
        degraded,
      });
    }

    if (!authorized(req)) {
      return sendJson(res, 401, { ok: false, error: 'unauthorized' });
    }

    if (route === '/api/v1/subscriptions' && req.method === 'GET') {
      return sendJson(res, 200, { ok: true, data: manager.summary() });
    }

    if (route === '/api/v1/subscriptions/refresh' && req.method === 'POST') {
      await manager.refreshAll();
      return sendJson(res, 200, { ok: true, data: manager.summary() });
    }

    if (route === '/api/v1/capabilities' && req.method === 'GET') {
      const s = manager.summary();
      const caps = {};
      for (const sub of manager.subscriptions) {
        if (!sub.ready) continue;
        for (const [platform, def] of Object.entries(sub.sources || {})) {
          if (!caps[platform]) caps[platform] = { platform, qualitys: new Set(), subscriptions: [] };
          const qs = def.qualitys || def.quality || [];
          if (Array.isArray(qs)) qs.forEach((q) => caps[platform].qualitys.add(q));
          caps[platform].subscriptions.push(sub.name);
        }
      }
      for (const v of Object.values(caps)) {
        v.qualitys = [...v.qualitys];
        v.playback_available = true;
      }
      return sendJson(res, 200, { ok: true, platforms: s.platforms, capabilities: caps });
    }

    if (route === '/api/v1/track/url' && req.method === 'GET') {
      const payload = await handleResolve(parsed.searchParams);
      return sendJson(res, payload.ok ? 200 : 404, payload);
    }

    if (route === '/api/v1/track/url' && req.method === 'POST') {
      const raw = await readBody(req);
      let body = {};
      try { body = raw ? JSON.parse(raw) : {}; } catch (_) { throw new Error('invalid json body'); }
      const params = new URLSearchParams();
      for (const k of ['source', 'id', 'musicId', 'quality', 'title', 'artist']) {
        if (body[k] != null) params.set(k, String(body[k]));
      }
      if (body.info != null) params.set('info', JSON.stringify(body.info));
      const payload = await handleResolve(params);
      return sendJson(res, payload.ok ? 200 : 404, payload);
    }

    return sendJson(res, 404, { ok: false, error: 'not found' });
  } catch (err) {
    log('[lxsource] request failed:', req.method, route, err && err.message);
    return sendJson(res, 400, { ok: false, error: String((err && err.message) || err) });
  }
});

async function main() {
  if (!manager.subscriptions.length) {
    log('[lxsource] WARNING: LX_SUBSCRIPTIONS is empty; service will return 404 for all resolves');
  } else {
    log(`[lxsource] loading ${manager.subscriptions.length} subscription(s)...`);
  }
  const ready = await manager.loadAll();
  log(`[lxsource] ${ready}/${manager.subscriptions.length} subscription(s) ready`);
  manager.startAutoRefresh(REFRESH_S * 1000);

  server.listen(PORT, HOST, () => {
    log(`[lxsource] listening on http://${HOST}:${PORT} (refresh=${REFRESH_S}s, token=${TOKEN ? 'on' : 'off'})`);
  });
}

function shutdown(signal) {
  log(`[lxsource] received ${signal}, shutting down`);
  manager.stop();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

if (require.main === module) {
  main().catch((err) => {
    console.error('[lxsource] fatal:', err);
    process.exit(1);
  });
}

module.exports = { server, manager, buildMusicInfo };
