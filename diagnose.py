#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HomePod / AirPlay 设备发现诊断工具

当主程序报「自动发现未找到任何 AirPlay 设备」时，先跑这个：
    python3 diagnose.py                     # 全面诊断（自动发现，无需 IP）
    python3 diagnose.py --address 192.168.1.7   # 附带探测指定 IP（可选）

它会逐项检查并给出结论：
  1. 本机网卡与 IP（判断网段、多网卡）
  2. UDP 5353 是否被 avahi/mdnsresponder 占用（会导致组播响应收不到）
  3. 全协议 mDNS 自动发现结果：设备名 / IP / 服务 / 配对状态 / mDNS 属性
  4. 单独看 AirPlay 与 RAOP 两类服务的发现差异（HomePod mini 只宣告 AirPlay）
  5. 若给了 --address：TCP 7000 连通性 + 单播扫描
"""

import argparse
import asyncio
import ipaddress
import logging
import socket
import subprocess
import sys
from typing import List, Optional

import pyatv
from pyatv.const import Protocol

LOG = logging.getLogger("diagnose")


def section(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


# --------------------------------------------------------------------------- #
def show_interfaces() -> List[str]:
    section("1) 本机网络接口与 IP")
    ips: List[str] = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.strip().splitlines():
            parts = line.split()
            iface, ip = parts[1], parts[3].split("/")[0]
            if iface == "lo":
                continue
            ips.append(ip)
            print(f"  {iface:<12} {ip}")
    except Exception as exc:
        print(f"  (无法执行 ip 命令: {exc})")
    if not ips:
        try:
            ips = [socket.gethostbyname(socket.gethostname())]
            print(f"  fallback: {ips[0]}")
        except Exception:
            print("  未取到任何 IP")
    if len(ips) > 1:
        print("  ⚠ 多网卡：mDNS 查询可能从错误的接口发出，建议配置里指定 device.address")
    return ips


def check_mdns_port() -> None:
    section("2) UDP 5353 端口占用（mDNS）")
    holders = []
    try:
        for line in open("/proc/net/udp").read().splitlines()[1:]:
            fields = line.split()
            local = fields[1]
            port = int(local.split(":")[1], 16)
            if port == 5353:
                holders.append(fields[-1] if fields[-1] != "" else fields[9])
    except Exception as exc:
        print(f"  读取失败: {exc}")

    if not holders:
        print("  5353 空闲 —— 组播监听正常")
        return
    print(f"  ⚠ 5353 已被占用 (inode: {', '.join(holders)})，通常是 avahi-daemon / mDNSResponder")
    print("    组播响应会被占用进程截走，pyatv 可能收不到回应。")
    print("    处理：容器内 `--network host` 时停掉宿主的 avahi，或改用单播扫描/手动模式。")
    print("    Ubuntu/Debian: systemctl stop avahi-daemon && systemctl disable avahi-daemon")


def check_port(address: str, port: int = 7000) -> bool:
    section(f"3) TCP {address}:{port} 连通性")
    try:
        with socket.create_connection((address, port), timeout=3):
            print(f"  ✅ {address}:{port} 可连接（AirPlay 控制端口开放）")
            return True
    except Exception as exc:
        print(f"  ❌ {address}:{port} 连接失败: {exc}")
        print("     可能原因：IP 不对、防火墙拦截、HomePod 休眠/断网")
        return False


# --------------------------------------------------------------------------- #
def dump_devices(atvs, title: str) -> None:
    print(f"\n{title}: 发现 {len(atvs)} 台设备")
    if not atvs:
        return
    for cfg in atvs:
        model = getattr(getattr(cfg, "device_info", None), "model", None)
        model = getattr(model, "name", "?")
        print(f"\n  名称: {cfg.name}")
        print(f"  型号: {model}")
        print(f"  地址: {cfg.address}")
        print(f"  标识符: {', '.join(cfg.all_identifiers)}")
        for svc in cfg.services:
            print(
                f"    - {svc.protocol.name:<9} 端口={svc.port} "
                f"配对={getattr(svc, 'pairing_requirement', '?')} "
                f"凭据={'有' if svc.credentials else '无'}"
            )
        props = {p: s.properties for p, s in
                 ((s.protocol.name, s) for s in cfg.services)}
        for name, pr in props.items():
            if pr:
                print(f"      {name} 属性: {dict(list(pr.items())[:8])}")


async def run_scans(loop, address: Optional[str], timeout: int) -> None:
    section("3) mDNS 自动发现（全协议，主程序用的就是这一路）")
    try:
        atvs = await pyatv.scan(loop, timeout=timeout)
        dump_devices(atvs, "自动发现")
        if not atvs:
            print("\n  ❌ 自动发现为空。重点怀疑：")
            print("     - Docker 未使用 --network host（bridge 收不到组播）")
            print("     - 设备与本机不在同一网段/VLAN")
            print("     - 家庭 App 未开启『允许扬声器和电视访问 -> 所有人』")
            print("     - 5353 被 avahi 占用（见第 2 项）")
        else:
            homepods = [
                c for c in atvs
                if "HomePod" in (getattr(getattr(getattr(c, "device_info", None),
                                                "model", None), "name", "") or "")
                or "HomePod" in (c.name or "")
            ]
            if homepods:
                print(f"\n  ✅ 识别到 {len(homepods)} 台 HomePod，主程序会优先选它：")
                for c in homepods:
                    print(f"     - {c.name} @ {c.address}")
                if len(homepods) > 1:
                    print("     ⚠ 多台 HomePod，建议在 config.yaml 的 device.name 里写明唯一名称")
    except Exception as exc:
        print(f"  扫描异常: {exc}")

    section("4) 对照：仅扫 RAOP 服务（_raop._tcp.local）")
    try:
        raop = await pyatv.scan(loop, timeout=timeout, protocol=Protocol.RAOP)
        dump_devices(raop, "RAOP 扫描")
        if not raop:
            print("  ℹ 空属正常现象：HomePod mini 是 AirPlay 2『统一宣告』设备，")
            print("    只广播 _airplay._tcp.local，不单独广播 _raop._tcp.local。")
            print("    所以主程序必须同时扫 AirPlay + RAOP 才能发现它。")
    except Exception as exc:
        print(f"  扫描异常: {exc}")

    if address:
        section(f"5) 指定地址探测 {address}（可选，用于人工核对）")
        check_port(address)
        try:
            uni = await pyatv.scan(loop, timeout=timeout, hosts=[address])
            dump_devices(uni, "单播扫描")
            if not uni:
                print(f"  ❌ 单播到 {address}:5353 无响应，说明 mDNS 在链路上被阻断。")
        except Exception as exc:
            print(f"  单播扫描异常: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="AirPlay/HomePod 发现诊断")
    ap.add_argument("--address", help="可选：额外探测指定 IP 的 7000 端口与单播扫描")
    ap.add_argument("--timeout", type=int, default=8, help="每轮扫描超时秒数")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    from importlib.metadata import version as _v
    print("pyatv 版本:", _v("pyatv"))
    show_interfaces()
    check_mdns_port()
    if args.address:
        try:
            ipaddress.IPv4Address(args.address)
        except ValueError:
            print(f"\n⚠ 地址不合法: {args.address}")
            return 2

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_scans(loop, args.address, args.timeout))
    finally:
        loop.close()

    section("结论")
    print("  把上面的输出贴给我即可定位。最常见的三类原因：")
    print("   A. Docker 没加 --network host（mDNS 组播收不到）")
    print("   B. 只扫了 RAOP 协议（HomePod mini 需连同 AirPlay 一起扫）")
    print("   C. 家庭 App 未允许『所有人』访问扬声器")
    return 0


if __name__ == "__main__":
    sys.exit(main())
