# 洛雪歌单（平台排行榜）接入说明

在飞牛音乐侧边栏的「歌单」中显示网易云 / 酷狗 / 酷我的排行榜，点击即可播放。

## 一、先厘清一个常见误解

「洛雪歌单」有两种完全不同的含义，本功能实现的是**前者**：

| 含义 | 是否支持 | 原因 |
| :--- | :--- | :--- |
| **平台排行榜**（热歌榜、TOP500…） | ✅ 本次实现 | 平台有公开接口，可直接拉取 |
| 你洛雪客户端里的自建歌单 | ❌ 不支持 | 洛雪音源脚本协议**只声明 `musicUrl`**，不提供歌单/搜索动作；第三方源也读不到你客户端本地数据 |

洛雪音源脚本（如 `ikun-music-source.js`）的声明是：

```js
actions: ["musicUrl"]   // 只有解析直链，没有 playlist
```

所以「订阅洛雪脚本」能换来**解析能力**（换源），但不能换来**歌单数据**。
歌单数据必须另找来源——即各平台的公开排行榜接口。

## 二、实现方式

复用每日推荐已有的「虚拟歌单」注入机制：

```
GET /music/api/v1/playlist/list          ← 侧边栏歌单列表
      └─ 注入 chart guid 前缀的歌单条目
GET /music/api/v1/playlist/detail        ← 歌单详情
GET /music/api/v1/track/playlist-detail/list  ← 歌单曲目
      └─ 返回榜单曲目（guid = online:lx:<平台>:<id>）
GET /music/api/v1/track/stream           ← 播放时由 lxmusic-service 解析直链
```

- GUID 形式：`online:playlist:chart:<平台>:<榜单id>`
- 榜单**不写入**飞牛数据库，纯内存缓存（默认 30 分钟）
- 曲目播放仍走既有在线曲目链路，自动享受边播边存/歌词/封面

## 三、实测数据（2026-09-17）

### 可用榜单（默认白名单）

| 平台 | 榜单 | 曲目数 |
| :--- | :--- | :--- |
| 网易云 | 热歌榜 / 新歌榜 / 飙升榜 / 原创榜 | 各 100 |
| 酷狗 | TOP500 / 飙升榜 / 短视频热歌榜 / DJ热歌榜 / 民谣榜 | 各 100（短视频榜 38） |
| 酷我 | 热歌榜 / 飙升榜 / 新歌榜 / 华语榜 | 各 100 |

### 播放链路验证

对三个平台各抽 3 首实测 `track/url`：**9/9 成功解析出直链**并通过 Range 探活。

| 平台 | 抽样结果 |
| :--- | :--- |
| 网易云 | 3/3 → `m801.music.126.net` |
| 酷狗 | 3/3 → `fsdg360.hw.kugou.com` |
| 酷我 | 3/3 → `car-lv.kuwo.cn` |

## 四、关键设计点

### 1. 榜单曲目的元数据必须自行登记（重要）

`lxmusic-service` 的曲目缓存**只在搜索时填充**。榜单曲目是直接构造的，
若直接去查 `track/info` 会拿到空标题，导致：

- 界面显示空标题
- 边播边存写出 `unknown.mp3`
- 歌词/封面全部缺失

因此本功能新增 `_CHART_META` 元数据表，在拉取榜单曲目时登记
`title/artist/album/cover/duration`，并由 `_fetch_online_info()` 优先返回：

```python
async def _fetch_online_info(request, guid):
    chart_meta = chart_meta_for(guid)   # 榜单曲目走这里
    if chart_meta:
        return dict(chart_meta)
    ...
```

这正是此前 `unknown.mp3` 问题的通用解法。

### 2. 重名榜单自动加平台前缀

网易云和酷狗都有「飙升榜」。若同名会显示两个一样的名字，
本功能在检测到重名时自动加前缀：`网易云·飙升榜` / `酷狗·飙升榜`。

### 3. 歌曲数回填

- 网易云榜单列表接口带 `trackCount`，直接使用
- 酷我榜单详情接口带 `num`，`_kw_charts` 会并发补齐真实名称/数量/封面
- 酷狗的列表接口**不提供**歌曲数，只有拉过曲目才知道

因此显示 `0` 而非"假数字"；一旦你打开过该榜单，会把真实数量回填到缓存，
下次进侧边栏即显示正确数量。

### 4. 歌单封面

侧边栏歌单封面走 `coverId`（即歌单 guid）请求 `/static/cover`，
而榜单封面**不来自在线曲目**，因此需要单独登记：

```
playlist/list  →  remember_chart_cover(guid, ch["cover"])
/static/cover  →  chartrec.is_chart_guid(guid) → 302 到远程封面
```

三个平台的封面字段与处理：

| 平台 | 字段 | 处理 |
| :--- | :--- | :--- |
| 网易云 | `coverImgUrl` | 直接可用 |
| 酷狗 | `imgurl` / `bannerurl` | 含 `{size}` 占位符，需替换为 480 |
| 酷我 | `v9_pic2` | 榜单详情接口返回，并发补齐 |

**冷缓存自愈**：封面可能先于侧边栏列表被请求（例如代理刚重启、用户直接点开历史歌单）。
此时 `_refresh_chart_cover()` 会按平台重新取一次榜单元信息；
若仍拿不到，再退一步用榜单首曲的封面。

### 5. 歌词（必须走 lxmusic，不能被元数据短路）

榜单曲目没有本地歌词缓存。历史 bug：`_fetch_online_info()` 对榜单曲目
**直接返回 `chart_meta`（`lyric=""`）**，导致歌词查询被短路，
表现为「有封面没歌词」。

修复：`_fetch_online_info()` 在返回 `chart_meta` 前，若 `lyric` 为空，
调用 `_fetch_lx_lyric()` 向 lxmusic-service 取歌词并合并。

```
_fetch_online_info(guid)
  ├─ chart_meta 命中 → 合并执行 _fetch_lx_lyric()  ← 修复点
  └─ 未命中 → 原有 lx 分支（已有歌词逻辑）
```

歌词落盘策略：未播放时以 GUID 命名（`online_lx_wy_<id>.lrc`），
播放后 `remember_media_path` 记录音频文件名，歌词自动对齐为
`歌手 - 歌名.lrc`。

实测：网易云/酷狗歌词可用；部分酷我曲目源站本身无歌词（返回空，非我方问题）。

### 6. 音质格式：默认无损，无无损源才回退 mp3

榜单曲目原先写死 `ext: "mp3"`，导致界面全部显示 mp3，而实际解析出来多为 flac。

修复：按各平台元数据判断是否具备无损源，有则默认 `flac`：

| 平台 | 无损判据 |
| :--- | :--- |
| 网易云 | `sq`（无损）/ `hr`（Hi-Res）字段存在且有 br/size |
| 酷狗 | `sqhash` 非空 |
| 酷我 | `formats` 含 `ALFLAC` / `DTSX` / `ZPGA714` 等标记 |

注意：**显示格式 ≠ 播放音质**。显示只影响界面与文件名；
真正播放时仍由 lxmusic-service 按 `lossless → high → standard` 顺序解析，
拿不到无损才降级。实测三平台榜单抽样均为 flac，仅个别曲目无无损源回退 mp3。

可用 `FNMUSIC_CHART_DEFAULT_EXT` 调整默认扩展名（默认 `flac`）。

### 7. 失败隔离与超时

- 单个平台接口失败 → 只丢该平台，其它照常
- 整批榜单拉取设 `FNMUSIC_CHART_LIST_WAIT`（默认 6s）上限，超时本次不注入，
  **绝不拖慢侧边栏**
- 平台接口偶发 TLS/连接抖动 → 自动重试 3 次（退避 0.6s/1.2s）
- 榜单注入失败不影响官方歌单与每日推荐

## 五、启用方法

编辑仓库根目录 `.env`（需 root）：

```bash
sudo tee -a .env >/dev/null <<'EOF'

# ---- 洛雪歌单：平台排行榜注入侧边栏 ----
FNMUSIC_CHARTS_ENABLED=true
FNMUSIC_CHART_PLATFORMS=wy,kg,kw
FNMUSIC_CHART_TRACK_LIMIT=100
FNMUSIC_CHART_TTL=1800
EOF
```

代理以 systemd（宿主模式）运行，改完需重启：

```bash
sudo systemctl restart fnmusic-ext
```

> 若用 Docker 热补丁运行 lxmusic，注意本功能改的是 **proxy/**，不是 lxmusic-service。
> proxy 始终由宿主机 systemd 管理，所以重启 `fnmusic-ext` 即可。

### 验证

```bash
# 侧边栏歌单接口（需已登录态）
curl -s --unix-socket /var/run/trim_music.socket \
  "http://localhost/music/api/v1/playlist/list" | python3 -m json.tool | head -40

# 应能看到 name 为「热歌榜」「TOP500」等、isChart=true 的条目
```

## 六、环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_CHARTS_ENABLED` | `false` | 总开关 |
| `FNMUSIC_CHART_PLATFORMS` | `wy,kg,kw` | 展示哪些平台 |
| `FNMUSIC_CHART_TRACK_LIMIT` | `100` | 单榜曲目上限 |
| `FNMUSIC_CHART_TTL` | `1800` | 榜单缓存秒数 |
| `FNMUSIC_CHART_LIST_WAIT` | `6` | 侧边栏等待上限（秒） |
| `FNMUSIC_CHART_TIMEOUT` | `12` | 单次接口超时（秒） |
| `FNMUSIC_CHART_RETRIES` | `3` | 接口重试次数 |
| `FNMUSIC_CHART_COVER_SIZE` | `480` | 榜单封面尺寸（酷狗 URL 的 `{size}` 占位符） |

## 七、测试

```bash
python -m pytest proxy/tests/test_playlists.py -q
```

58 项测试，全部使用 `MockTransport`，**不访问外网**，覆盖：

- GUID 构造/解析/非法平台拒绝
- 三平台字段映射（含酷狗 `singername=null` 时从 `filename` 提取歌手）
- 白名单过滤
- 平台失败隔离
- 代理端点注入与元数据登记
- 歌曲数回填
- 歌单封面（酷狗 {size} 占位符替换、酷我元信息补齐、冷缓存自愈、302 重定向）
- 无损格式判定（三平台判据 + mp3 回退）
- 歌词不被元数据短路

## 八、已知边界

- **榜单数量固定**：`CHART_WHITELIST` 内置了精选榜单白名单，
  避免侧边栏被 60+ 个榜单淹没。要增删榜单改 `proxy/playlists.py` 中的白名单即可。
- **酷我榜单列表是静态的**：酷我没有「榜单列表」接口，故使用内置集合；
  其榜单接口失效时会返回空曲目而非报错。
- **第三方接口随时可能变**：平台接口属非官方公开接口，
  失效时该平台榜单会返回空；熔断/超时保护确保不影响其它功能。
- **版权**：榜单数据来自各平台公开接口，仅供个人学习研究，
  请遵守当地法律与各平台服务条款，支持正版。
