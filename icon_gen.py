#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
icon_gen.py — 用 Python 标准库生成 DNS-Monitor 托盘图标（DNS 网络样式）。

仅用 zlib / struct / math（无第三方依赖，本机零安装可运行），输出：
  icons/dns_monitor.png   （32 位 RGBA）
  icons/dns_monitor.ico   （供 exe / 托盘使用）

设计：深蓝圆形底 + 地球环 + 网络节点与连线（DNS 解析网络）。
以 4 倍超采样绘制后降采样，边缘平滑。

用法：python icon_gen.py
"""
import math
import os
import struct
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "icons")
SIZE = 256
SS = 4  # 超采样倍数
W = H = SIZE * SS

# 配色（R, G, B）
C_BG = (15, 42, 67)        # 深蓝底
C_GLOBE = (79, 195, 247)   # 亮青（地球环）
C_LINE = (25, 121, 202)    # 网络连线
C_NODE = (41, 182, 246)    # 节点
C_NODE_CORE = (255, 255, 255)  # 节点核心


# ---------- 基础绘制（RGBA 缓冲，超采样尺寸）----------

def blend(buf, x, y, r, g, b, a=255):
    """Alpha 混合写像素。"""
    if x < 0 or y < 0 or x >= W or y >= H:
        return
    i = (y * W + x) * 4
    d = buf[i + 3]
    if d == 0:
        buf[i] = r
        buf[i + 1] = g
        buf[i + 2] = b
        buf[i + 3] = a
    elif a == 255:
        buf[i] = r
        buf[i + 1] = g
        buf[i + 2] = b
        buf[i + 3] = 255
    else:
        aa = a / 255.0
        out = aa + d * (1.0 - aa)
        if out <= 0:
            return
        buf[i] = int((r * aa + buf[i] * d * (1.0 - aa)) / out)
        buf[i + 1] = int((g * aa + buf[i + 1] * d * (1.0 - aa)) / out)
        buf[i + 2] = int((b * aa + buf[i + 2] * d * (1.0 - aa)) / out)
        buf[i + 3] = int(out)


def fill_circle(buf, cx, cy, r, color, alpha=255):
    r2 = r * r
    x0 = int(cx - r) - 1
    x1 = int(cx + r) + 1
    y0 = int(cy - r) - 1
    y1 = int(cy + r) + 1
    r_, g_, b_ = color
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            dx = x - cx
            dy = y - cy
            if dx * dx + dy * dy <= r2:
                blend(buf, x, y, r_, g_, b_, alpha)


def line(buf, x1, y1, x2, y2, thickness, color, alpha=255):
    dx = x2 - x1
    dy = y2 - y1
    steps = int(max(abs(dx), abs(dy))) + 1
    for i in range(steps + 1):
        t = i / steps
        fill_circle(buf, x1 + dx * t, y1 + dy * t, thickness / 2.0, color, alpha)


def ring(buf, cx, cy, r, thickness, color, alpha=255):
    steps = 360
    for i in range(steps + 1):
        t = 2 * math.pi * i / steps
        fill_circle(buf, cx + r * math.cos(t), cy + r * math.sin(t),
                    thickness / 2.0, color, alpha)


def ellipse_ring(buf, cx, cy, rx, ry, thickness, color, alpha=255):
    steps = 360
    for i in range(steps + 1):
        t = 2 * math.pi * i / steps
        fill_circle(buf, cx + rx * math.cos(t), cy + ry * math.sin(t),
                    thickness / 2.0, color, alpha)


def draw():
    buf = bytearray(W * H * 4)  # 全透明
    c = SIZE / 2.0 * SS

    # 深蓝圆形底
    fill_circle(buf, c, c, 120 * SS, C_BG)

    # 地球环 + 经线/纬线
    ring(buf, c, c, 78 * SS, 5 * SS, C_GLOBE)
    line(buf, c, c - 78 * SS, c, c + 78 * SS, 3 * SS, C_GLOBE)
    line(buf, c - 78 * SS, c, c + 78 * SS, c, 3 * SS, C_GLOBE)
    ellipse_ring(buf, c, c, 34 * SS, 78 * SS, 3 * SS, C_GLOBE)

    # 网络节点：中心 + 5 个外围节点（五边形分布）
    angles = [90, 162, 234, 306, 18]
    rr = 62 * SS
    nodes = [(c, c)]
    for a in angles:
        t = math.radians(a)
        nodes.append((c + rr * math.cos(t), c + rr * math.sin(t)))

    # 连线：中心到各外围节点（星型）+ 外围相邻节点（环形）
    for i in range(1, len(nodes)):
        line(buf, nodes[0][0], nodes[0][1], nodes[i][0], nodes[i][1],
             4 * SS, C_LINE)
    for i in range(1, len(nodes)):
        j = i + 1 if i + 1 < len(nodes) else 1
        line(buf, nodes[i][0], nodes[i][1], nodes[j][0], nodes[j][1],
             3 * SS, C_LINE)

    # 节点：光晕 + 实心 + 核心
    for idx, (x, y) in enumerate(nodes):
        rad = 13 * SS if idx == 0 else 9 * SS
        fill_circle(buf, x, y, rad + 6 * SS, C_NODE, alpha=40)
        fill_circle(buf, x, y, rad, C_NODE)
        fill_circle(buf, x, y, rad * 0.55, C_NODE_CORE)

    return buf


def downscale(buf):
    """SS 倍超采样降采样为目标尺寸（块平均）。"""
    out = bytearray(SIZE * SIZE * 4)
    ss2 = SS * SS
    for y in range(SIZE):
        for x in range(SIZE):
            r = g = b = a = 0
            for dy in range(SS):
                for dx in range(SS):
                    i = ((y * SS + dy) * W + (x * SS + dx)) * 4
                    r += buf[i]
                    g += buf[i + 1]
                    b += buf[i + 2]
                    a += buf[i + 3]
            i = (y * SIZE + x) * 4
            out[i] = r // ss2
            out[i + 1] = g // ss2
            out[i + 2] = b // ss2
            out[i + 3] = a // ss2
    return out


# ---------- PNG / ICO 编码 ----------

def chunk(tag, data):
    c = tag + data
    crc = zlib.crc32(c) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + c + struct.pack(">I", crc)


def write_png(path, width, height, rgba):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter: none
        raw.extend(rgba[y * stride:(y + 1) * stride])
    idat = chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    iend = chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(sig + ihdr + idat + iend)


def write_ico(path, png_bytes, size):
    """把 PNG 数据封装为单尺寸 ICO。"""
    hdr = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack(
        "<BBBBHHII",
        size % 256, size % 256, 0, 0,
        1, 32, len(png_bytes), 22,
    )
    with open(path, "wb") as f:
        f.write(hdr + entry + png_bytes)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    buf = draw()
    img = downscale(buf)

    png_path = os.path.join(OUT_DIR, "dns_monitor.png")
    write_png(png_path, SIZE, SIZE, img)
    with open(png_path, "rb") as f:
        png_bytes = f.read()

    ico_path = os.path.join(OUT_DIR, "dns_monitor.ico")
    write_ico(ico_path, png_bytes, SIZE)

    print("已生成:", png_path)
    print("已生成:", ico_path)


if __name__ == "__main__":
    main()
