#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_fw.py -- 360T7 (MT7981 SPI-NAND) 固件解包器
=====================================================
适用于 "360 VRF (\x7fVRF) 容器 + UBI 镜像" 结构的固件：

  0x000000  +-----------------------------+
            | 360 VRF 头 (\x7f 'VRF')       |
            |   ... bootloader / kernel   |
  0x0E89B0  | UBI 镜像起点 (magic 'UBI#')  |
            |   PEB = 128 KiB             |
            |   EC  hdr @ +0x000          |
            |   VID hdr @ +0x800  'UBI!'  |
            |   data    @ +0x1000         |
            |     - vol 0: 内核/其他       |
            |     - vol 1: rootfs squashfs (xz, v4.0) |
            +-----------------------------+

处理流程：
  1. 自动定位 UBI 起点（首个 'UBI#'）
  2. 自动检测 PEB 大小（相邻 'UBI#' 间距）
  3. 自动检测 VID/data 偏移（PEB 内 'UBI!' 位置）
  4. 按 vol_id/lnum 重组各卷 LEB 流（跳过坏块，重复 LEB 取 sqnum 最大）
  5. 每个卷落盘为 vol_<id>.bin
  6. 在所有卷中搜 'hsqs' 切出 rootfs.squashfs（校验超级块+metadata 块）
  7. 若 PATH 有 unsquashfs 则直接解包成 rootfs/ 目录

用法：
  python3 extract_fw.py 360T7.bin -o out
  python3 extract_fw.py 360T7.bin -o out --no-unsquashfs
"""

from __future__ import annotations

import argparse
import collections
import lzma
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

UBI_EC_MAGIC = b"UBI#"
UBI_VID_MAGIC = b"UBI!"
SQUASHFS_MAGIC = b"hsqs"
PEB_CANDIDATES = (0x20000, 0x40000, 0x10000, 0x80000)


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# UBI 定位
# --------------------------------------------------------------------------
def find_first_ubi(data: bytes) -> int:
    return data.find(UBI_EC_MAGIC)


def detect_peb_size(data: bytes, first: int) -> int:
    """相邻 'UBI#' 间距出现次数最多的值即为 PEB 大小。"""
    offs, i = [], first
    while True:
        i = data.find(UBI_EC_MAGIC, i)
        if i < 0 or len(offs) >= 512:
            break
        offs.append(i)
        i += 4
    if len(offs) >= 2:
        gaps = collections.Counter(
            offs[k + 1] - offs[k] for k in range(len(offs) - 1))
        gap, _ = gaps.most_common(1)[0]
        for c in PEB_CANDIDATES:
            if abs(gap - c) < 0x1000:
                return c
        if gap > 0x8000:
            return gap
    return 0x20000


def detect_vid_data_offsets(data: bytes, first: int, peb: int) -> tuple[int, int]:
    """在第一个 PEB 内找 'UBI!' -> VID 偏移；data 偏移取 vid*2（2K/4K page 惯例）。"""
    window = data[first + 8: first + peb]
    v = window.find(UBI_VID_MAGIC)
    if v < 0:
        return 0x800, 0x1000
    vid_off = v + 8
    # 常见组合：vid=0x800->data=0x1000(2K page)；vid=0x1000->data=0x2000(4K page)
    if vid_off <= 0x800:
        return vid_off, 0x1000
    if vid_off <= 0x1000:
        return vid_off, 0x2000
    return vid_off, vid_off * 2


def parse_vid(hdr: bytes) -> dict | None:
    """解析 64B UBI VID header（大端）。"""
    if len(hdr) < 64 or hdr[:4] != UBI_VID_MAGIC:
        return None
    vol_type = hdr[5]
    vol_id, lnum = struct.unpack(">II", hdr[8:16])
    data_size = struct.unpack(">I", hdr[20:24])[0]
    sqnum = struct.unpack(">Q", hdr[40:48])[0]
    return {"vol_id": vol_id, "lnum": lnum, "data_size": data_size,
            "sqnum": sqnum, "vol_type": vol_type}


# --------------------------------------------------------------------------
# 卷重组
# --------------------------------------------------------------------------
def rebuild_volumes(data: bytes, first: int, peb: int,
                    vid_off: int, data_off: int) -> dict[int, bytes]:
    """遍历所有 PEB，(vol_id, lnum) -> payload（同键取 sqnum 大者）。"""
    total = (len(data) - first) // peb
    vols: dict[int, dict[int, tuple[int, bytes]]] = collections.defaultdict(dict)
    bad = 0
    for k in range(total):
        base = first + k * peb
        if data[base:base + 4] != UBI_EC_MAGIC:
            bad += 1
            continue
        vid = parse_vid(data[base + vid_off: base + vid_off + 64])
        if not vid:
            bad += 1
            continue
        payload = data[base + data_off: base + peb]
        if vid["vol_type"] == 2 and 0 < vid["data_size"] <= len(payload):
            payload = payload[: vid["data_size"]]  # static 卷按 data_size 截断
        cur = vols[vid["vol_id"]].get(vid["lnum"])
        if cur is None or vid["sqnum"] >= cur[0]:
            vols[vid["vol_id"]][vid["lnum"]] = (vid["sqnum"], payload)
    log(f"[+] PEB total={total} bad/skip={bad}")
    out = {}
    for vid_id in sorted(vols):
        lebs = vols[vid_id]
        joined = b"".join(lebs[n][1] for n in sorted(lebs))
        out[vid_id] = joined
        log(f"[+] volume {vid_id}: {len(lebs)} LEBs, {len(joined):,} bytes")
    return out


# --------------------------------------------------------------------------
# squashfs
# --------------------------------------------------------------------------
def parse_squashfs_super(img: bytes) -> dict | None:
    if len(img) < 96 or img[:4] != SQUASHFS_MAGIC:
        return None
    (_, inodes, _, bsize, frags) = struct.unpack("<IIIII", img[:20])
    (comp, blog, flags, noids, vmaj, vmin) = struct.unpack("<HHHHHH", img[20:32])
    root, used = struct.unpack("<QQ", img[32:48])
    (idt, xattr, inode_t, dir_t, frag_t, lookup_t) = struct.unpack("<6Q", img[48:96])
    return {"inodes": inodes, "block_size": bsize, "frags": frags,
            "comp": comp, "ver": f"{vmaj}.{vmin}", "bytes_used": used,
            "inode_table": inode_t}


def verify_metadata_block(img: bytes, off: int) -> str:
    """校验 inode_table 首块可否按 comp id 解压（zlib/xz/lzma/lzo 试探）。"""
    if off + 2 > len(img):
        return "out-of-range"
    hdr = struct.unpack("<H", img[off:off + 2])[0]
    if hdr & 0x8000:
        return "ok (uncompressed block)"
    size = hdr & 0x7FFF
    raw = img[off + 2: off + 2 + size]
    try:
        out = lzma.decompress(raw)
        return f"ok (xz, {len(out)} B)"
    except Exception:
        pass
    try:
        out = zlib.decompress(raw)
        return f"ok (zlib, {len(out)} B)"
    except Exception:
        pass
    try:
        filt = [{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 23}]
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filt)
        out = dec.decompress(raw)
        return f"ok (lzma-raw, {len(out)} B)"
    except Exception:
        return "FAILED to decode"


def carve_squashfs(blob: bytes) -> tuple[bytes, int] | None:
    pos = blob.find(SQUASHFS_MAGIC)
    while pos >= 0:
        sb = parse_squashfs_super(blob[pos:pos + 96])
        if sb and sb["ver"] == "4.0" and 1 <= sb["comp"] <= 6:
            used = sb["bytes_used"]
            if 0 < used <= len(blob) - pos:
                return blob[pos: pos + used], pos
        pos = blob.find(SQUASHFS_MAGIC, pos + 4)
    return None


def count_entries(root: Path) -> int:
    """统计目录树条目数（含符号链接，不跟随）。"""
    n = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    n += 1
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
        except OSError:
            pass
    return n


def run_unsquashfs(u: str, sp: Path, out: Path) -> None:
    """执行 unsquashfs，完整输出落盘+智能摘录；rc!=0 但已解出内容时宽容处理。"""
    dest = out / (sp.stem + "_rootfs")
    log(f"[*] unsquashfs {sp.name} -> {dest}")
    r = subprocess.run(
        [u, "-d", str(dest), "-f", "-no-progress", "-no-xattrs", str(sp)],
        capture_output=True, text=True)
    merged = (r.stdout or "") + (r.stderr or "")
    logfile = out / (sp.stem + ".unsquashfs.log")
    logfile.write_text(merged, encoding="utf-8", errors="replace")
    lines = [ln for ln in merged.splitlines() if ln.strip()]
    interesting = [ln for ln in lines
                   if any(k in ln.lower() for k in ("fail", "error", "xattr", "warning"))]
    for ln in lines[:5]:
        log("    " + ln)
    for ln in interesting[:20]:
        log("    ! " + ln)
    for ln in lines[-8:]:
        log("    " + ln)
    n = count_entries(dest) if dest.is_dir() else 0
    if r.returncode != 0:
        if n > 100:
            log(f"[i] unsquashfs rc={r.returncode} but tree has {n} entries "
                f"-> accepted (details: {logfile.name})")
        else:
            log(f"[!] unsquashfs rc={r.returncode} and tree only has {n} entries")
            raise RuntimeError(f"unsquashfs failed rc={r.returncode} entries={n}")
    else:
        log(f"[+] unsquashfs OK, {n} entries")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="360T7 UBI firmware extractor")
    ap.add_argument("firmware", help="firmware file, e.g. 360T7.bin")
    ap.add_argument("-o", "--outdir", default="out", help="output dir (default: out)")
    ap.add_argument("--no-unsquashfs", action="store_true",
                    help="skip running unsquashfs even if available")
    args = ap.parse_args()

    fw = Path(args.firmware)
    if not fw.is_file():
        log(f"[!] firmware not found: {fw}")
        return 2
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)

    data = fw.read_bytes()
    log(f"[*] firmware: {fw.name}  {len(data):,} bytes ({len(data)/1048576:.2f} MiB)")

    first = find_first_ubi(data)
    problems = []
    if first < 0:
        log("[!] no 'UBI#' found, try direct squashfs carve")
        res = carve_squashfs(data)
        if not res:
            log("[!] no squashfs either. abort.")
            return 1
        img, pos = res
        p = out / "rootfs.squashfs"
        p.write_bytes(img)
        log(f"[+] direct carve @0x{pos:X} -> {p} ({len(img):,} B)")
    else:
        peb = detect_peb_size(data, first)
        vid_off, data_off = detect_vid_data_offsets(data, first, peb)
        log(f"[+] UBI @0x{first:X}  PEB=0x{peb:X} ({peb//1024}K)  "
            f"VID@0x{vid_off:X}  DATA@0x{data_off:X}")

        vols = rebuild_volumes(data, first, peb, vid_off, data_off)
        for vid_id, blob in vols.items():
            vp = out / f"vol_{vid_id}.bin"
            vp.write_bytes(blob)
            log(f"[+] saved {vp} ({len(blob):,} B)")

        got = False
        for vid_id, blob in vols.items():
            res = carve_squashfs(blob)
            if not res:
                continue
            img, pos = res
            sb = parse_squashfs_super(img)
            meta = verify_metadata_block(img, sb["inode_table"])
            log(f"[+] squashfs in vol {vid_id} @0x{pos:X}: "
                f"ver={sb['ver']} comp={sb['comp']} inodes={sb['inodes']} "
                f"meta-block: {meta}")
            if meta.startswith("FAILED"):
                problems.append(f"vol{vid_id}:squashfs-metadata")
            sp = out / f"rootfs_from_vol{vid_id}.squashfs"
            sp.write_bytes(img)
            log(f"[+] saved {sp} ({len(img):,} B)")
            got = True
        if not got:
            log("[!] no squashfs found inside any volume")
            return 1

    # ---- unsquashfs ----
    if not args.no_unsquashfs:
        u = shutil.which("unsquashfs")
        if not u:
            log("[i] unsquashfs not found in PATH; install squashfs-tools to unpack.")
        else:
            try:
                for sp in sorted(out.glob("*.squashfs")):
                    run_unsquashfs(u, sp, out)
            except RuntimeError as e:
                problems.append(str(e))

    log("")
    if problems:
        log("[!] WARNING: " + "; ".join(problems))
        return 1
    log("[+] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
