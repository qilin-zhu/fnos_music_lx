'use strict';
/**
 * 洛雪音乐 (LX Music) 音源脚本沙箱。
 *
 * 用 node:vm 提供一个最小可用的 `globalThis.lx`，让社区音源脚本（JS）
 * 能在隔离上下文中加载并执行其声明的动作。
 *
 * 重要事实（决定了本服务的定位）：
 *   标准 LX 音源脚本只声明 `musicUrl` 动作，即「给定平台 + 歌曲 id + 音质，
 *   返回可直接播放的直链」。它 **不提供 search / lyric**。
 *   因此搜索仍由 lxmusic-service 的官方接口负责，本服务只替代「解析」环节。
 */
const vm = require('node:vm');
const crypto = require('node:crypto');

const EVENT_NAMES = Object.freeze({
  request: 'request',
  inited: 'inited',
  updateAlert: 'updateAlert',
});

const DEFAULT_LOAD_TIMEOUT_MS = 20000;
const DEFAULT_REQUEST_TIMEOUT_MS = 15000;
const MAX_BODY_BYTES = 2 * 1024 * 1024;

function toBuffer(value, encoding) {
  if (Buffer.isBuffer(value)) return value;
  if (value instanceof Uint8Array) return Buffer.from(value);
  return Buffer.from(String(value), encoding || 'utf8');
}

function safeConsole(log) {
  const emit = (...args) => {
    try {
      log(args.map((a) => {
        if (typeof a === 'string') return a;
        try { return JSON.stringify(a); } catch (_) { return String(a); }
      }).join(' '));
    } catch (_) { /* 日志失败绝不影响脚本 */ }
  };
  return {
    log: emit,
    info: emit,
    warn: emit,
    error: emit,
    debug: emit,
    group: emit,
    groupEnd: () => {},
    table: emit,
  };
}

/**
 * 构造 lx API 与沙箱上下文。
 * @param {object} hooks { onInited, onUpdateAlert, onLog, requestTimeoutMs, fetchImpl }
 */
function buildContext(hooks = {}) {
  const handlers = new Map();
  const log = hooks.onLog || (() => {});
  const requestTimeoutMs = hooks.requestTimeoutMs || DEFAULT_REQUEST_TIMEOUT_MS;
  const doFetch = hooks.fetchImpl || globalThis.fetch;

  const lx = {
    EVENT_NAMES,
    env: hooks.env || 'desktop',
    version: hooks.version || '2.11.0',

    on(event, handler) {
      if (typeof handler === 'function') handlers.set(event, handler);
    },

    send(event, data) {
      try {
        if (event === EVENT_NAMES.inited) hooks.onInited && hooks.onInited(data);
        else if (event === EVENT_NAMES.updateAlert) hooks.onUpdateAlert && hooks.onUpdateAlert(data);
      } catch (err) {
        log(`[lxsource] send(${event}) hook failed: ${err && err.message}`);
      }
    },

    /**
     * 脚本唯一的网络出口。返回 (err, resp)，与 LX 官方一致。
     * resp.body 已按内容尽力解析为 JSON。
     */
    request(url, options, callback) {
      const opts = options || {};
      const cb = typeof callback === 'function' ? callback : () => {};
      if (!/^https?:\/\//i.test(String(url))) {
        setImmediate(() => cb(new Error('only http(s) urls are allowed')));
        return;
      }
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), requestTimeoutMs);
      (async () => {
        const method = String(opts.method || 'GET').toUpperCase();
        const headers = Object.assign({}, opts.headers || {});
        let body;
        if (opts.body != null) {
          if (typeof opts.body === 'string') body = opts.body;
          else {
            body = JSON.stringify(opts.body);
            if (!Object.keys(headers).some((k) => k.toLowerCase() === 'content-type')) {
              headers['Content-Type'] = 'application/json';
            }
          }
        }
        const res = await doFetch(url, { method, headers, body, signal: controller.signal, redirect: 'follow' });
        const text = await res.text();
        if (text.length > MAX_BODY_BYTES) throw new Error('response body too large');
        let parsed = text;
        try { parsed = JSON.parse(text); } catch (_) { /* 保留原始文本 */ }
        return {
          statusCode: res.status,
          statusMessage: res.statusText,
          headers: Object.fromEntries(res.headers),
          body: parsed,
          raw: text,
        };
      })()
        .then((resp) => cb(null, resp))
        .catch((err) => cb(err && err.name === 'AbortError' ? new Error('request timeout') : err))
        .finally(() => clearTimeout(timer));
    },

    utils: {
      buffer: {
        from: (...a) => Buffer.from(...a),
        bufToString: (b, f) => toBuffer(b).toString(f || 'utf8'),
      },
      crypto: {
        md5: (s) => crypto.createHash('md5').update(toBuffer(s)).digest('hex'),
        sha1: (s) => crypto.createHash('sha1').update(toBuffer(s)).digest('hex'),
        randomBytes: (n) => crypto.randomBytes(Number(n) || 16),
        aesEncrypt: () => { throw new Error('aesEncrypt not supported'); },
        rsaEncrypt: () => { throw new Error('rsaEncrypt not supported'); },
      },
      str2b64: (s) => Buffer.from(String(s), 'utf8').toString('base64'),
      b64Encode: (s) => Buffer.from(String(s), 'utf8').toString('base64'),
      b64Decode: (s) => Buffer.from(String(s), 'base64').toString('utf8'),
      md5: (s) => crypto.createHash('md5').update(toBuffer(s)).digest('hex'),
      randomBytes: (n) => crypto.randomBytes(Number(n) || 16),
    },
  };

  const sandbox = {
    lx,
    console: safeConsole(log),
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    setImmediate,
    queueMicrotask,
    Buffer,
    URL,
    URLSearchParams,
    TextEncoder,
    TextDecoder,
    AbortController,
    fetch: doFetch,
    Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, Error,
    TypeError, RangeError, Map, Set, WeakMap, WeakSet, RegExp, Symbol, Proxy,
    Reflect, ArrayBuffer, Uint8Array, Int8Array, Uint16Array, Int16Array,
    Uint32Array, Int32Array, Float32Array, Float64Array, DataView,
    encodeURIComponent, decodeURIComponent, encodeURI, decodeURI,
    parseInt, parseFloat, isNaN, isFinite,
  };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  return { sandbox, handlers, lx };
}

/**
 * 在沙箱中加载一段音源脚本。
 * @returns {{sources: object, updateAlert: object|null, handlers: Map, errors: string[]}}
 */
function loadSource(code, hooks = {}) {
  const { sandbox, handlers } = buildContext(hooks);
  const ctx = vm.createContext(sandbox);
  vm.runInContext(String(code), ctx, {
    timeout: hooks.loadTimeoutMs || DEFAULT_LOAD_TIMEOUT_MS,
    filename: hooks.filename || 'lx-source.js',
  });
  return { sandbox, handlers };
}

/**
 * 对已加载的沙箱执行一次 musicUrl 调用。
 * @param {Map} handlers
 * @param {{source:string, musicInfo:object, quality:string, action?:string}} params
 */
async function invoke(handlers, { source, musicInfo, quality, action = 'musicUrl' }) {
  const handler = handlers.get(EVENT_NAMES.request);
  if (typeof handler !== 'function') {
    throw new Error('source did not register a request handler');
  }
  const result = handler({ action, source, info: { type: quality, musicInfo } });
  const url = await Promise.resolve(result);
  if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) {
    throw new Error(`source returned an invalid url: ${String(url).slice(0, 120)}`);
  }
  return url;
}

module.exports = { loadSource, invoke, buildContext, EVENT_NAMES };
