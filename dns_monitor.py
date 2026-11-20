#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DNS-Monitor — 常用网站/App 对应域名 DNS 解析监测工具

功能：
  1. 同时做两套解析：
     - 系统解析：socket.getaddrinfo，反映你实际上网时用的本地 DNS 结果。
     - 干净解析：经代理走 DoH（Cloudflare/Google），拿到未被污染的基准结果。
  2. 两套结果对照，识别：
     - 本地 DNS 污染/拦截（系统解析失败，但干净解析可用）；
     - 解析到错误服务器（系统解析成功，但 IP 不落在该域名的预期 IP 段）；
     - 真实解析失败（系统与干净解析都失败）；
     - 解析结果变化（与上一轮系统解析对比）。
  3. 异常时桌面通知 + 日志提醒。支持持续监测 / 单次检查 / 指定域名等模式。

说明：
  - 只做「解析 + 提醒」，不写 hosts、不清缓存、不改系统，纯只读监测，安全无副作用。
  - App（*.exe）本身不产生 DNS 查询，无法直接解析出 IP，因此只作为「关注的 App」
    记录进配置（方便你对着清单核对），真正做解析监测的是 domains 域名列表。
  - 域名在配置里写成 *.google.com 这种通配形式时，会自动取基础域（google.com）来解析。
  - 为减少误报，对配置了「预期 IP 段」(domain_expect_cidr) 的域名，系统解析的 IP
    必须落在该段内才算正常；CDN 轮询导致的系统 IP 与干净基准无交集属正常，不判污染。

用法：
  python dns_monitor.py               # 持续监测（默认每 60s 一轮，异常自动提醒）
  python dns_monitor.py --status      # 只看一次状态（只读，不提醒）
  python dns_monitor.py --once        # 只检查一轮（同 --status）
  python dns_monitor.py --domains google.com github.com   # 只监测指定域名
  python dns_monitor.py --interval 30   # 自定义监测间隔（秒）
  python dns_monitor.py --no-notify    # 持续监测但不弹桌面通知（只写日志）
  python dns_monitor.py --no-doh      # 不走代理 DoH，只用系统解析（代理没开时可临时用）
"""

import argparse
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime

# PyInstaller 打包（onefile）后 __file__ 指向临时解压目录，
# 改用 exe 所在目录，保证 config / domains / 日志 / 状态都跟随 exe。
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# Windows 控制台默认 GBK，输出 emoji 状态符号会 UnicodeEncodeError；
# 强制 stdout/stderr 走 UTF-8 并容错替换（exe 打包后在中文系统上必需）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Google 系常用段（用于给 Google 相关域名做「预期段」判断）
GOOGLE_NETS = [
    ipaddress.ip_network(c, strict=False) for c in [
        "142.250.0.0/15", "172.217.0.0/16", "172.218.0.0/16", "74.125.0.0/16",
        "64.233.160.0/19", "216.58.192.0/19", "216.58.216.0/21",
        "108.177.97.0/24", "108.177.98.0/24", "108.177.99.0/24", "108.177.100.0/23",
        "173.194.0.0/16", "70.32.128.0/17", "72.14.192.0/18", "72.14.208.0/20",
        "72.14.224.0/19", "103.10.64.0/18", "103.24.128.0/17",
        "104.108.64.0/18", "104.154.128.0/17",
    ]
]

# Cloudflare 共享段（很多站点走 Cloudflare，IP 变化属正常轮询）
CLOUDFLARE_NETS = [
    ipaddress.ip_network(c, strict=False) for c in [
        "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "188.114.96.0/20",
    ]
]


# ============ 基础工具 ============

def _resolve_file(cfg, key, default):
    """把配置里的相对路径解析为绝对路径。"""
    p = cfg.get(key, default)
    if not p:
        return None
    if not os.path.isabs(p):
        p = os.path.join(SCRIPT_DIR, p)
    return p


def load_line_list(path):
    """从文本文件读取清单（每行一个，忽略空行与 # 注释）。"""
    items = []
    if not path or not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(line)
    return items


def load_config(config_path=None):
    with open(config_path or CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 本地化配置覆盖（config.local.json 已被 .gitignore 屏蔽，不上传，用于保留个人代理等）
    local_path = os.path.join(SCRIPT_DIR, "config.local.json")
    if os.path.exists(local_path):
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                local_cfg = json.load(f)
            cfg.update(local_cfg)
        except Exception:
            pass
    for key in ("log_path",):
        if key in cfg:
            cfg[key] = cfg[key].replace("/", "\\")
    cfg.setdefault("proxy", "")
    cfg.setdefault("doh_server", "https://cloudflare-dns.com/dns-query")
    cfg.setdefault("doh_fallback", "https://dns.google/resolve")
    cfg.setdefault("compare_with_doh", True)

    # 域名 / App / 预期段从独立文件加载（便于单独维护、不被配置文件结构绑定）
    domains_file = _resolve_file(cfg, "domains_file", "domains/monitor_domains.txt")
    apps_file = _resolve_file(cfg, "apps_file", "apps/apps.txt")
    expect_file = _resolve_file(cfg, "expect_cidr_file", "domains/domain_expect_cidr.json")

    cfg["domains"] = load_line_list(domains_file)
    cfg["apps"] = load_line_list(apps_file)

    # 编译域名预期 IP 段（从独立 JSON 文件加载）
    cfg["_expect_nets"] = {}
    if expect_file and os.path.exists(expect_file):
        try:
            with open(expect_file, "r", encoding="utf-8") as f:
                cidr_map = json.load(f)
            for dom, cidr_list in cidr_map.items():
                try:
                    cfg["_expect_nets"][dom] = [
                        ipaddress.ip_network(c, strict=False) for c in cidr_list
                    ]
                except Exception:
                    pass
        except Exception:
            pass
    return cfg


def get_log_path(cfg):
    lp = cfg.get("log_path", "dns_monitor.log")
    if not os.path.isabs(lp):
        lp = os.path.join(SCRIPT_DIR, lp)
    return lp


def get_alerts_jsonl_path():
    """异常记录 JSONL 路径（状态窗口/导出提示词的数据源）。"""
    return os.path.join(SCRIPT_DIR, "dns_monitor_alerts.jsonl")


def append_alerts_jsonl(alerts, cfg):
    """把本轮触发的异常记录追加写入 JSONL（每行一个 JSON 对象）。"""
    if not alerts:
        return
    try:
        with open(get_alerts_jsonl_path(), "a", encoding="utf-8") as f:
            for r in alerts:
                f.write(json.dumps({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "domain": r["domain"],
                    "status": r["status"],
                    "reason": r["reason"],
                    "sys_ips": r["sys_ips"],
                    "clean_ips": r["clean_ips"],
                    "sys_error": r["sys_error"],
                    "clean_error": r["clean_error"],
                }, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"  ⚠ 异常记录写入失败：{e}", cfg)


def load_alerts_jsonl(limit=500):
    """读取异常记录（时间倒序，最多 limit 条），返回 list[dict]。"""
    path = get_alerts_jsonl_path()
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return list(reversed(rows[-limit:]))


def log(msg, cfg=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if cfg is not None:
        try:
            with open(get_log_path(cfg), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def notify(title, message, cfg):
    """发 Windows 桌面通知；失败静默降级为只记日志。"""
    if not cfg.get("notify", True):
        return
    try:
        from winotify import Notification, audio
    except ImportError:
        log(f"  [通知] {title}：{message}（未安装 winotify，已降级为日志）", cfg)
        return
    try:
        n = Notification(
            app_id="DNS-Monitor",
            title=title,
            msg=message,
            duration="long",
        )
        # winotify >= 1.0 用 set_audio(枚举)；旧写法 audio.DefaultAudio() 已移除
        try:
            n.set_audio(audio.Default, loop=False)
        except AttributeError:
            pass  # 极老版本无音效 API，跳过
        n.show()
    except Exception as e:
        log(f"  [通知] 发送失败：{e}（已降级为日志）", cfg)


def ip_in_nets(ip_str, nets):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def is_cloudflare_ip(ip_str):
    return ip_in_nets(ip_str, CLOUDFLARE_NETS)


# ============ DNS 解析 ============

def resolve_system(domain, timeout=8):
    """用系统解析器解析 IPv4，带超时保护。返回 (ips, latency_ms, error)。

    用 daemon 线程包一层：getaddrinfo 本身无法设超时，Windows DNS 慢/重试时
    可能挂起数分钟；超时后直接放弃等待（残留 daemon 线程不阻塞监测轮，
    也不阻塞进程退出）。此前用 ThreadPoolExecutor 的 with 块退出时会
    shutdown(wait=True) 无限等卡死线程，把整轮监测拖死。
    """
    start = time.monotonic()
    result = []
    error = None
    holder = {}

    def _worker():
        try:
            holder["addrs"] = socket.getaddrinfo(
                domain, 443, socket.AF_INET, socket.SOCK_STREAM)
        except Exception as e:
            holder["exc"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        error = f"解析超时（>{timeout}s，已放弃等待）"
    elif "exc" in holder:
        e = holder["exc"]
        if isinstance(e, socket.gaierror):
            error = f"解析失败：{e}"
        else:
            error = f"解析异常：{e}"
    else:
        seen = set()
        for a in holder.get("addrs", []):
            ip = a[4][0]
            if ip not in seen:
                seen.add(ip)
                result.append(ip)
    latency_ms = int((time.monotonic() - start) * 1000)
    return result, latency_ms, error


def _http_get_json(url, proxy, timeout=10):
    """经代理用 HTTPS 获取 JSON。"""
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(
        url,
        headers={"accept": "application/dns-json", "User-Agent": "DNS-Monitor/1.0"},
    )
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def resolve_doh(domain, cfg, timeout=10):
    """经代理走 DoH 获取干净 IPv4 列表（Cloudflare 优先，失败回退 Google）。
    返回 (ips, latency_ms, error)。"""
    start = time.monotonic()
    proxy = cfg.get("proxy", "")
    doh = cfg.get("doh_server", "https://cloudflare-dns.com/dns-query")
    fallback = cfg.get("doh_fallback", "https://dns.google/resolve")
    ips = []
    error = None
    for base in (doh, fallback):
        try:
            url = f"{base}?name={domain}&type=A"
            data = _http_get_json(url, proxy, timeout=timeout)
            if data.get("Status") == 0:
                seen = set()
                out = []
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:  # A 记录
                        ip = ans.get("data")
                        if ip and ip not in seen:
                            seen.add(ip)
                            out.append(ip)
                if out:
                    return out, int((time.monotonic() - start) * 1000), None
            error = f"DoH({base}) 无有效结果（Status={data.get('Status')}）"
        except Exception as e:
            error = f"DoH({base}) 失败：{e}"
    return [], int((time.monotonic() - start) * 1000), error


def normalize_domain(raw):
    """把 *.google.com 这类通配域名归一化为可解析的基础域 google.com。"""
    d = raw.strip()
    if d.startswith("*."):
        d = d[2:]
    return d


# ============ 状态管理 ============

def load_state(state_path):
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state_path, state):
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  ⚠ 保存状态失败：{e}")


# ============ 监测一轮 ============

def check_all(domains, cfg, state, use_doh=True):
    """两步分级监测，返回 (records, alerts)。

    第一步（不挂代理，纯本地）：系统解析 + 预期段检查 + 与上轮对比，
      能自判正常的域名直接 OK，不消耗代理。
    第二步（走代理 DoH）：仅对第一步存疑的域名取干净基准对照，
      确认污染 / 真实失败 / 正常轮询——最终确认异常才生成提醒。
    （--no-doh 模式跳过第二步，用上轮对比 + CDN 白名单兜底判定）
    """
    records = []
    pending = []
    alerts = []
    now = time.time()
    cooldown = cfg.get("alert_cooldown_sec", 900)
    resolve_timeout = cfg.get("resolve_timeout_sec", 8)
    expect_nets = cfg.get("_expect_nets", {})

    # ---- 第一步：系统解析，本地快速分级（不走代理）----
    for raw in domains:
        domain = normalize_domain(raw)
        sys_ips, sys_latency, sys_error = resolve_system(domain, timeout=resolve_timeout)
        prev = state.get(domain, {})
        prev_ips = set(prev.get("ips", []))
        cur_set = set(sys_ips)
        expect = expect_nets.get(domain)

        record = {
            "domain": domain, "raw": raw,
            "sys_ips": sys_ips, "sys_latency_ms": sys_latency, "sys_error": sys_error,
            "clean_ips": [], "clean_latency_ms": None, "clean_error": None,
            "status": "OK", "reason": "", "doh_checked": False,
        }

        if sys_error or not sys_ips:
            # 系统解析失败：需干净基准对照，区分「污染」与「真实失败」
            record["_why"] = "fail"
            pending.append(record)
        elif expect:
            foreign = [ip for ip in sys_ips if not ip_in_nets(ip, expect)]
            if foreign:
                # 解析到非预期段：疑似污染，需干净基准佐证
                record["_why"] = "foreign"
                record["_foreign"] = foreign
                pending.append(record)
            else:
                record["status"] = "OK"
                record["reason"] = "解析正常（系统 IP 均在预期段内）"
        else:
            if prev_ips and cur_set == prev_ips and prev.get("verified_ok"):
                # 上轮已用干净基准验证正常，本轮结果无变化 → 静默通过
                record["status"] = "OK"
                record["reason"] = "解析正常（与上轮一致，上轮已用干净基准验证）"
            else:
                # 首轮 / 结果变化 / 从未验证过：需基准对照
                record["_why"] = "verify"
                if prev_ips and cur_set and cur_set != prev_ips:
                    record["_changed_from"] = sorted(prev_ips)
                pending.append(record)

        records.append(record)

    # ---- 第二步：仅对存疑域名走代理 DoH 对照 ----
    for record in pending:
        domain = record["domain"]
        prev = state.get(domain, {})
        sys_ips = record["sys_ips"]
        sys_error = record["sys_error"]
        expect = expect_nets.get(domain)
        why = record.pop("_why", "verify")
        foreign = record.pop("_foreign", None)
        changed_from = record.pop("_changed_from", None)
        clean_ips, clean_error = [], None

        if use_doh:
            clean_ips, clean_latency, clean_error = resolve_doh(domain, cfg)
            record.update(clean_ips=clean_ips, clean_latency_ms=clean_latency,
                          clean_error=clean_error, doh_checked=True)

        if why == "fail":
            if use_doh and clean_ips:
                record["status"] = "POLLUTED"
                record["reason"] = (
                    f"系统解析失败（{sys_error or '无结果'}），"
                    f"但干净解析可用 {clean_ips[:3]}，疑似本地 DNS 污染/拦截"
                )
            else:
                record["status"] = "FAIL"
                parts = [sys_error or "系统无解析结果"]
                if use_doh:
                    parts.append(clean_error or "干净解析也无结果")
                record["reason"] = "；".join(parts)
        elif why == "foreign":
            if clean_ips:
                record["status"] = "POLLUTED"
                record["reason"] = (
                    f"系统解析到非预期段 IP：{foreign}（干净基准 {clean_ips[:2]}），"
                    "疑似解析到错误服务器"
                )
            elif not use_doh:
                record["status"] = "POLLUTED"
                record["reason"] = (
                    f"系统解析到非预期段 IP：{foreign}（预期段不包含），疑似解析到错误服务器"
                )
            else:
                record["status"] = "CHECK"
                record["reason"] = (
                    f"系统解析 {sys_ips[:3]} 不在预期段，"
                    f"且干净解析不可用（{clean_error}），无法对照定性"
                )
        else:  # verify：系统解析成功，需基准判定
            if use_doh and clean_ips:
                overlap = set(sys_ips) & set(clean_ips)
                if overlap:
                    record["status"] = "OK"
                    if changed_from:
                        record["reason"] = (
                            f"解析结果虽变化，但与干净基准一致（{len(overlap)} 个 IP 重合），"
                            "属正常 CDN/全球调度轮询"
                        )
                    else:
                        record["reason"] = f"解析正常（{len(overlap)} 个 IP 与干净基准一致）"
                else:
                    record["status"] = "CHECK"
                    record["reason"] = (
                        f"系统解析 {sys_ips[:3]} 与干净基准 {clean_ips[:3]} 无交集，"
                        "需关注（可能是 CDN 轮询或代理出口差异，不一定是污染）"
                    )
            elif not use_doh:
                # 无代理模式：没有基准，只能靠与上轮对比
                if changed_from:
                    changed = set(sys_ips) - set(changed_from)
                    non_cdn = [ip for ip in changed if not is_cloudflare_ip(ip)]
                    if non_cdn:
                        record["status"] = "CHANGED"
                        record["reason"] = (
                            f"解析结果变化：新增 {sorted(non_cdn)}（原 {changed_from}）"
                        )
                    else:
                        record["reason"] = "解析结果变化，但为 CDN/云共享 IP，属正常轮询"
                else:
                    record["status"] = "OK"
                    record["reason"] = "首轮记录基准（无代理模式，仅记录）"
            else:
                record["status"] = "NO_BASELINE"
                record["reason"] = (
                    f"系统解析正常（{sys_ips[:3]}），"
                    f"但干净解析不可用（{clean_error}），跳过污染判定"
                )

        # —— 生成提醒（FAIL / POLLUTED / CHANGED / CHECK，带冷却）——
        if record["status"] in ("FAIL", "POLLUTED", "CHANGED", "CHECK"):
            last_alert = prev.get("last_alert", 0)
            if now - last_alert >= cooldown:
                alerts.append(record)
                record["last_alert"] = now

    # 更新状态：verified_ok = 最近一次被「预期段 / 干净基准」验证为正常
    for r in records:
        prev = state.get(r["domain"], {})
        state[r["domain"]] = {
            "ips": r["sys_ips"],
            "sys_latency_ms": r["sys_latency_ms"],
            "clean_ips": r["clean_ips"],
            "last_alert": r.get("last_alert", prev.get("last_alert", 0)),
            "verified_ok": r["status"] == "OK",
        }
    return records, alerts


def print_report(records, cfg):
    print("\n" + "=" * 60)
    print("DNS-Monitor 状态报告")
    print("=" * 60)
    fail = [r for r in records if r["status"] == "FAIL"]
    polluted = [r for r in records if r["status"] == "POLLUTED"]
    changed = [r for r in records if r["status"] == "CHANGED"]
    check = [r for r in records if r["status"] == "CHECK"]
    ok = [r for r in records if r["status"] == "OK"]
    no_baseline = [r for r in records if r["status"] == "NO_BASELINE"]

    for r in records:
        flag = {
            "OK": "✅",
            "FAIL": "🔴",
            "POLLUTED": "🟠",
            "CHANGED": "🟡",
            "CHECK": "🟢",
            "NO_BASELINE": "⚪",
        }.get(r["status"], "⚪")
        sys_str = ", ".join(r["sys_ips"]) if r["sys_ips"] else (r["sys_error"] or "(无)")
        clean_str = ", ".join(r["clean_ips"]) if r["clean_ips"] else (r["clean_error"] or "(未启用)")
        lat = f"{r['sys_latency_ms']}ms" if r["sys_latency_ms"] is not None else ""
        print(f"\n{flag} {r['domain']}  [{r['status']}]  ({lat})")
        print(f"    系统解析 : {sys_str}")
        if cfg.get("compare_with_doh", True) and r["clean_ips"]:
            print(f"    干净解析 : {clean_str}")
        print(f"    说明     : {r['reason']}")

    print("\n" + "-" * 60)
    print(f"汇总：正常 {len(ok)} | 疑似污染 {len(polluted)} | 结果变化 {len(changed)} "
          f"| 需关注 {len(check)} | 真实失败 {len(fail)} | 无基准 {len(no_baseline)}")
    print("=" * 60 + "\n")


# ============ 主流程 ============

def alert_title(r):
    if r["status"] == "POLLUTED":
        return "DNS 疑似污染/解析异常"
    if r["status"] == "FAIL":
        return "DNS 解析失败"
    if r["status"] == "CHECK":
        return "DNS 解析需关注"
    return "DNS 解析结果变化"


def run_one_round(cfg, state, domains, notify_on=True, use_doh=True):
    records, alerts = check_all(domains, cfg, state, use_doh=use_doh)
    print_report(records, cfg)
    append_alerts_jsonl(alerts, cfg)
    if alerts and notify_on:
        log(f"本轮检测到 {len(alerts)} 个异常域名，开始提醒", cfg)
        for r in alerts:
            msg = (f"{r['domain']}：{r['reason']}"
                   f"（系统：{', '.join(r['sys_ips']) if r['sys_ips'] else r['sys_error']}）")
            log(f"  ⚠ {alert_title(r)}：{msg}", cfg)
            notify(alert_title(r), msg, cfg)
    elif not alerts:
        log("本轮所有域名解析正常，无异常", cfg)
    return records


def run_monitor(cfg, domains, notify_on=True, use_doh=True):
    interval = cfg.get("monitor_interval_sec", 60)
    max_rounds = cfg.get("max_rounds", 0)  # 0 = 无限
    state_path = os.path.join(SCRIPT_DIR, "dns_monitor_state.json")
    state = load_state(state_path)

    log(f"DNS-Monitor 启动持续监测，间隔 {interval}s，通知={'开' if notify_on else '关'}，"
        f"干净基准DoH={'开' if use_doh else '关'}", cfg)
    if use_doh:
        log(f"代理：{cfg.get('proxy', '(空)')}", cfg)
    log(f"监测域名 {len(domains)} 个；关注 App {len(cfg.get('apps', []))} 个（App 仅记录，不做解析）", cfg)

    round_no = 0
    try:
        while True:
            round_no += 1
            log(f"—— 第 {round_no} 轮监测开始 ——", cfg)
            run_one_round(cfg, state, domains, notify_on=notify_on, use_doh=use_doh)
            save_state(state_path, state)
            if max_rounds and round_no >= max_rounds:
                log(f"已达到设定的 {max_rounds} 轮，监测结束。", cfg)
                break
            log(f"本轮结束，等待 {interval}s ...", cfg)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("收到中断，退出监测。", cfg)
    finally:
        save_state(state_path, state)


def main():
    ap = argparse.ArgumentParser(description="DNS-Monitor：常用网站/App 域名 DNS 解析监测 + 提醒")
    ap.add_argument("--status", action="store_true", help="只查一次状态（只读，不提醒）")
    ap.add_argument("--once", action="store_true", help="只检查一轮（同 --status）")
    ap.add_argument("--domains", nargs="+", help="自定义监测域名（覆盖配置）")
    ap.add_argument("--interval", type=int, help="自定义监测间隔（秒）")
    ap.add_argument("--no-notify", action="store_true", help="持续监测但不弹桌面通知（只写日志）")
    ap.add_argument("--no-doh", action="store_true", help="不走代理 DoH，只用系统解析（代理没开时可临时用）")
    args = ap.parse_args()

    cfg = load_config()
    if args.interval is not None:
        cfg["monitor_interval_sec"] = args.interval

    domains = args.domains if args.domains else cfg["domains"]
    use_doh = not args.no_doh

    state_path = os.path.join(SCRIPT_DIR, "dns_monitor_state.json")
    state = load_state(state_path)

    if args.status or args.once:
        run_one_round(cfg, state, domains, notify_on=False, use_doh=use_doh)
        save_state(state_path, state)
    else:
        run_monitor(cfg, domains, notify_on=not args.no_notify, use_doh=use_doh)


if __name__ == "__main__":
    main()
