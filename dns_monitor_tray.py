#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dns_monitor_tray.py — DNS-Monitor 托盘常驻入口（Windows，零第三方依赖）。

功能：
  1. 启动后自动隐藏到系统托盘，不在任务栏显示窗口。
  2. 后台线程持续运行 dns_monitor 的监测逻辑，异常时桌面通知 + 日志。
  3. 托盘图标为 DNS 网络样式（icons/dns_monitor.ico，由 icon_gen.py 生成）。
  4. 右键菜单：查看状态 / 开始监测 / 暂停 / 退出。

说明：
  - 托盘用 ctypes 调用 Windows 原生 Shell API（Shell_NotifyIcon），不依赖 pystray。
  - 复用 dns_monitor.py 的监测核心，保持逻辑一致。

用法：
  python dns_monitor_tray.py            # 托盘常驻
  python dns_monitor_tray.py --no-notify # 不弹桌面通知
"""

import argparse
import os
import sys
import threading
import time
import traceback

import ctypes
from ctypes import wintypes

# 导入监测核心（同目录 dns_monitor.py）
import dns_monitor

# PyInstaller 打包后：SCRIPT_DIR 用 exe 所在目录（配置随 exe），
# 图标从打包资源 _MEIPASS 里读（只读资源，放临时目录没问题）。
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
    ICO_PATH = os.path.join(getattr(sys, "_MEIPASS", SCRIPT_DIR), "icons", "dns_monitor.ico")
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    ICO_PATH = os.path.join(SCRIPT_DIR, "icons", "dns_monitor.ico")

APP_ID = "DNS-Monitor"

# ============ Windows API 定义 ============

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# 常量
WM_APP = 0x8000
WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_NOTIFYICON = 0x004F  # 自定义托盘回调消息号
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010  # 气泡通知（szInfo）

WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202

# 菜单标志
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
TPM_RETURNCMD = 0x00000100
TPM_RIGHTBUTTON = 0x00000002

IDM_START = 102
IDM_PAUSE = 103
IDM_QUIT = 104
IDM_STATUS = 105


# LRESULT = LONG_PTR：64 位系统 8 字节，32 位 4 字节（wintypes 里没有现成的）
LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM)),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# 64 位下句柄/WPARAM/LRESULT 是 8 字节。ctypes 对未声明 argtypes 的函数
# 按 32 位 c_int 传参，高 32 位寄存器残留垃圾——会导致句柄/WPARAM 间歇性
# 错乱（实测：菜单命令 PostMessage 时灵时不灵）。必须全部显式声明。
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UpdateWindow.restype = wintypes.BOOL
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.CreatePopupMenu.argtypes = []
user32.AppendMenuW.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, ctypes.c_void_p,
]
user32.DestroyMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
# 第二参数是 MAKEINTRESOURCEW(32512)——小整数伪装的指针，用 LPVOID 才能传 int
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
]
user32.TranslateMessage.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostQuitMessage.restype = None
user32.PostQuitMessage.argtypes = [ctypes.c_int]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.UINT, ctypes.c_void_p]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


# ============ 图标加载 ============

def load_icon():
    """从 ICO 文件加载图标句柄（零依赖，用 LoadImage）。"""
    if not os.path.exists(ICO_PATH):
        return None
    # LoadImageW 属于 user32.dll；参数: (实例, 路径, 类型1=IMAGE_ICON, 宽0, 高0, 标志)
    h = user32.LoadImageW(
        None, ICO_PATH, 1, 0, 0,
        0x00000010 | 0x00000040,  # LR_LOADFROMFILE | LR_LOADTRANSPARENT
    )
    return h or None


# ============ 托盘应用 ============

class TrayApp:
    def __init__(self, notify_on=True):
        self.notify_on = notify_on
        self.stop_evt = threading.Event()
        self.paused = threading.Event()
        self.monitor_thread = None
        self.cfg = None
        self.hicon = load_icon()
        self.nid = NOTIFYICONDATA()
        self.running = True
        self.last_status = ""

        # 窗口过程回调必须挂 self 引用，防止被 GC 后崩溃
        self._wnd_proc_cb = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM
        )(self.wnd_proc)

        # 注册隐藏窗口类
        self.register_class()
        self.hwnd = user32.CreateWindowExW(
            0, "DNSMonitorTrayW", APP_ID,
            0, 0, 0, 0, 0, None, None,
            kernel32.GetModuleHandleW(None), None,
        )
        user32.ShowWindow(self.hwnd, 0)  # 隐藏
        user32.UpdateWindow(self.hwnd)

        # 填充托盘数据
        self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.uCallbackMessage = WM_NOTIFYICON
        self.nid.hIcon = self.hicon if self.hicon else None
        self.nid.szTip = APP_ID

    def register_class(self):
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wnd_proc_cb
        wc.lpszClassName = "DNSMonitorTrayW"
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        wc.hIcon = self.hicon
        wc.hbrBackground = 16  # COLOR_BTNFACE + 1
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())

    # ---- 窗口过程 ----
    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_NOTIFYICON:
            ni_type = lparam & 0xFFFFFFFF
            if ni_type == WM_RBUTTONUP:
                # 托盘回调的 lparam 只有鼠标事件号，坐标必须用 GetCursorPos 取
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self.show_menu(pt.x, pt.y)
                return 0
            if ni_type == WM_LBUTTONUP:
                self.show_status_window()
                return 0
            return 0
        if msg == WM_APP:
            cmd = wparam
            if cmd == IDM_STATUS:
                self.show_status_window()
            elif cmd == IDM_START:
                if self.paused.is_set():
                    self.paused.clear()
                    self.set_tip("DNS-Monitor 监测已开始")
                    self.show_balloon("DNS-Monitor", "监测已恢复运行")
                else:
                    self.show_balloon("DNS-Monitor", "监测已在运行中（启动即自动开始，无需手动点）")
            elif cmd == IDM_PAUSE:
                if self.paused.is_set():
                    self.show_balloon("DNS-Monitor", "监测已是暂停状态")
                else:
                    self.paused.set()
                    self.set_tip("DNS-Monitor 监测已暂停")
                    self.show_balloon("DNS-Monitor", "监测已暂停")
            elif cmd == IDM_QUIT:
                self.quit()
            return 0
        # 托盘常驻程序的隐藏主窗口绝不能被外部消息关掉：
        # WM_CLOSE / WM_DESTROY 一律拦截（否则任何窗口管理工具、
        # RDP 会话切换、任务栏整理等发出的一次 WM_CLOSE 就会让
        # 托盘静默退出，且不留下任何日志）。退出只走菜单 IDM_QUIT。
        if msg == WM_CLOSE:
            dns_monitor.log("收到 WM_CLOSE，已忽略（托盘继续驻留）", self.cfg)
            return 0
        if msg == WM_DESTROY:
            dns_monitor.log("收到 WM_DESTROY，已忽略", self.cfg)
            return 0
        # 系统关机/注销：正常退出，同时把托盘图标清理干净
        if msg == WM_ENDSESSION and wparam:
            self.del_tray()
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- 托盘操作 ----
    def add_tray(self):
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))

    def del_tray(self):
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))

    def set_tip(self, text):
        self.nid.szTip = text[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def show_balloon(self, title, text):
        """弹托盘气泡通知，给菜单操作可见反馈。"""
        self.nid.uFlags = NIF_INFO
        self.nid.szInfoTitle = title[:63]
        self.nid.szInfo = text[:255]
        self.nid.dwInfoFlags = 0  # NIIF_NONE
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP

    # ---- 右键菜单 ----
    def show_menu(self, x, y):
        hmenu = user32.CreatePopupMenu()
        items = [(IDM_STATUS, "查看状态", False)]
        # 动态单项：只显示当前可执行的动作，菜单文字随监测状态自动切换
        if self.paused.is_set():
            items.append((IDM_START, "开始监测", False))
        else:
            items.append((IDM_PAUSE, "暂停监测", False))
        items.append((0, None, None))  # 分隔符
        items.append((IDM_QUIT, "退出", False))
        for mid, text, check in items:
            if text is None:
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            flags = (MF_CHECKED if check else 0) | MF_STRING
            user32.AppendMenuW(hmenu, flags, mid, text)
        # 经典托盘菜单模式：前置 SetForegroundWindow，后置 WM_NULL，
        # 否则点击菜单外区域时菜单不会自动收起
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON, x, y, 0, self.hwnd, None,
        )
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        if cmd:
            user32.PostMessageW(self.hwnd, WM_APP, cmd, 0)

    # ---- 状态窗口（Tkinter 异常记录窗口，独立线程运行）----
    def show_status_window(self):
        """打开异常记录窗口（防重复：同时只开一个）。"""
        if getattr(self, "_status_open", False):
            return
        self._status_open = True

        def _run():
            try:
                self._build_status_window()
            except Exception:
                import traceback
                dns_monitor.log("状态窗口异常：%s" % traceback.format_exc(), self.cfg)
            finally:
                self._status_open = False

        threading.Thread(target=_run, name="dns-status-window", daemon=True).start()

    def _build_status_window(self):
        import tkinter as tk
        from tkinter import ttk, messagebox

        STATUS_LABEL = {
            "POLLUTED": ("疑似污染", "#854F0B", "#FAEEDA"),
            "FAIL": ("真实失败", "#A32D2D", "#FCEBEB"),
            "CHANGED": ("结果变化", "#633806", "#FAEEDA"),
            "CHECK": ("需关注", "#3B6D11", "#EAF3DE"),
            "NODATA": ("无A记录", "#4A4A4A", "#EEEEEE"),
        }

        root = tk.Tk()
        root.title("DNS-Monitor — 异常记录")
        root.geometry("980x520")
        root.minsize(760, 380)

        # 顶部状态摘要
        summary = self.last_status or "DNS-Monitor 运行中"
        top = tk.Frame(root, padx=12, pady=8)
        top.pack(fill="x")
        tk.Label(top, text=summary, font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        tk.Label(top, text="数据源：dns_monitor_alerts.jsonl（由监测自动记录）",
                 fg="#888888", font=("Microsoft YaHei UI", 9)).pack(side="right")

        # 表格
        cols = ("time", "domain", "status", "sys", "clean", "reason")
        tree_frame = tk.Frame(root)
        tree_frame.pack(fill="both", expand=True, padx=12)
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        headings = {
            "time": ("时间", 80, "center"), "domain": ("域名", 130, "w"),
            "status": ("状态", 80, "center"), "sys": ("系统解析", 150, "w"),
            "clean": ("干净解析", 150, "w"), "reason": ("说明", 360, "w"),
        }
        for c in cols:
            text, width, anchor = headings[c]
            tree.heading(c, text=text)
            tree.column(c, width=width, anchor=anchor, stretch=(c == "reason"))
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for status, (label, fg, bg) in STATUS_LABEL.items():
            tree.tag_configure(status, foreground=fg)

        def fmt_ips(row, key, err_key):
            err = row.get(err_key)
            ips = row.get(key) or []
            if ips:
                return ", ".join(ips[:4])
            return err or "—"

        def reload_table():
            tree.delete(*tree.get_children())
            for i, row in enumerate(dns_monitor.load_alerts_jsonl()):
                label = STATUS_LABEL.get(row.get("status"), (row.get("status"), None, None))[0]
                status_key = row.get("status", "")
                if status_key not in STATUS_LABEL:
                    status_key = ""
                tree.insert("", "end", iid=str(i), values=(
                    row.get("time", ""), row.get("domain", ""), label,
                    fmt_ips(row, "sys_ips", "sys_error"),
                    fmt_ips(row, "clean_ips", "clean_error"),
                    row.get("reason", ""),
                ), tags=(status_key,))

        def update_summary():
            s = self.last_status or "DNS-Monitor 运行中"
            for w in top.winfo_children():
                if isinstance(w, tk.Label) and w.cget("fg") != "#888888":
                    w.configure(text=s)

        reload_table()

        # 一键清除（备份后清空异常记录）
        def clear_records():
            path = dns_monitor.get_alerts_jsonl_path()
            if not os.path.exists(path):
                messagebox.showinfo("DNS-Monitor", "当前没有异常记录", parent=root)
                return
            if not messagebox.askyesno(
                    "DNS-Monitor", "确定清除全部异常记录？\n（清除前会自动备份为 dns_monitor_alerts.jsonl.bak）",
                    parent=root):
                return
            try:
                bak = path + ".bak"
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(path, bak)
                reload_table()
                messagebox.showinfo("DNS-Monitor", "已清除（备份：%s）" % os.path.basename(bak), parent=root)
            except Exception as e:
                messagebox.showerror("DNS-Monitor", "清除失败：%s" % e, parent=root)

        # 一键导出：生成给 agent 的诊断提示词（写文件 + 复制剪贴板）
        def export_prompt():
            rows = dns_monitor.load_alerts_jsonl()
            if not rows:
                messagebox.showinfo("DNS-Monitor", "当前没有异常记录可导出", parent=root)
                return
            proxy = (self.cfg or {}).get("proxy", "")
            doh = (self.cfg or {}).get("doh_server", "")
            doh_fb = (self.cfg or {}).get("doh_fallback", "")
            lines = []
            lines.append("你是网络与 DNS 诊断专家。请根据下面的 DNS 监测异常记录，逐条诊断原因并给出可执行的解决方案。")
            lines.append("")
            lines.append("## 监测原理")
            lines.append("DNS-Monitor 在 Windows 上对每个域名做「两步分级」解析，不挂代理时尽量自判，存疑才走代理复核：")
            lines.append("第一步（不挂代理，纯本地）：用系统解析（本机默认 DNS，反映实际上网时的本地解析）查该域名，")
            lines.append("  并结合「预期 IP 段」和「上一轮结果」做快速分级——能自判正常的直接通过，不消耗代理。")
            lines.append("第二步（走代理复核）：只对第一步存疑的域名，经本地代理 %s 走 DoH（%s，失败回退 %s）"
                         "取未污染基准对照，确认到底是污染、真实失败，还是其实只是正常 CDN 轮询。" % (proxy, doh, doh_fb))
            lines.append("")
            lines.append("状态判定规则：")
            lines.append("- POLLUTED（疑似污染）：系统解析失败但干净解析可用；或系统 IP 不在预期段 / 命中已知 GFW 注入假 IP 池（Facebook/Twitter/Dropbox 等被墙服务商网段）")
            lines.append("- FAIL（真实失败）：系统与干净解析都失败（NXDOMAIN=域名不存在；SERVFAIL=域名权威服务器故障，属域名方问题）")
            lines.append("- NODATA（无 A 记录）：域名本身没有 A 记录（DoH NOERROR 但无结果），属监控目标配置问题，非网络故障")
            lines.append("- CHANGED（结果变化）：仅在不挂代理模式下使用（上轮与干净基准一致且本轮变化）")
            lines.append("- CHECK（需关注）：系统解析与干净基准无交集（也不在同 /24），可能是 CDN 轮询或代理出口差异")
            lines.append("- OK（正常）：系统 IP 在预期段内，或与干净基准一致 / 同属一个 /24")
            lines.append("注：已标记「预期状态」的域名（如已知被墙站点测出污染，见 domains/domain_expect_status.json）符合预期时不提醒、不写入本记录。")
            lines.append("")
            lines.append("## 当前状态")
            lines.append(self.last_status or "运行中")
            lines.append("")
            lines.append("## 异常记录（共 %d 条，时间倒序）" % len(rows))
            lines.append("| 时间 | 域名 | 状态 | 系统解析 | 干净解析 | 说明 |")
            lines.append("|---|---|---|---|---|---|")
            for row in rows:
                label = STATUS_LABEL.get(row.get("status"), (row.get("status", ""),))[0]
                lines.append("| %s | %s | %s | %s | %s | %s |" % (
                    row.get("time", ""), row.get("domain", ""), label,
                    fmt_ips(row, "sys_ips", "sys_error").replace("|", "/"),
                    fmt_ips(row, "clean_ips", "clean_error").replace("|", "/"),
                    row.get("reason", "").replace("|", "/"),
                ))
            lines.append("")
            lines.append("## 请你完成")
            lines.append("1. 逐条诊断：区分 DNS 污染/被墙、域名自身故障、CDN 正常轮询、本地网络或代理问题")
            lines.append("2. 标出哪些需要处理、哪些可以忽略")
            lines.append("3. 对需要处理的给出具体可执行步骤（命令或配置修改）")
            lines.append("4. 若信息不足以判断，列出你还需要我提供哪些诊断信息")
            text = "\n".join(lines)

            out_path = os.path.join(SCRIPT_DIR, "dns_monitor_agent_prompt.txt")
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                messagebox.showerror("DNS-Monitor", "导出失败：%s" % e, parent=root)
                return
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update_idletasks()
            messagebox.showinfo(
                "DNS-Monitor",
                "已导出 %d 条异常记录：\n\n✔ 提示词已复制到剪贴板，可直接粘贴给 AI agent\n✔ 文件已保存：%s" % (len(rows), out_path),
                parent=root)

        # 底部按钮
        bottom = tk.Frame(root, padx=12, pady=10)
        bottom.pack(fill="x")
        tk.Button(bottom, text="一键清除记录", fg="#A32D2D", command=clear_records).pack(side="left")
        tk.Button(bottom, text="一键导出（agent 提示词）", command=export_prompt).pack(side="left", padx=8)
        tk.Button(bottom, text="刷新", command=lambda: (reload_table(), update_summary())).pack(side="left")
        tk.Button(bottom, text="关闭", command=root.destroy).pack(side="right")

        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.mainloop()

    # ---- 监测线程 ----
    def monitor_loop(self):
        try:
            self.cfg = dns_monitor.load_config()
            if self.notify_on is False:
                self.cfg["notify"] = False
            domains = self.cfg["domains"]
            interval = self.cfg.get("monitor_interval_sec", 60)
            max_rounds = self.cfg.get("max_rounds", 0)
            state_path = os.path.join(SCRIPT_DIR, "dns_monitor_state.json")
            state = dns_monitor.load_state(state_path)

            dns_monitor.log("DNS-Monitor 托盘版启动，监测 %d 个域名，间隔 %ds" % (len(domains), interval), self.cfg)
            self.set_tip("DNS-Monitor 运行中（%d 域名）" % len(domains))

            rnd = 0
            while not self.stop_evt.is_set():
                if self.paused.is_set():
                    # 短周期等待，让「恢复监测」即时生效，而不是最多等一个完整间隔
                    self.stop_evt.wait(1)
                    continue
                rnd += 1
                # —— 每轮热加载配置：改域名清单/预期段/预期状态/间隔，下一轮即生效，无需重启托盘 ——
                try:
                    fresh = dns_monitor.load_config()
                    if self.notify_on is False:
                        fresh["notify"] = False
                    new_domains = fresh["domains"]
                    if new_domains != domains:
                        dns_monitor.log(
                            "配置热加载：监测域名 %d → %d 个" % (len(domains), len(new_domains)),
                            fresh)
                        self.set_tip("DNS-Monitor 运行中（%d 域名）" % len(new_domains))
                    self.cfg, domains = fresh, new_domains
                    interval = fresh.get("monitor_interval_sec", 60)
                    max_rounds = fresh.get("max_rounds", 0)
                except Exception as e:
                    dns_monitor.log("配置热加载失败（沿用上一轮配置）：%s" % e, self.cfg)
                dns_monitor.log("—— 第 %d 轮监测 ——" % rnd, self.cfg)
                try:
                    records, alerts = dns_monitor.check_all(
                        domains, self.cfg, state, use_doh=True
                    )
                    dns_monitor.append_alerts_jsonl(alerts, self.cfg)
                    ok = sum(1 for r in records if r["status"] == "OK" or r.get("expected"))
                    bad = len(records) - ok
                    self.last_status = "DNS-Monitor 第 %d 轮：正常 %d / 异常 %d" % (rnd, ok, bad)
                    dns_monitor.log(self.last_status, self.cfg)
                    # 异常提醒
                    if alerts and self.cfg.get("notify", True):
                        for r in alerts:
                            dns_monitor.notify(dns_monitor.alert_title(r), r["reason"], self.cfg)
                    dns_monitor.save_state(state_path, state)
                except Exception as e:
                    dns_monitor.log("监测轮异常：%s" % e, self.cfg)
                if max_rounds and rnd >= max_rounds:
                    dns_monitor.log("达到 %d 轮，监测结束" % max_rounds, self.cfg)
                    break
                self.stop_evt.wait(interval)
            dns_monitor.log("监测线程退出。", self.cfg)
        except Exception:
            try:
                dns_monitor.log("监测线程异常：%s" % traceback.format_exc(), self.cfg)
            except Exception:
                traceback.print_exc()

    def start_monitor(self):
        self.monitor_thread = threading.Thread(
            target=self.monitor_loop, name="dns-monitor-worker", daemon=True
        )
        self.monitor_thread.start()

    # ---- 退出 ----
    def quit(self):
        try:
            dns_monitor.log("收到退出请求，正在退出托盘", self.cfg)
        except Exception:
            pass
        self.running = False
        self.stop_evt.set()
        self.del_tray()
        user32.PostQuitMessage(0)

    # ---- 消息循环 ----
    def run(self):
        self.add_tray()
        self.start_monitor()
        msg = wintypes.MSG()
        exit_reason = "消息循环正常结束"
        try:
            while self.running:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:
                    exit_reason = "收到 WM_QUIT"
                    break
                if ret == -1:
                    exit_reason = "GetMessage 出错"
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            try:
                dns_monitor.log("托盘消息循环退出：%s" % exit_reason, self.cfg)
            except Exception:
                pass
            self.del_tray()


def main():
    ap = argparse.ArgumentParser(description="DNS-Monitor 托盘常驻版")
    ap.add_argument("--no-notify", action="store_true", help="不弹桌面通知，只写日志")
    args = ap.parse_args()

    app = TrayApp(notify_on=not args.no_notify)
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit()


if __name__ == "__main__":
    main()
