'use strict';
/**
 * 离线自检：不访问外网，用本地 fixture 脚本验证沙箱与订阅层。
 * 运行： node test.js
 */
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { loadSource, invoke, EVENT_NAMES } = require('./shim');
const { parseSubscriptions, SubscriptionManager } = require('./subscription');

const FIXTURE = path.join(__dirname, 'fixtures', 'example-source.js');
let passed = 0;
function ok(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}: ${err.message}`);
    process.exitCode = 1;
  }
}
async function okAsync(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}: ${err.message}`);
    process.exitCode = 1;
  }
}

console.log('parseSubscriptions');
ok('解析 name|url 形式', () => {
  const r = parseSubscriptions('ikun|https://x.top/s.js');
  assert.deepStrictEqual(r, [{ name: 'ikun', url: 'https://x.top/s.js' }]);
});
ok('解析裸 URL 自动命名', () => {
  const r = parseSubscriptions('https://x.top/script/mysrc.js');
  assert.strictEqual(r.length, 1);
  assert.ok(r[0].name.length > 0);
  assert.strictEqual(r[0].url, 'https://x.top/script/mysrc.js');
});
ok('去重同名订阅', () => {
  const r = parseSubscriptions('a|https://x/1.js,a|https://x/2.js');
  assert.strictEqual(r.length, 2);
  assert.notStrictEqual(r[0].name, r[1].name);
});
ok('支持 file:// 本地脚本', () => {
  const r = parseSubscriptions(`local|file://${FIXTURE}`);
  assert.strictEqual(r[0].url, `file://${FIXTURE}`);
});
ok('忽略空项', () => {
  assert.deepStrictEqual(parseSubscriptions('  ,, \n '), []);
});

console.log('shim loadSource');
ok('脚本可加载并注册 request 处理器', () => {
  const { handlers } = loadSource(fs.readFileSync(FIXTURE, 'utf8'));
  assert.strictEqual(typeof handlers.get(EVENT_NAMES.request), 'function');
});
ok('inited 事件回传 sources', () => {
  let inited = null;
  loadSource(fs.readFileSync(FIXTURE, 'utf8'), { onInited: (d) => { inited = d; } });
  assert.ok(inited && inited.status === true);
  assert.ok(inited.sources.kg);
  assert.ok(Array.isArray(inited.sources.kg.qualitys));
});
ok('沙箱内无法访问宿主 require/process', () => {
  // 与真实脚本一致：只从 globalThis.lx 解构，不依赖额外全局。
  const code = 'const {EVENT_NAMES, send} = globalThis.lx;'
    + 'send(EVENT_NAMES.inited,{status:true,sources:{}});'
    + 'globalThis.__leak = (typeof require) + "," + (typeof process) + "," + (typeof module);';
  const { sandbox } = loadSource(code);
  assert.strictEqual(sandbox.__leak, 'undefined,undefined,undefined');
});

console.log('shim invoke');
okAsync('musicUrl 返回直链', async () => {
  const { handlers } = loadSource(fs.readFileSync(FIXTURE, 'utf8'));
  const url = await invoke(handlers, {
    source: 'kg', musicInfo: { hash: 'abc' }, quality: '320k',
  });
  assert.ok(/^https:\/\//.test(url));
});
okAsync('脚本抛错时 invoke 也拒绝', async () => {
  const { handlers } = loadSource(fs.readFileSync(FIXTURE, 'utf8'));
  await assert.rejects(
    () => invoke(handlers, { source: 'bad', musicInfo: { hash: 'x' }, quality: '320k' }),
    /unsupported|not support/i
  );
});
okAsync('返回非 URL 时拒绝', async () => {
  const { handlers } = loadSource(fs.readFileSync(FIXTURE, 'utf8'));
  await assert.rejects(
    () => invoke(handlers, { source: 'badurl', musicInfo: { hash: 'x' }, quality: '320k' }),
    /invalid url/i
  );
});

console.log('SubscriptionManager');
(async () => {
  await okAsync('加载本地订阅并解析', async () => {
    const mgr = new SubscriptionManager(`fixture|file://${FIXTURE}`);
    const ready = await mgr.loadAll();
    assert.strictEqual(ready, 1);
    const r = await mgr.resolve('kg', { hash: 'abc' }, '320k');
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.subscription, 'fixture');
  });
  await okAsync('坏订阅不影响好订阅', async () => {
    const mgr = new SubscriptionManager(`bad|file:///nonexistent/nope.js,fixture|file://${FIXTURE}`);
    const ready = await mgr.loadAll();
    assert.strictEqual(ready, 1);
    const r = await mgr.resolve('kg', { hash: 'abc' }, '320k');
    assert.strictEqual(r.ok, true);
  });
  await okAsync('所有订阅都不可用时 resolve 失败但不抛异常', async () => {
    const mgr = new SubscriptionManager('bad|file:///nonexistent/nope.js');
    await mgr.loadAll();
    const r = await mgr.resolve('kg', { hash: 'abc' }, '320k');
    assert.strictEqual(r.ok, false);
    assert.ok(Array.isArray(r.attempts));
  });
  await okAsync('summary 反映就绪状态', async () => {
    const mgr = new SubscriptionManager(`fixture|file://${FIXTURE}`);
    await mgr.loadAll();
    const s = mgr.summary();
    assert.strictEqual(s.ready, 1);
    assert.ok(s.platforms.includes('kg'));
  });

  console.log(`\n${passed} 项检查通过${process.exitCode ? '，但有失败' : ''}`);
})();
