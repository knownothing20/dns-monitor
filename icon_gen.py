#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
icon_gen.py — 用 Python 标准库生成 DNS-Monitor 托盘图标（雷达样式）。

仅用 zlib / struct / math（无第三方依赖，本机零安装可运行），输出：
  icons/dns_monitor.png   （256×256，32 位 RGBA，便于预览）
  icons/dns_monitor.ico   （多尺寸 16/24/32/48/256，供 exe / 托盘使用）

设计：琥珀橙圆角方块底 + 白色雷达同心圆 + 扫描线 + 中心点。
关键：每个尺寸独立绘制并按需简化——小尺寸只保留最核心的
「一圈环 + 中心点」，避免缩放糊成一团；大尺寸才加完整细节。
每个尺寸均以 4 倍超采样绘制后降采样，边缘平滑。

用法：python icon_gen.py
"""
import math
import os
import struct
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "icons")
SS = 4  # 超采样倍数

# 配色（R, G, B）—— 琥珀橙雷达风格，与常见深蓝网络类图标明显区分
C_BG = (245, 158, 11)      # 琥珀橙底 #F59E0B
C_RING = (255, 255, 255)   # 白色雷达波纹
C_CORE = (255, 255, 255)   # 中心点
C_SWEEP = (255, 255, 255)  # 扫描线


# ---------- 基础绘制（按画布尺寸操作的 RGBA 缓冲）----------

def blend(buf, w, h, x, y, r, g, b, a=255):
    if x < 0 or y < 0 or x >= w or y >= h:
        return
    i = (y * w + x) * 4
    d = buf[i + 3]
    if d == 0:
        buf[i] = r; buf[i + 1] = g; buf[i + 2] = b; buf[i + 3] = a
    elif a == 255:
        buf[i] = r; buf[i + 1] = g; buf[i + 2] = b; buf[i + 3] = 255
    else:
        aa = a / 255.0
        out = aa + d * (1.0 - aa)
        if out <= 0:
            return
        buf[i] = int((r * aa + buf[i] * d * (1.0 - aa)) / out)
        buf[i + 1] = int((g * aa + buf[i + 1] * d * (1.0 - aa)) / out)
        buf[i + 2] = int((b * aa + buf[i + 2] * d * (1.0 - aa)) / out)
        buf[i + 3] = int(out)


def fill_circle(buf, w, h, cx, cy, r, color, alpha=255):
    r2 = r * r
    for y in range(int(cy - r) - 1, int(cy + r) + 2):
        for x in range(int(cx - r) - 1, int(cx + r) + 2):
            dx = x - cx; dy = y - cy
            if dx * dx + dy * dy <= r2:
                blend(buf, w, h, x, y, color[0], color[1], color[2], alpha)


def line(buf, w, h, x1, y1, x2, y2, thickness, color, alpha=255):
    dx = x2 - x1; dy = y2 - y1
    steps = int(max(abs(dx), abs(dy))) + 1
    for i in range(steps + 1):
        t = i / steps
        fill_circle(buf, w, h, x1 + dx * t, y1 + dy * t, thickness / 2.0, color, alpha)


def ring(buf, w, h, cx, cy, r, thickness, color, alpha=255):
    steps = max(120, int(r * 3))
    for i in range(steps + 1):
        t = 2 * math.pi * i / steps
        fill_circle(buf, w, h, cx + r * math.cos(t), cy + r * math.sin(t),
                    thickness / 2.0, color, alpha)


def fill_rounded_rect(buf, w, h, x0, y0, x1, y1, r, color, alpha=255):
    r_ = min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0)
    for y in range(int(y0) - 1, int(y1) + 2):
        for x in range(int(x0) - 1, int(x1) + 2):
            cx = min(max(x, x0 + r_), x1 - r_)
            cy = min(max(y, y0 + r_), y1 - r_)
            dx = x - cx; dy = y - cy
            if dx * dx + dy * dy <= r_ * r_:
                blend(buf, w, h, x, y, color[0], color[1], color[2], alpha)


# ---------- 按尺寸分级绘制 ----------

def draw_canvas(size):
    """绘制 size×size 的图标（内部 4 倍超采样）。
    细节随尺寸分级，保证小尺寸（16/24）依然清晰不糊。"""
    W = H = size * SS
    buf = bytearray(W * H * 4)
    c = W / 2.0
    s = W / 256.0  # 相对 256 设计稿的缩放系数

    # 圆角方块底（内边距随尺寸等比）
    m = 18 * s
    fill_rounded_rect(buf, W, H, m, m, W - m, H - m, 56 * s, C_BG)

    if size <= 24:
        # 极小尺寸：只画一圈环 + 中心点，最大可辨识度
        ring(buf, W, H, c, c, 62 * s, 13 * s, C_RING)
        fill_circle(buf, W, H, c, c, 20 * s, C_CORE)
    elif size <= 32:
        # 小尺寸：只画两圈环 + 中心点（加扫描线会糊，去掉）
        ring(buf, W, H, c, c, 40 * s, 9 * s, C_RING, alpha=235)
        ring(buf, W, H, c, c, 70 * s, 9 * s, C_RING, alpha=235)
        fill_circle(buf, W, H, c, c, 15 * s, C_CORE)
    else:
        # 中大尺寸：完整雷达（两圈 + 十字刻度 + 扫描光束 + 中心点）
        ring(buf, W, H, c, c, 48 * s, 4 * s, C_RING, alpha=255)
        ring(buf, W, H, c, c, 76 * s, 4 * s, C_RING, alpha=200)
        for ang in (0, 90, 180, 270):
            t = math.radians(ang)
            line(buf, W, H,
                 c + 48 * s * math.cos(t), c + 48 * s * math.sin(t),
                 c + 76 * s * math.cos(t), c + 76 * s * math.sin(t),
                 2 * s, C_RING, alpha=120)
        for deg in range(-16, 2):
            t = math.radians(deg)
            line(buf, W, H, c, c,
                 c + 76 * s * math.cos(t), c + 76 * s * math.sin(t),
                 1 * s, C_SWEEP, alpha=22)
        t = math.radians(-16)
        line(buf, W, H, c, c,
             c + 76 * s * math.cos(t), c + 76 * s * math.sin(t),
             3 * s, C_SWEEP, alpha=240)
        fill_circle(buf, W, H, c, c, 12 * s, C_CORE)

    return buf, W, H


def downscale(buf, W, H, size):
    out = bytearray(size * size * 4)
    ss2 = SS * SS
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(SS):
                for dx in range(SS):
                    i = ((y * SS + dy) * W + (x * SS + dx)) * 4
                    r += buf[i]; g += buf[i + 1]; b += buf[i + 2]; a += buf[i + 3]
            i = (y * size + x) * 4
            out[i] = r // ss2; out[i + 1] = g // ss2
            out[i + 2] = b // ss2; out[i + 3] = a // ss2
    return out


# ---------- PNG / ICO 编码 ----------

def chunk(tag, data):
    c = tag + data
    crc = zlib.crc32(c) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + c + struct.pack(">I", crc)


def png_bytes_of(width, height, rgba):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])
    idat = chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def write_ico_multi(path, size_to_rgba):
    """把 {尺寸: RGBA} 打包成多尺寸 ICO（每尺寸独立绘制，避免缩放糊）。"""
    sizes = sorted(size_to_rgba.keys())
    images = [(s, png_bytes_of(s, s, size_to_rgba[s])) for s in sizes]
    hdr = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    body = b""
    for s, png in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                               len(png), offset)
        offset += len(png)
        body += png
    with open(path, "wb") as f:
        f.write(hdr + entries + body)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # 预览用 256 大图
    big_buf, big_w, big_h = draw_canvas(256)
    img256 = downscale(big_buf, big_w, big_h, 256)
    png_path = os.path.join(OUT_DIR, "dns_monitor.png")
    with open(png_path, "wb") as f:
        f.write(png_bytes_of(256, 256, img256))

    # 多尺寸 ICO：每个尺寸独立按级绘制
    size_to_rgba = {}
    for s in (16, 24, 32, 48, 256):
        b, w, h = draw_canvas(s)
        size_to_rgba[s] = downscale(b, w, h, s)
    ico_path = os.path.join(OUT_DIR, "dns_monitor.ico")
    write_ico_multi(ico_path, size_to_rgba)

    print("已生成:", png_path)
    print("已生成:", ico_path, "(多尺寸 16/24/32/48/256，各尺寸独立绘制)")


if __name__ == "__main__":
    main()
