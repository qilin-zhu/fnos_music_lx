'use strict';
/**
 * 订阅管理：从 URL 或本地文件加载洛雪音源脚本，缓存元信息与更新状态。
 *
 * 订阅来源（环境变量 LX_SUBSCRIPTIONS，逗号或换行分隔）：
 *   https://example.com/script.js          纯 URL
 *   name|https://example.com/script.js     带别名
 *   name|file:///path/to/script.js         本地文件
 *
 * 脚本按 `MD5(内容)` 做版本判定；脚本自身的 checkUpdate 请求同样可用。
 */
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');

const { loadSource, invoke } = require('./shim');

const FETCH_TIMEOUT_MS = Number(process.env.LX_SUB_FETCH_TIMEOUT_MS || 20000);
const MAX_SCRIPT_BYTES = Number(process.env.LX_SUB_MAX_BYTES || 4 * 1024 * 1024);

function md5(text) {
  return crypto.createHash('md5').update(text).digest('hex');
}

/** 解析 LX_SUBSCRIPTIONS */
function parseSubscriptions(raw) {
  const out = [];
  const items = String(raw || '')
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const used = new Set();
  items.forEach((item, idx) => {
    let name = '';
    let url = item;
    const sep = item.indexOf('|');
    if (sep > 0) {
      name = item.slice(0, sep).trim();
      url = item.slice(sep + 1).trim();
    }
    if (!url) return;
    if (!name) {
      try {
        const u = new URL(url);
        name = u.protocol === 'file:'
          ? path.basename(u.pathname).replace(/\.js$/i, '')
          : (u.hostname + u.pathname.replace(/\.js$/i, '')).replace(/[^A-Za-z0-9_.-]/g, '_');
      } catch (_) {
        name = `source${idx + 1}`;
      }
    }
    let unique = name || `source${idx + 1}`;
    let n = 2;
    while (used.has(unique)) unique = `${name}_${n++}`;
    used.add(unique);
    out.push({ name: unique, url });
  });
  return out;
}

async function readSourceText(url) {
  if (url.startsWith('file://')) {
    const filePath = new URL(url);
    const text = await fsp.readFile(filePath, 'utf8');
    if (text.length > MAX_SCRIPT_BYTES) throw new Error('script too large');
    return text;
  }
  if (!/^https?:\/\//i.test(url)) throw new Error(`unsupported subscription scheme: ${url}`);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      redirect: 'follow',
      headers: { 'User-Agent': 'fnmusic-ext-lxsource/1.0' },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    if (text.length > MAX_SCRIPT_BYTES) throw new Error('script too large');
    return text;
  } finally {
    clearTimeout(timer);
  }
}

class Subscription {
  constructor({ name, url }) {
    this.name = name;
    this.url = url;
    this.hash = '';
    this.sources = {};
    this.status = 'pending';
    this.error = '';
    this.loadedAt = 0;
    this.updateAlert = null;
    this.handlers = null;
    this.cachedCode = '';
  }

  get ready() {
    return this.status === 'ready' && this.handlers !== null;
  }

  /** 拉取并（重新）加载脚本；内容没变则复用已加载的沙箱。 */
  async refresh(force = false) {
    let text;
    try {
      text = await readSourceText(this.url);
    } catch (err) {
      // 拉取失败时保留上一次可用版本，只在从未加载成功时报错。
      if (this.ready && !force) {
        this.error = `refresh failed, keeping previous: ${err.message}`;
        return false;
      }
      this.status = 'error';
      this.error = err.message;
      return false;
    }

    const hash = md5(text);
    if (hash === this.hash && this.ready && !force) {
      this.error = '';
      return false;
    }

    try {
      const { handlers } = loadSource(text, {
        filename: `${this.name}.js`,
        onInited: (data) => {
          if (data && typeof data === 'object' && data.sources) {
            this.sources = data.sources;
          }
        },
        onUpdateAlert: (data) => { this.updateAlert = data || null; },
        onLog: (msg) => console.log(`[lxsource:${this.name}] ${msg}`),
      });
      // inited 是同步 send 的；若脚本没发，也允许空 sources（罕见）。
      this.handlers = handlers;
      this.hash = hash;
      this.cachedCode = text;
      this.status = 'ready';
      this.error = '';
      this.loadedAt = Date.now();
      this.updateAlert = null;
      console.log(
        `[lxsource:${this.name}] loaded hash=${hash.slice(0, 8)} sources=${Object.keys(this.sources).join(',') || '(none)'}`
      );
      return true;
    } catch (err) {
      this.status = 'error';
      this.error = `load failed: ${err.message}`;
      return false;
    }
  }

  /** 在沙箱中解析直链。 */
  async resolve(source, musicInfo, quality) {
    if (!this.ready) throw new Error(`subscription not ready: ${this.error || this.status}`);
    return invoke(this.handlers, { source, musicInfo, quality });
  }

  /** 该订阅是否声明支持某平台/音质。 */
  supports(source, quality) {
    const def = this.sources && this.sources[source];
    if (!def) return false;
    const qs = def.qualitys || def.quality || [];
    if (!quality) return true;
    return Array.isArray(qs) ? qs.includes(quality) : true;
  }

  info() {
    return {
      name: this.name,
      url: this.url,
      status: this.status,
      error: this.error,
      hash: this.hash,
      loadedAt: this.loadedAt,
      sources: Object.keys(this.sources || {}),
      updateAlert: this.updateAlert,
    };
  }
}

class SubscriptionManager {
  constructor(rawList) {
    this.subscriptions = parseSubscriptions(rawList).map((s) => new Subscription(s));
    this.refreshTimer = null;
  }

  get(name) {
    return this.subscriptions.find((s) => s.name === name) || null;
  }

  /** 首次加载：并发拉取，单个失败不影响其它。 */
  async loadAll() {
    await Promise.all(this.subscriptions.map((s) => s.refresh(true)));
    return this.subscriptions.filter((s) => s.ready).length;
  }

  /** 定时刷新；按 hash 判断，未变更不重载沙箱。 */
  async refreshAll() {
    await Promise.all(this.subscriptions.map((s) => s.refresh(false)));
  }

  startAutoRefresh(intervalMs) {
    if (!intervalMs || intervalMs <= 0) return;
    this.refreshTimer = setInterval(() => {
      this.refreshAll().catch((err) => console.error('[lxsource] auto refresh failed:', err && err.message));
    }, intervalMs);
    if (this.refreshTimer.unref) this.refreshTimer.unref();
  }

  stop() {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    this.refreshTimer = null;
  }

  /**
   * 依次尝试所有就绪订阅解析；返回第一个成功的直链。
   * 失败的订阅不影响后续订阅。
   */
  async resolve(source, musicInfo, quality) {
    const attempts = [];
    for (const sub of this.subscriptions) {
      if (!sub.ready) {
        attempts.push({ name: sub.name, ok: false, error: sub.error || sub.status });
        continue;
      }
      if (!sub.supports(source, quality)) {
        attempts.push({ name: sub.name, ok: false, error: `unsupported source/quality` });
        continue;
      }
      try {
        const url = await sub.resolve(source, musicInfo, quality);
        return { ok: true, url, subscription: sub.name, attempts };
      } catch (err) {
        attempts.push({ name: sub.name, ok: false, error: err.message });
      }
    }
    return { ok: false, attempts };
  }

  snapshot() {
    return this.subscriptions.map((s) => s.info());
  }

  summary() {
    const ready = this.subscriptions.filter((s) => s.ready);
    const platforms = new Set();
    for (const s of ready) for (const p of Object.keys(s.sources || {})) platforms.add(p);
    return {
      total: this.subscriptions.length,
      ready: ready.length,
      platforms: [...platforms].sort(),
      subscriptions: this.snapshot(),
    };
  }
}

module.exports = { SubscriptionManager, Subscription, parseSubscriptions };
