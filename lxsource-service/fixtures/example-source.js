/*!
 * 测试用洛雪音源脚本 fixture（不访问外网）。
 * 模拟真实 LX 脚本的最小契约：声明 sources、注册 request 处理器、返回直链。
 */
const MUSIC_QUALITY = { kg: ["128k", "320k", "flac"], wy: ["128k", "320k", "flac"] };

const { EVENT_NAMES, on, send } = globalThis.lx;

const musicSources = {};
Object.keys(MUSIC_QUALITY).forEach((item) => {
  musicSources[item] = {
    name: item,
    type: "music",
    actions: ["musicUrl"],
    qualitys: MUSIC_QUALITY[item],
  };
});

on(EVENT_NAMES.request, ({ action, source, info }) => {
  if (action !== "musicUrl") return Promise.reject("action not support");
  const id = info.musicInfo.hash ?? info.musicInfo.songmid;
  if (source === "bad") return Promise.reject("source not support");
  if (source === "badurl") return Promise.resolve("not-a-url");
  if (!id) return Promise.reject("missing id");
  return Promise.resolve(`https://cdn.example.com/${source}/${id}.mp3`);
});

send(EVENT_NAMES.inited, { status: true, openDevTools: false, sources: musicSources });
