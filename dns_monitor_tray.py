#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dns_monitor_tray.py — DNS-Monitor 托盘常驻入口（Windows，零第三方依赖）。

功能：
  1. 启动后自动隐藏到系统托盘，不在任务栏显示窗口。
  2. 后台线程持续运行 dns_monitor 的监测逻辑，异常时桌面通知 + 日志。
  3. 托盘图标为 DNS 网络样式（icons/dns_monitor.ico，由 icon_gen.py 生成）。
  4. 右键菜单：打开状态 / 开始监测 / 暂停 / 退出。

说明：
  - 托盘用 ctypes 调用 Windows 原生 Shell API（Shell_NotifyIcon），不依赖 pystray。
  - 复用 dns_monitor.py 的监测核心，保持逻辑一致。

用法：
  python dns_monitor_tray.py            # 托盘常驻
  python dns_monitor_tray.py --no-notify # 不弹桌面通知
"""

import argparse
import os
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
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# 常量
WM_APP = 0x8000
WM_USER = 0x0400
WM_NOTIFYICON = 0x004F
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202
IDM_SHOW = 101
IDM_START = 102
IDM_PAUSE = 103
IDM_QUIT = 104
IDM_STATUS = 105

# 消息框类型
MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 256),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class MENUITEMINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("fMask", wintypes.UINT),
        ("fType", wintypes.UINT),
        ("fState", wintypes.UINT),
        ("wID", wintypes.UINT),
        ("hSubMenu", wintypes.HMENU),
        ("hbmpChecked", wintypes.HBITMAP),
        ("hbmpUnchecked", wintypes.HBITMAP),
        ("dwItemData", wintypes.LPARAM),
        ("dwTypeData", wintypes.LPVOID),
        ("cch", wintypes.UINT),
    ]


# ============ 图标加载 ============

def load_icon():
    """从 ICO 文件加载图标句柄（零依赖，用 LoadImage）。"""
    if not os.path.exists(ICO_PATH):
        return None
    h = shell32.LoadImageW(
        None, ICO_PATH, wintypes.HANDLE, 0, 0,
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
        self.hwnd = None
        self.hicon = load_icon()
        self.nid = NOTIFYICONDATA()
        self.running = True
        self.last_status = ""

        # 注册隐藏窗口类
        self.register_class()
        self.hwnd = user32.CreateWindowExW(
            0, "DNSMonitorTrayW", APP_ID,
            0, 0, 0, 0, 0, None, None, kernel32.GetModuleHandleW(None), None,
        )
        user32.ShowWindow(self.hwnd, 0)  # 隐藏
        user32.UpdateWindow(self.hwnd)

        # 填充托盘数据
        self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.uCallbackMessage = WM_NOTIFYICON
        self.nid.hIcon = self.hicon if self.hicon else -1
        self.nid.szTip = APP_ID

    def register_class(self):
        wc = user32.WNDCLASSW()
        wc.lpfnWndProc = ctypes.CFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM
        )(self.wnd_proc)
        wc.lpszClassName = "DNSMonitorTrayW"
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        wc.hIcon = self.hicon if self.hicon else -1
        wc.hbrBackground = 16  # COLOR_BTNFACE + 1
        user32.RegisterClassW(ctypes.byref(wc))

    # ---- 窗口过程 ----
    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_NOTIFYICON:
            nid_id = wparam
            ni_type = lparam & 0xFFFFFFFF
            if ni_type == WM_RBUTTONUP:
                x = lparam & 0xFFFF
                y = (lparam >> 16) & 0xFFFF
                self.show_menu(x, y)
                return 0
            if ni_type == WM_LBUTTONUP:
                self.show_status_window()
                return 0
            return 0
        if msg == WM_APP:
            cmd = wparam
            if cmd == IDM_SHOW:
                self.show_status_window()
            elif cmd == IDM_START:
                self.paused.clear()
                self.set_tip("DNS-Monitor 监测已开始")
            elif cmd == IDM_PAUSE:
                self.paused.set()
                self.set_tip("DNS-Monitor 监测已暂停")
            elif cmd == IDM_STATUS:
                self.show_status_window()
            elif cmd == IDM_QUIT:
                self.quit()
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- 托盘操作 ----
    def add_tray(self):
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))

    def update_tray(self, tip=None, icon=None):
        if tip:
            self.nid.szTip = tip
        if icon is not None:
            self.nid.hIcon = icon
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def del_tray(self):
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))

    def set_tip(self, text):
        self.nid.szTip = text
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    # ---- 右键菜单 ----
    def show_menu(self, x, y):
        hmenu = user32.CreatePopupMenu()
        # 设置菜单项
        items = [
            (IDM_STATUS, "查看状态", False),
            (IDM_START, "开始监测", self.paused.is_set()),
            (IDM_PAUSE, "暂停监测", not self.paused.is_set()),
            (0x80000000, None, None),  # 分隔符
            (IDM_QUIT, "退出", False),
        ]
        for mid, text, check in items:
            if text is None:
                user32.AppendMenuW(hmenu, 0x8000, 0, None)  # MF_SEPARATOR
                continue
            flags = 0x00000080 if check else 0x00000000  # MF_UNCHECKED / MF_CHECKED
            if check:
                flags = 0x00000008  # MF_CHECKED
            user32.AppendMenuW(hmenu, flags | 0x00000100, mid, text)  # MF_STRING
        # 用 popup 菜单，跟踪鼠标
        r = user32.TrackPopupMenu(
            hmenu, 0x100 | 0x0008, x, y, 0, self.hwnd, None,
        )
        user32.DestroyMenu(hmenu)
        if r:
            user32.PostMessageW(self.hwnd, WM_APP, r, 0)

    # ---- 状态窗口（非阻塞，自动消失）----
    def show_status_window(self):
        """弹一个非阻塞的提示框显示最近状态。"""
        msg = self.last_status or "DNS-Monitor 运行中"
        user32.MessageBoxW(self.hwnd, msg, APP_ID, MB_OK | MB_ICONINFORMATION)

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
                    self.stop_evt.wait(interval)
                    continue
                rnd += 1
                dns_monitor.log("—— 第 %d 轮监测 ——" % rnd, self.cfg)
                try:
                    records, alerts = dns_monitor.check_all(
                        domains, self.cfg, state, use_doh=True
                    )
                    ok = sum(1 for r in records if r["status"] == "OK")
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
            dns_monitor.log("监测线程异常：%s" % traceback.format_exc(), self.cfg)

    def start_monitor(self):
        self.monitor_thread = threading.Thread(
            target=self.monitor_loop, name="dns-monitor-worker", daemon=True
        )
        self.monitor_thread.start()

    # ---- 退出 ----
    def quit(self):
        self.running = False
        self.stop_evt.set()
        self.del_tray()
        user32.DestroyWindow(self.hwnd)
        kernel32.PostQuitMessage(0)

    # ---- 消息循环 ----
    def run(self):
        self.add_tray()
        self.start_monitor()
        msg = wintypes.MSG()
        while self.running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
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
