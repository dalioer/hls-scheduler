#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HLS(m3u8) 定时推流调度器 —— 按配置把直播流推送到局域网 HomePod mini

链路：m3u8 -> ffmpeg(实时转 MP3) -> pyatv/miniaudio(解码 + ALAC) -> RAOP -> HomePod
  * miniaudio 不解析 HLS 也没有 AAC 解码器，所以必须先经 ffmpeg 转成 MP3；
  * MP3 是唯一在「不可 seek 的管道流」下也能被 pyatv 稳定接收的格式。

能力：
  1. 多组时间段（跨天、按星期过滤），到点自动起播、到点自动停播；
  2. 配置文件热加载：改 yaml 立即生效，无需重启容器/进程；
  3. 声音设置：全局音量 + 单流音量覆盖 + ffmpeg 增益(volume_boost)；
  4. 命令行 --url 可临时推一条流（忽略时间段）；--probe 可做开播前解码体检。

运行：
  python3 hls_scheduler.py --config config.yaml
Docker（mDNS 必须用 host 网络）：
  docker run -d --network host -v $PWD/config.yaml:/app/config.yaml:ro <image>
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

import pyatv
from pyatv.const import FeatureName, FeatureState, Protocol
from pyatv.interface import MediaMetadata

LOG = logging.getLogger("hls2homepod")

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
@dataclass
class Schedule:
    start: str
    end: str
    weekdays: Optional[List[int]] = None  # 1=周一 ... 7=周日；None/空=每天

    def minutes(self):
        def to_min(s: str) -> int:
            h, m = str(s).split(":")[:2]
            return int(h) * 60 + int(m)

        return to_min(self.start), to_min(self.end)

    @property
    def start_time(self) -> dtime:
        h, m = str(self.start).split(":")[:2]
        return dtime(int(h), int(m))

    def matches(self, now: datetime) -> bool:
        if self.weekdays and now.isoweekday() not in self.weekdays:
            return False
        start, end = self.minutes()
        cur = now.hour * 60 + now.minute
        if start <= end:            # 同一天内
            return start <= cur < end
        return cur >= start or cur < end  # 跨天，如 23:30-00:30


@dataclass
class StreamSpec:
    name: str
    url: str
    schedules: List[Schedule] = field(default_factory=list)
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    volume: Optional[float] = None        # 覆盖设备默认音量
    volume_boost: Optional[float] = None  # ffmpeg 端增益，如 1.5
    bitrate: Optional[str] = None
    referer: Optional[str] = None
    user_agent: Optional[str] = None
    reconnect_delay: float = 3.0
    priority: int = 0
    enabled: bool = True

    def fingerprint(self) -> str:
        """用于热加载时判断该流配置是否被改动（改动则重启推流）。"""
        return repr(
            (
                self.url, self.title, self.artist, self.album,
                self.volume, self.volume_boost, self.bitrate,
                self.referer, self.user_agent, self.reconnect_delay,
            )
        )


@dataclass
class Config:
    # 纯自动发现：靠 mDNS 组播，不需要填 IP
    device_name: Optional[str] = None      # 按名称模糊匹配（可选）
    device_id: Optional[str] = None        # 按 identifier 精确匹配（可选）
    raop_password: Optional[str] = None
    scan_timeout: int = 5
    scan_retries: int = 3
    rescan_interval: int = 300             # 空闲时重新发现设备的间隔（秒）
    volume: Optional[float] = None
    volume_boost: Optional[float] = None
    bitrate: str = "192k"
    referer: Optional[str] = None
    user_agent: str = DEFAULT_UA
    reconnect_delay: float = 3.0
    timezone: str = "Asia/Shanghai"
    streams: List[StreamSpec] = field(default_factory=list)

    def resolve(self, spec: StreamSpec) -> StreamSpec:
        """把全局默认值合并进单条流（命令行/单流字段优先）。"""
        merged = StreamSpec(
            name=spec.name,
            url=spec.url,
            schedules=spec.schedules,
            title=spec.title or spec.name,
            artist=spec.artist or "直播流",
            album=spec.album or spec.url,
            volume=spec.volume if spec.volume is not None else self.volume,
            volume_boost=spec.volume_boost if spec.volume_boost is not None else self.volume_boost,
            bitrate=spec.bitrate or self.bitrate,
            referer=spec.referer if spec.referer is not None else self.referer,
            user_agent=spec.user_agent or self.user_agent,
            reconnect_delay=spec.reconnect_delay or self.reconnect_delay,
        )
        return merged

    def fingerprint(self) -> str:
        return repr(
            (self.device_name, self.device_id, self.raop_password,
             self.volume, self.volume_boost, self.bitrate)
        )


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    dev = raw.get("device") or {}
    dflt = raw.get("defaults") or {}

    streams: List[StreamSpec] = []
    for item in raw.get("streams") or []:
        streams.append(
            StreamSpec(
                name=item.get("name") or item.get("url", "unnamed"),
                url=item["url"],
                schedules=[Schedule(**s) for s in (item.get("schedules") or [])],
                title=item.get("title"),
                artist=item.get("artist"),
                album=item.get("album"),
                volume=item.get("volume"),
                volume_boost=item.get("volume_boost"),
                bitrate=item.get("bitrate"),
                referer=item.get("referer"),
                user_agent=item.get("user_agent"),
                reconnect_delay=float(item.get("reconnect_delay") or 0) or None,  # type: ignore
                priority=int(item.get("priority") or 0),
                enabled=bool(item.get("enabled", True)),
            )
        )

    cfg = Config(
        device_name=dev.get("name"),
        device_id=dev.get("id"),
        raop_password=dev.get("raop_password"),
        scan_timeout=int(dev.get("scan_timeout", 5)),
        scan_retries=int(dev.get("scan_retries", 3)),
        rescan_interval=int(dev.get("rescan_interval", 300)),
        volume=dev.get("volume"),
        volume_boost=dev.get("volume_boost"),
        bitrate=dflt.get("bitrate", "192k"),
        referer=dflt.get("referer"),
        user_agent=dflt.get("user_agent", DEFAULT_UA),
        reconnect_delay=float(dflt.get("reconnect_delay", 3.0)),
        timezone=dflt.get("timezone") or raw.get("timezone") or "Asia/Shanghai",
        streams=streams,
    )
    # None 兜底
    for s in cfg.streams:
        if not s.reconnect_delay:
            s.reconnect_delay = cfg.reconnect_delay
    return cfg


# --------------------------------------------------------------------------- #
# ffmpeg 参数
# --------------------------------------------------------------------------- #
def ffmpeg_input_args(url: str, user_agent: str, referer: Optional[str]) -> List[str]:
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
        "-user_agent", user_agent,
    ]
    if referer:
        args += ["-headers", f"Referer: {referer}\r\n"]
    args += ["-i", url]
    return args


def ffmpeg_output_args(bitrate: str, volume_boost: Optional[float]) -> List[str]:
    args = ["-vn"]
    filters = ["aresample=async=1:first_pts=0"]
    if volume_boost:
        filters.append(f"volume={volume_boost}")
    args += ["-af", ",".join(filters)]
    # MP3：管道(不可 seek)场景下唯一被 pyatv 稳定支持的格式
    args += ["-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100", "-ac", "2"]
    args += ["-f", "mp3", "-"]
    return args


# --------------------------------------------------------------------------- #
# 播放器：一个 StreamSpec 对应一个长跑任务
# --------------------------------------------------------------------------- #
class Player:
    def __init__(self, spec: StreamSpec, cfg: Config, loop: asyncio.AbstractEventLoop):
        self.spec = spec
        self.cfg = cfg
        self.loop = loop
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._atv: Optional[Any] = None

    async def discover(self):
        # 纯自动发现：mDNS 组播扫描，无需任何 IP。
        # 关键：HomePod mini 属于 AirPlay 2 的 "统一宣告"(HasUnifiedAdvertiserInfo)，
        # 只宣告 _airplay._tcp.local，不会单独宣告 _raop._tcp.local。
        # 若只扫 Protocol.RAOP 会扫不到 —— 必须同时扫 AirPlay，
        # pyatv 会在 AirPlay 服务上自动补出一个 RAOP 服务。
        protocols = {Protocol.RAOP, Protocol.AirPlay}

        atvs: List[Any] = []
        for attempt in range(1, self.cfg.scan_retries + 1):
            atvs = await pyatv.scan(
                self.loop,
                timeout=self.cfg.scan_timeout,
                protocol=protocols,
                identifier=self.cfg.device_id or None,
            )
            atvs = [c for c in atvs
                    if c.get_service(Protocol.RAOP) or c.get_service(Protocol.AirPlay)]
            if atvs:
                break
            LOG.warning("第 %d/%d 次扫描未发现设备，重试 ...",
                        attempt, self.cfg.scan_retries)
            await asyncio.sleep(1)

        if not atvs:
            raise RuntimeError(
                "自动发现未找到任何 AirPlay 设备。请依次排查：\n"
                "  1) Docker 是否用 --network host（bridge 收不到 mDNS 组播）；\n"
                "  2) 宿主机跑 'atvremote scan' 验证能否发现设备；\n"
                "  3) 家庭 App -> 家庭设置 -> 允许扬声器和电视访问 -> 设为『所有人』；\n"
                "  4) 电脑与 HomePod 同一网段，UDP 5353 / TCP 7000 未被防火墙或 VLAN 隔离；\n"
                "  5) UDP 5353 未被 avahi-daemon 占用（会截走组播响应）。\n"
                "  详细诊断：python3 diagnose.py"
            )

        def hit(c) -> bool:
            if self.cfg.device_id and self.cfg.device_id.lower() not in [
                i.lower() for i in c.all_identifiers
            ]:
                return False
            if self.cfg.device_name and self.cfg.device_name.lower() not in (c.name or "").lower():
                return False
            return True

        scored = [c for c in atvs if hit(c)]
        if not scored:
            LOG.warning(
                "未匹配到 name=%s 的设备，已发现的: %s",
                self.cfg.device_name,
                ", ".join(f"{c.name}({c.address})" for c in atvs),
            )
            scored = atvs

        # 优先挑名字里带 HomePod 的，其次挑有独立 RAOP 服务的，最后取第一台
        def rank(c) -> tuple:
            model = getattr(getattr(c, "device_info", None), "model", None)
            model_name = getattr(model, "name", "") or ""
            is_homepod = "HomePod" in model_name or "HomePod" in (c.name or "")
            has_raop = c.get_service(Protocol.RAOP) is not None
            return (not is_homepod, not has_raop)

        scored.sort(key=rank)
        picked = scored[0]
        svcs = [s.protocol.name for s in picked.services]
        LOG.info("自动发现选中: %s @ %s (%s) 服务=%s",
                 picked.name, picked.address,
                 getattr(picked.device_info.model, "name", "?"), svcs)
        if len(atvs) > 1:
            LOG.info("局域网内共发现 %d 台设备，可在 device.name 中指定唯一名称", len(atvs))
        return picked

    async def connect(self):
        conf = await self.discover()
        if self.cfg.raop_password:
            svc = conf.get_service(Protocol.RAOP)
            if svc:
                svc.password = self.cfg.raop_password
        self._atv = await pyatv.connect(conf, self.loop)
        state = self._atv.features.get_feature(FeatureName.StreamFile).state
        LOG.info("已连接，StreamFile 特性: %s", state)
        if state == FeatureState.Unavailable:
            LOG.warning("设备不支持 stream_file，请检查家庭 App 的访问授权设置")
        return self._atv

    async def set_volume(self):
        if self.spec.volume is None or self._atv is None:
            return
        try:
            await self._atv.audio.set_volume(float(self.spec.volume))
            LOG.info("音量设置为 %.0f%%", self.spec.volume)
        except Exception as exc:
            LOG.warning("设置音量失败（可忽略）: %s", exc)

    async def _spawn(self):
        args = ffmpeg_input_args(self.spec.url, self.spec.user_agent, self.spec.referer)
        args += ffmpeg_output_args(self.spec.bitrate, self.spec.volume_boost)
        LOG.info("启动 ffmpeg: %s", " ".join(args[:6]) + " ... " + self.spec.url)
        self._proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=None
        )
        return self._proc

    async def run(self):
        """长跑：断流自动重连 ffmpeg，被 cancel 时优雅收尾。"""
        try:
            await self.connect()
            await self.set_volume()
            metadata = MediaMetadata(
                title=self.spec.title, artist=self.spec.artist, album=self.spec.album
            )
            while True:
                try:
                    await self._spawn()
                    assert self._proc and self._proc.stdout
                    await self._atv.stream.stream_file(self._proc.stdout, metadata=metadata)
                    LOG.warning("推流结束(ffmpeg 退出 rc=%s)，%ss 后重连",
                                self._proc.returncode, self.spec.reconnect_delay)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("推流异常: %s，%ss 后重试", exc, self.spec.reconnect_delay)
                finally:
                    await self._kill_ffmpeg()
                await asyncio.sleep(self.spec.reconnect_delay)
        except asyncio.CancelledError:
            LOG.info("停止推流: %s", self.spec.name)
            raise
        finally:
            await self._kill_ffmpeg()
            await self._close_atv()

    async def _kill_ffmpeg(self):
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None

    async def _close_atv(self):
        if self._atv is not None:
            try:
                pending = self._atv.close()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            except Exception as exc:
                LOG.debug("关闭连接时出错: %s", exc)
            self._atv = None


# --------------------------------------------------------------------------- #
# 调度器
# --------------------------------------------------------------------------- #
class Scheduler:
    def __init__(self, config_path: Path, once_url: Optional[str] = None,
                 once_volume: Optional[float] = None):
        self.path = config_path
        self.once_url = once_url
        self.once_volume = once_volume
        self.cfg: Optional[Config] = None
        self._mtime: float = 0.0
        self._task: Optional[asyncio.Task] = None
        self._task_spec: Optional[StreamSpec] = None
        self._fail_streak: int = 0
        self._next_try_at: float = 0.0

    # ---------- 配置 ----------
    def _reload_if_changed(self) -> bool:
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime and self.cfg is not None:
            return False
        try:
            cfg = load_config(self.path)
        except Exception as exc:
            LOG.error("配置文件解析失败，沿用旧配置: %s", exc)
            self._mtime = mtime
            return False
        old = self.cfg
        self.cfg = cfg
        self._mtime = mtime
        LOG.info("配置已加载(%s): %d 条流", self.path, len(cfg.streams))
        return old is not None and old.fingerprint() != cfg.fingerprint()

    def now(self) -> datetime:
        tz = None
        if ZoneInfo and self.cfg:
            try:
                tz = ZoneInfo(self.cfg.timezone)
            except Exception:
                tz = None
        return datetime.now(tz)

    def pick(self, now: datetime) -> Optional[StreamSpec]:
        """选出当前时间段内应播放的流；多条命中按 priority 排序取第一条。"""
        if not self.cfg:
            return None
        active = [
            s for s in self.cfg.streams
            if s.enabled and any(sch.matches(now) for sch in s.schedules)
        ]
        if not active:
            return None
        active.sort(key=lambda s: -s.priority)
        return self.cfg.resolve(active[0])

    def next_run(self, now: datetime):
        """下一个将要开播的时间点，仅用于日志提示。"""
        best = None
        for s in self.cfg.streams if self.cfg else []:
            if not s.enabled:
                continue
            for sch in s.schedules:
                for offset in range(8):
                    day = (now + timedelta(days=offset)).date()
                    cand = datetime.combine(day, sch.start_time)
                    if now.tzinfo:
                        cand = cand.replace(tzinfo=now.tzinfo)
                    if cand > now and (not sch.weekdays or cand.isoweekday() in sch.weekdays):
                        if best is None or cand < best[1]:
                            best = (s.name, cand)
                        break
        return best

    # ---------- 任务 ----------
    async def _stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task, self._task_spec = None, None

    async def _start(self, spec: StreamSpec, loop):
        await self._stop()
        player = Player(spec, self.cfg, loop)
        self._task = loop.create_task(player.run(), name=f"play:{spec.name}")
        self._task_spec = spec
        LOG.info("▶ 开始推流: %s -> %s", spec.name, spec.url)

    async def _reap(self):
        """任务自己退出(异常)时清理状态，下个 tick 会重新起播。"""
        if self._task and self._task.done():
            self._fail_streak += 1
            try:
                self._task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                LOG.error("推流任务异常退出: %s", exc)
            self._task, self._task_spec = None, None

    # ---------- 主循环 ----------
    async def serve(self, loop):
        self._reload_if_changed()
        last_idle_log = 0.0
        while True:
            await self._reap()
            device_changed = self._reload_if_changed()
            now = self.now()

            if self.once_url:  # 命令行临时推流模式
                desired = self.cfg.resolve(
                    StreamSpec(name="CLI", url=self.once_url,
                               volume=self.once_volume if self.once_volume is not None
                               else self.cfg.volume)
                )
            else:
                desired = self.pick(now)

            if desired is None:
                if self._task_spec is not None:
                    LOG.info("⏹ 当前无匹配时间段，停止推流")
                    await self._stop()
                if time.time() - last_idle_log > 300:  # 空闲时 5 分钟提示一次
                    last_idle_log = time.time()
                    nxt = self.next_run(now)
                    LOG.info("空闲中%s", f"，下一次: {nxt[0]} @ {nxt[1]:%m-%d %H:%M}" if nxt else "，无待播任务")
            else:
                need_switch = (
                    self._task_spec is None
                    or self._task_spec.fingerprint() != desired.fingerprint()
                    or self._task_spec.name != desired.name
                    or device_changed
                )
                if need_switch:
                    if time.time() < self._next_try_at:
                        await asyncio.sleep(1)
                        continue
                    LOG.info("▶ 切换到: %s (%s) 音量=%s",
                             desired.name, desired.url, desired.volume)
                    await self._start(desired, loop)
                    # 起播后等一小会儿，若任务立刻失败(设备没找到)就进入退避
                    try:
                        await asyncio.wait_for(asyncio.shield(self._task), timeout=3)
                    except asyncio.TimeoutError:
                        self._fail_streak = 0
                        self._next_try_at = 0.0
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    if self._task is None or self._task.done():
                        backoff = min(10 * 2 ** min(self._fail_streak, 5), 300)
                        self._next_try_at = time.time() + backoff
                        LOG.warning("起播失败，%ds 后重新自动发现设备（第 %d 次）",
                                    backoff, self._fail_streak)

            await asyncio.sleep(1)


# --------------------------------------------------------------------------- #
# 开播前体检（可选）
# --------------------------------------------------------------------------- #
async def probe(url: str, user_agent: str, referer: Optional[str],
                bitrate: str, seconds: int = 3) -> int:
    import miniaudio

    args = ffmpeg_input_args(url, user_agent, referer) + ["-t", str(seconds)]
    args += ffmpeg_output_args(bitrate, None)
    LOG.info("体检：抓取 %ss 验证流可用 ...", seconds)
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0 or not out:
        LOG.error("拉流/转码失败（防盗链 403、URL 失效或 ffmpeg 缺 libmp3lame）:\n%s",
                  err.decode("utf-8", "ignore").strip()[-800:])
        return 1
    try:
        decoded = miniaudio.decode(
            out, output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=2, sample_rate=44100,
        )
    except Exception as exc:
        LOG.error("miniaudio 解码失败: %s", exc)
        return 1
    LOG.info("体检通过: %.1fKB MP3 -> %dch/%dHz（pyatv 与 HomePod 均可识别）",
             len(out) / 1024, decoded.nchannels, decoded.sample_rate)
    return 0


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="按时间段把 HLS 流推送到 HomePod mini")
    parser.add_argument("--config", default=os.environ.get("CONFIG", "./config.yaml"),
                        help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument("--url", help="临时推流：忽略时间段立即播放该 m3u8")
    parser.add_argument("--volume", type=float, help="配合 --url 指定音量(0-100)")
    parser.add_argument("--probe", action="store_true", help="对 --url 做开播前解码体检后退出")
    parser.add_argument("--bitrate", default="192k")
    parser.add_argument("--referer", help="防盗链 Referer")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    path = Path(args.config)
    if not path.exists():
        LOG.error("配置文件不存在: %s", path)
        return 2

    if args.probe:
        if not args.url:
            LOG.error("--probe 需要配合 --url")
            return 2
        return asyncio.run(
            probe(args.url, DEFAULT_UA, args.referer, args.bitrate)
        )

    loop = asyncio.new_event_loop()
    sched = Scheduler(path, once_url=args.url, once_volume=args.volume)

    async def runner():
        task = loop.create_task(sched.serve(loop))
        stop = asyncio.Event()

        def _signal():
            LOG.info("收到退出信号，正在停止 ...")
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal)
            except NotImplementedError:
                signal.signal(sig, lambda *_: _signal())
        await stop.wait()
        await sched._stop()  # noqa: SLF001
        task.cancel()

    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(runner())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
