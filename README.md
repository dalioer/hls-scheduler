# HLS → HomePod 定时推流

按时间段把 HLS（m3u8）直播流自动推送到局域网里的 HomePod / HomePod mini，支持多组时段、配置热加载、音量控制，可用 Docker 一键部署。

设备**靠 mDNS 自动发现**，不需要填 IP。

---

## 一、它解决什么问题

手上有一条网络电台/电视直播的 m3u8 地址，想在每天的固定时间自动用 HomePod 播放——比如早上 7 点自动放 30 分钟新闻。本项目就是这么一个常驻调度器。

**音频链路**

```
m3u8(HLS/AAC) → ffmpeg 实时转 MP3 → 管道 → pyatv/miniaudio 解码 + ALAC 编码 → RAOP → HomePod
```

为什么必须有 ffmpeg 这一环：

- miniaudio 只是音频解码库，既不解析 HLS 播放列表，也没有 AAC 解码器；
- pyatv 的 `stream_file` 只接受 miniaudio 能解的格式：MP3 / WAV / FLAC / OGG；
- 管道是不可 seek 的流，这四种里**只有 MP3 稳定**（WAV/OGG/FLAC 需要 seek）。

所以 ffmpeg 负责「拉 HLS + 转 MP3」，pyatv 负责「解码 + 推 RAOP」。

---

## 二、快速开始

### 前置条件

1. **HomePod 侧**：iPhone 打开「家庭」App → 家庭设置 → 允许扬声器和电视访问 → 设为**「所有人」**。改完重启 HomePod。这一步不做，RAOP 会显示 `Pairing: Disabled`，推流必然失败。
2. **网络**：运行推流的机器与 HomePod 在同一网段，UDP 5353、TCP 7000 未被防火墙或 VLAN 隔离。
3. **依赖**：Python 3.9+，ffmpeg（需含 `libmp3lame`）。

### 本地运行

```bash
pip install -r requirements.txt
cp config.yaml my.yaml    # 按需修改
python3 hls_scheduler.py --config my.yaml
```

### Docker 运行（推荐）

```bash
docker build -t hls2homepod .

docker run -d --name hls2homepod \
  --network host \
  --restart unless-stopped \
  -v $PWD/config.yaml:/app/config.yaml:ro \
  -e TZ=Asia/Shanghai \
  hls2homepod
```

> `--network host` 是**必须的**。AirPlay 设备靠 mDNS 组播发现，bridge 网络收不到组播包，自动发现会永远为空。

镜像内已打包 ffmpeg（含 libmp3lame）、tzdata、pyatv、miniaudio、PyYAML。

---

## 三、配置说明

改完 `config.yaml` **立即生效**，进程和容器都不用重启（主循环每秒比对文件 mtime）。

```yaml
device:
  name: null          # 设备名模糊匹配，多台设备时填，如「客厅」
  id: null            # 或用 identifier 精确匹配（atvremote scan 可查）
  raop_password: null # 家庭 App 给 AirPlay 设了密码才填
  volume: 40          # 全局默认音量 0-100
  volume_boost: null  # ffmpeg 端增益倍数，如 1.5
  scan_timeout: 5
  scan_retries: 3
  rescan_interval: 300

defaults:
  bitrate: 192k
  referer: null       # 防盗链 Referer
  user_agent: "Mozilla/5.0 ..."
  reconnect_delay: 3  # 断流后重连间隔（秒）
  timezone: Asia/Shanghai

streams:
  - name: CCTV-10 科教
    url: https://xxx/cctv10_2.m3u8
    title: CCTV-10 科教   # HomePod 上显示的标题
    volume: 35            # 覆盖 device.volume
    volume_boost: null
    bitrate: 192k
    referer: null
    priority: 10          # 多流同时命中时，大的优先
    enabled: true
    schedules:
      - start: "07:00"
        end: "07:30"
        weekdays: [1,2,3,4,5]   # 1=周一 … 7=周日；省略=每天
      - start: "23:30"
        end: "00:30"            # 跨天自动识别
```

### 字段速查

| 字段 | 位置 | 说明 |
|---|---|---|
| `device.name` / `device.id` | device | 选设备用；都留空则自动选，优先挑 HomePod |
| `device.volume` | device | 全局音量，0-100 |
| `device.volume_boost` | device | 增益倍数，音源偏小时用 1.3~1.8 |
| `defaults.timezone` | defaults | 时间段判定时区 |
| `streams[].url` | streams | m3u8 地址 |
| `streams[].volume` | streams | 覆盖全局音量 |
| `streams[].priority` | streams | 多条流同时命中时的优先级 |
| `streams[].schedules[].weekdays` | streams | 星期过滤，1=周一 |

### 设备选择策略

1. 配了 `id` → 按 identifier 精确匹配；
2. 配了 `name` → 按名称模糊匹配；
3. 都没配 → 自动选：**优先挑 HomePod**（型号或名称含 HomePod），其次挑有独立 RAOP 服务的设备。

局域网内同时有 Apple TV 和 HomePod 时不会推错。

---

## 四、命令行

```bash
# 常驻调度（主用法）
python3 hls_scheduler.py --config config.yaml

# 临时推一条流，忽略时间段
python3 hls_scheduler.py --url https://xxx/live.m3u8 --volume 40

# 开播前体检：抓 3 秒验证流能拉到、能转码、miniaudio 能解码
python3 hls_scheduler.py --url https://xxx/live.m3u8 --probe

# 扫不到设备时跑诊断
python3 diagnose.py
```

| 参数 | 说明 |
|---|---|
| `--config` | 配置文件路径，默认 `./config.yaml`，也可用环境变量 `CONFIG` |
| `--url` | 临时推流，忽略时间段 |
| `--volume` | 配合 `--url` 指定音量 |
| `--bitrate` | MP3 码率，默认 192k |
| `--referer` | 防盗链 Referer |
| `--probe` | 解码体检后退出 |
| `--debug` | 输出 DEBUG 日志 |

---

## 五、功能细节

**定时调度**：支持每组流配置多段时间、按星期过滤、跨天时段（如 23:30→次日 00:30）。到点自动起播，时段结束自动停播。多条流同时命中时按 `priority` 取最高。

**配置热加载**：改配置即刻生效。改了 URL / 音量 / 码率等推流参数会自动停旧流起新流；改了设备选择或全局音量同样会重建连接。配置文件写错时保留旧配置并打日志，不会让服务挂掉。

**声音控制**三层，后者覆盖前者：

1. `device.volume` 全局音量；
2. `streams[].volume` 单流覆盖；
3. `volume_boost` ffmpeg 端增益（应对音源本身音量偏小）。

音量在起播时经 pyatv 的 `audio.set_volume` 下发到 HomePod。

**断流自愈**：ffmpeg 进程退出或推流异常会自动重连，间隔由 `reconnect_delay` 控制。设备暂时找不到时按指数退避重试（10s → 20s → …上限 300s），设备上线后自动捞回，不会疯狂刷组播。

**优雅退出**：收到 SIGINT / SIGTERM 会先停 ffmpeg、关闭 RAOP 连接再退出。

---

## 六、排障

### 扫不到设备

先跑诊断，它会逐项输出本机 IP、UDP 5353 占用情况、自动发现结果、设备型号与配对状态：

```bash
python3 diagnose.py
```

最常见三类原因：

| 现象 | 原因 | 处理 |
|---|---|---|
| Docker 里扫不到，宿主机 `atvremote scan` 能扫到 | bridge 网络收不到组播 | 加 `--network host` |
| 5353 被占用 | avahi-daemon / mDNSResponder 截走组播响应 | `systemctl stop avahi-daemon` |
| 设备可见但推流报错 | 家庭 App 未授权 | 允许扬声器和电视访问 → 所有人 |

另一个坑值得单独说：**HomePod mini 是 AirPlay 2「统一宣告」设备**，只广播 `_airplay._tcp.local`，不单独广播 `_raop._tcp.local`。只扫 RAOP 协议会返回空，必须连同 AirPlay 一起扫——pyatv 会自动从 AirPlay 服务补出 RAOP 服务。本项目的扫描已经同时包含这两个协议。

### 流拉不到（403 / 拉流失败）

先用体检确认：`python3 hls_scheduler.py --url <地址> --probe`

若返回 403，多半是 CDN 防盗链，在配置里补 `referer` 和 `user_agent`（从浏览器 Network 面板里抄）。

### 有声音但延迟大

约 2 秒缓冲延迟属正常，是 pyatv 的缓冲机制，无法消除。

---

## 七、文件说明

| 文件 | 作用 |
|---|---|
| `hls_scheduler.py` | 主程序：调度器、设备发现、推流长跑任务、热加载 |
| `config.yaml` | 配置文件示例 |
| `diagnose.py` | 设备发现诊断工具 |
| `Dockerfile` | 打包 ffmpeg + pyatv + miniaudio |
| `requirements.txt` | Python 依赖 |

---

## 八、依赖

- [pyatv](https://pyatv.dev) — Apple TV / AirPlay 设备控制与 RAOP 音频流推送
- [miniaudio](https://github.com/pothosware/PyMiniAudio) — pyatv 内部用于音频解码
- ffmpeg — HLS 拉流与 MP3 转码
- PyYAML — 配置解析

经 pyatv 官方验证可用于 RAOP 音频流的设备包括：HomePod mini、Apple TV、AirPort Express、shairport-sync 等第三方接收端。
