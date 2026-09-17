# 洛雪音源脚本订阅服务（lxsource-service）

## 为什么需要它

`lxmusic-service` 是洛雪音源思路的**独立 Python 重写**，不加载 JS 脚本
（README 第 172 行已声明「不包含其脚本代码」）。因此社区的洛雪音源脚本
（`*.js`）无法直接使用，而这类脚本正是「接口失效时最快获得新解析途径」的来源。

本服务补齐这一环：**用 Node 内置 `vm` 沙箱加载洛雪 JS 脚本**，暴露成 HTTP 接口，
由 `lxmusic-service` 作为**兜底解析链路**调用。脚本随订阅链接更新即可生效，无需改仓库代码。

## 关键约束（务必先理解）

我在实测中确认：**标准洛雪音源脚本只声明 `musicUrl` 动作**。

```js
actions: ["musicUrl"]     // 没有 search，没有 lyric
```

它接收 `(source, musicInfo, quality)` 返回**直链**；搜索与歌词它不负责。

所以本服务的定位是**只替代「解析」环节**：

| 环节 | 负责方 |
| :--- | :--- |
| 搜索（歌名 → 平台 id） | `lxmusic-service` 官方接口（不变） |
| 解析（平台 id → 可播直链） | **本服务**（订阅脚本）+ 既有链路兜底 |
| 歌词 / 封面 | `lxmusic-service`（不变） |

## 实测结论（2026-09-17）

用你的 `ikun` 脚本（`https://c.wwwweb.top/script/lxmusic?key=...`）验证：

| 项目 | 结果 |
| :--- | :--- |
| 脚本加载 | ✅ `hash=75da8ac3`，声明 `git,kg,kw,tx,wy` |
| 沙箱隔离 | ✅ 脚本内 `require`/`process`/`module` 均为 `undefined` |
| kg / wy / tx 解析 | ✅ 全部返回真实直链 |
| Range 探活 | ✅ `206`，首字节 `ID3` |
| 解析延迟 | ✅ 约 0.2–0.3s（含一次跳转） |
| 坏订阅容错 | ✅ 一个订阅失败不影响其它订阅 |
| 空订阅 | ✅ 返回 404（**不**触发熔断） |

## 架构

```text
飞牛音乐前端
      │
      ▼
fnmusic-ext 代理（socket 接管）
      │
      ▼
lxmusic-service  ── 搜索走官方接口（kg/wy/mg/tx/kw）
      │
      │  解析失败时按序回退
      ├─► ikun 直连 API         （快路径，可选，需 LX_IKUN_KEY）
      ├─► changqing_kw / suyin_mg（原有链路，不变）
      └─► lxsource-service  ◄── 订阅的洛雪 JS 脚本（本服务，兜底）
                  │
                  └─ POST /music/url 等第三方 API
```

`lxsource-service` 只监听 `127.0.0.1:8774`，不对外暴露。

## 部署

### 1. 应用补丁（给 lxmusic-service 增加订阅链路）

```bash
cd fnmusic_ext

# 备份
sudo cp lxmusic-service/app.py lxmusic-service/app.py.bak.$(date +%Y%m%d%H%M%S)

# 应用（含 ikun 快路径 + 订阅兜底）
sudo patch -p1 < patches/lxmusic-subscription.patch
```

> 该补丁**替代**了之前的 `ikun-source.patch`（后者只含 ikun 快路径）。
> 若已应用过 `ikun-source.patch`，请先还原再应用此补丁。

### 2. 配置订阅（写入 `.env`）

```bash
sudo tee -a .env >/dev/null <<'EOF'

# ---- 洛雪音源脚本订阅（可选）----
# 多个订阅用逗号分隔；支持 name|url 与 file:// 本地文件
# 例：LX_SUBSCRIPTIONS='ikun|https://c.wwwweb.top/script/lxmusic?key=xxx,other|https://example.com/a.js'
LX_SUBSCRIPTIONS='ikun|https://c.wwwweb.top/script/lxmusic?key=<你的KEY>'
LXSOURCE_REFRESH_S=3600
# 可选：保护 lxsource 的 API（留空则不鉴权）
LX_TOKEN=
EOF
```

### 3. 启动

```bash
docker compose build lxmusic lxsource
docker compose up -d lxmusic lxsource
```

### 4. 验证

```bash
# 订阅服务（ok:true 表示至少一个订阅加载成功）
curl -s http://127.0.0.1:8774/healthz | python3 -m json.tool

# 订阅详情（每个脚本的状态与声明的平台）
curl -s http://127.0.0.1:8774/api/v1/subscriptions | python3 -m json.tool

# 直接测试解析
curl -s "http://127.0.0.1:8774/api/v1/track/url?source=tx&id=0039MnYb0qxYhV&quality=320k" \
  | python3 -m json.tool

# lxmusic 侧：chains 里应出现 subscription，tx 应变为可播放
curl -s http://127.0.0.1:8772/healthz | python3 -m json.tool
```

### 5. 改订阅源（推荐用脚本，免手改）

订阅列表存在 `.env.lxsource.local`（0600、已 gitignore）。
`deploy.sh` 内置了管理子命令，无需手工编辑：

```bash
cd fnmusic_ext

# 查看当前订阅（密钥自动脱敏）
./deploy.sh --list-subscriptions

# 追加一个订阅（支持 name|URL、裸 URL、file:// 本地脚本）
./deploy.sh --add-subscription 'mysrc|https://example.com/my-source.js'

# 删除
./deploy.sh --remove-subscription mysrc

# 整组替换（多订阅用逗号分隔，按顺序回退）
./deploy.sh --set-subscriptions 'a|https://a.js,b|https://b.js'

# 只改了脚本内容（地址/key 不变）→ 热刷新，无需重建
./deploy.sh --refresh-subscriptions
```

**关键区别**：

| 场景 | 需要的操作 | 原因 |
| :--- | :--- | :--- |
| 新增/删除/替换订阅 | 重建服务 | `LX_SUBSCRIPTIONS` 是**启动时注入的环境变量**，不是挂载文件 |
| 仅换 API key（地址不变） | 重建服务 | 同上——env 值变了 |
| 订阅脚本内容自己更新了 | `--refresh-subscriptions` | 服务每 3600s 自动拉取，按内容 MD5 判断 |

脚本会自动识别服务当前跑在 **docker 还是宿主机**，用对应方式重建。
加 `--no-redeploy` 可只改配置文件、暂不重建。

若要用 `docker compose` 手工重建，注意必须带上订阅配置，
否则 compose 读不到（它默认只读 root 的 `.env`）：

```bash
sudo docker compose --env-file .env.lxsource.local -f docker-compose.yml \
  up -d --force-recreate lxsource
```

设置 `LX_SUBSCRIPTIONS` 后重启 lxmusic 容器，`tx` 会从
`no_resolver_registered` 变为 `playback_available: true`。

## 接口契约

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/healthz` | 免鉴权；`ok` 表示至少一个订阅就绪 |
| GET | `/api/v1/subscriptions` | 订阅列表与加载状态 |
| POST | `/api/v1/subscriptions/refresh` | 手动刷新全部订阅 |
| GET | `/api/v1/capabilities` | 各平台可用音质 |
| GET | `/api/v1/track/url` | `source,id,quality[,title,artist,info]` |
| POST | `/api/v1/track/url` | 同上，JSON body |

设置 `LX_TOKEN` 后，除 `/healthz` 外都需请求头 `X-Api-Token`。

## 环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `LX_SUBSCRIPTIONS` | 空 | 订阅列表，逗号/换行分隔，支持 `name\|url` 与 `file://` |
| `LXSOURCE_PORT` | `8774` | 监听端口 |
| `LXSOURCE_HOST` | `127.0.0.1` | 监听地址（容器内为 `0.0.0.0`） |
| `LXSOURCE_REFRESH_S` | `3600` | 自动刷新间隔秒；`0` 关闭 |
| `LX_TOKEN` | 空 | 设置后 API 需 `X-Api-Token` |
| `LX_SUBSCRIPTIONS_URL` | 空 | lxmusic 侧指向本服务的地址 |

## 设计要点

1. **沙箱隔离**：`node:vm` 上下文只注入 `globalThis.lx` 与安全的 Web 标准对象，
   脚本拿不到 `require`/`process`/`module`。网络只能经 `lx.request`（仅允许 http/https，
   带超时与体积上限）。
2. **按内容哈希判定更新**：脚本内容 MD5 不变则不重建沙箱，避免无谓重载。
3. **坏订阅不阻塞好订阅**：并发拉取，单个失败只标记该订阅 `error`；
   拉取失败时**保留上一次可用版本**。
4. **未配置即隐身**：`ikun`/`subscription` 链路在未配置 Key/地址时，
   不出现在 `/healthz` 的 chains 中，也不参与能力上报——
   避免对用户谎称「可播放」。
5. **未命中不熔断**：404（所有订阅都解析不出）视为正常未命中；
   只有传输故障、401/403、429、5xx 才计入熔断。
6. **零 npm 依赖**：只用 Node 内置 `node:vm`/`node:http`/内置 `fetch`，
   无需 `npm install`，无供应链风险。

## 测试

```bash
cd lxsource-service && node test.js
```

31 项配置工具检查 + 15 项沙箱检查（均不联网）：订阅解析、沙箱隔离、动作调用、错误处理、多订阅容错。

lxmusic 侧回归（需 `pytest`）：

```bash
python -m pytest lxmusic-service/ -q   # 76 passed, 5 skipped
```

## 已知边界与风险

- **脚本能力差异**：不同洛雪脚本声明的平台/音质不同（如 `ikun` 无 `mg`）。
  本服务按脚本声明的 `qualitys` 过滤，不支持的组合直接跳过。
- **`vm` 不是安全边界**：`node:vm` 用于隔离意外全局污染，不是对抗恶意代码的
  安全沙箱。**只订阅你信任的脚本**。若要更强隔离，应改用独立进程 + 容器
  （本服务已按 `read_only` + `cap_drop: ALL` + `no-new-privileges` 运行）。
- **第三方可用性**：订阅的脚本后端（如 `c.wwwweb.top`）随时可能失效或限速。
  熔断器会自动旁路并回退到既有链路，不会出现「搜得到播不出」。
- **不解决搜索**：脚本只做解析。若某平台连搜索都失效，需在 `lxmusic-service`
  侧修；本服务无法补足。
- **版权**：仅限个人学习研究，请遵守当地法律与各平台服务条款。

## 文件清单

| 文件 | 说明 |
| :--- | :--- |
| `lxsource-service/shim.js` | `globalThis.lx` 沙箱实现 |
| `lxsource-service/subscription.js` | 订阅加载、哈希判定、多订阅回退 |
| `lxsource-service/server.js` | 零依赖 HTTP API |
| `lxsource-service/subscriptions_env.py` | 订阅配置读写（保留注释、原子写入、脱敏） |
| `lxsource-service/test_subscriptions_env.py` | 配置工具测试（31 项） |
| `lxsource-service/test.js` | 离线测试 |
| `lxsource-service/fixtures/example-source.js` | 测试用音源脚本 |
| `lxsource-service/Dockerfile` | node:22-alpine，非 root，只读根文件系统 |
| `docker-compose.lxsource.yml` | 单独启动用（也可用主 compose） |
| `patches/lxmusic-subscription.patch` | lxmusic-service 侧订阅链路补丁 |
