#!/usr/bin/env python3
"""
MULTIPLE build script

Patches a ChromeOS factory shim's ROOT-A with the MULTIPLE menu,
which adds dev-mode toggle, enrollment management, and a liability
warning on top of the standard KRAFT tools (TPM reset, GBB, Cr50).

Supports all boards in boards/boards.json.
Intended to run in CI. Pass --local to run locally.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ── paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
MENU_PATH    = os.path.join(SCRIPT_DIR, "src", "menu.sh")
STARTUP_PATH = os.path.join(SCRIPT_DIR, "src", "startup.conf")
STUB_PATH    = os.path.join(SCRIPT_DIR, "src", "stub.sh")
BANNER_PATH  = os.path.join(SCRIPT_DIR, "bootloader", "ui", "banner.txt")
BOARDS_JSON  = os.path.join(SCRIPT_DIR, "boards", "boards.json")
DIST_DIR     = os.path.join(SCRIPT_DIR, "dist")

SHIM_MIRRORS = [
    "https://dl.cros.download/files/{board}/{board}.zip",
]
FALLBACK_SHIM_MIRRORS: list[str] = []

HTTP_TIMEOUT         = 60
DOWNLOAD_SIZE_LIMIT  = 8 * 1024 * 1024 * 1024
MIN_FREE_SPACE_BYTES = 10 * 1024 * 1024 * 1024

_SLOT_BLOCKLIST = {
    "/sbin/init",
    "/sbin/chromeos_startup",
    "/sbin/chromeos_startup.sh",
    "/etc/init/startup.conf",
    "/usr/sbin/factory_install.sh",
    "/usr/sbin/factory_reset.sh",
}

_SLOT_PREFER = [
    "/usr/sbin/chromeos-install",
    "/usr/bin/cros_payload",
    "/usr/sbin/write_gpt.sh",
    "/usr/sbin/dump_vpd_log",
    "/usr/sbin/display_boot_message",
    "/usr/sbin/secure-wipe.sh",
]

_BOARD_RE = re.compile(r"^[a-z0-9_\-]+$")

BOARD_ALIASES: dict[str, str] = {
    "craasneto": "nissa",
    "craask":    "nissa",
}


# ── board helpers ─────────────────────────────────────────────────────────────

def read_boards_file() -> dict:
    if not os.path.exists(BOARDS_JSON):
        raise FileNotFoundError(f"boards.json missing at {BOARDS_JSON}")
    with open(BOARDS_JSON, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    board_keys = [k for k in data if not k.startswith("_")]
    if not board_keys:
        raise RuntimeError("No boards found in boards/boards.json")
    for k in board_keys:
        v = data.get(k)
        if not isinstance(v, dict) or "name" not in v or "family" not in v or "arch" not in v:
            raise RuntimeError(f"Invalid entry for board '{k}' in boards.json")
    return data


def sanitize_board(board_in: str) -> str:
    if not isinstance(board_in, str):
        raise RuntimeError("Board must be a string")
    b = board_in.strip().lower()
    resolved = BOARD_ALIASES.get(b, b)
    if not _BOARD_RE.match(resolved):
        raise RuntimeError(f"Invalid board name: '{board_in}'")
    return resolved


def ensure_free_space(path: str, need: int = MIN_FREE_SPACE_BYTES) -> None:
    root = os.path.abspath(path)
    while not os.path.exists(root):
        root = os.path.dirname(root)
        if root == "/":
            break
    usage = shutil.disk_usage(root)
    if usage.free < need:
        raise RuntimeError(
            f"Not enough free disk space in {root}: need {need} bytes, have {usage.free} bytes"
        )


# ── download helpers ──────────────────────────────────────────────────────────

def http_download(url: str, dest_path: str, timeout: int = HTTP_TIMEOUT, board: str = "") -> None:
    req = Request(url, headers={"User-Agent": "MULTIPLE-build/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            total = 0
            last_print = 0.0
            content_length = resp.headers.get("Content-Length")
            total_expected = int(content_length) if content_length else None
            with open(dest_path, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
                    if total > DOWNLOAD_SIZE_LIMIT:
                        raise RuntimeError(f"Download exceeded size limit")
                    now = time.monotonic()
                    if now - last_print >= 5:
                        mb = total / 1024 / 1024
                        if total_expected:
                            pct = total / total_expected * 100
                            print(f"[build:{board}]   {mb:.0f} MB / {total_expected/1024/1024:.0f} MB ({pct:.1f}%)")
                        else:
                            print(f"[build:{board}]   {mb:.0f} MB downloaded...")
                        last_print = now
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} downloading {url}: {e.reason}")
    except URLError as e:
        raise RuntimeError(f"URL error downloading {url}: {e.reason}")
    except Exception as e:
        raise RuntimeError(f"Failed to download {url}: {e}")


def download_shim(board: str, dest_dir: str) -> str:
    mirrors = list(SHIM_MIRRORS)
    last_exc = None
    for template in mirrors:
        url = template.format(board=board)
        dest = os.path.join(dest_dir, f"{board}.zip")
        print(f"[build:{board}] downloading shim: {url}")
        try:
            http_download(url, dest, board=board)
            if os.path.getsize(dest) == 0:
                os.remove(dest)
                raise RuntimeError("Downloaded file is empty")
            if zipfile.is_zipfile(dest):
                return dest
            raw = dest.replace(".zip", ".bin")
            os.rename(dest, raw)
            return raw
        except Exception as e:
            last_exc = e
            print(f"[build:{board}] download failed: {e}")
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except Exception:
                    pass
            time.sleep(1)
    raise RuntimeError(f"All shim mirrors failed. Last: {last_exc}")


# ── zip / bin helpers ─────────────────────────────────────────────────────────

def _safe_extract_to(zf: zipfile.ZipFile, member: str, dest_dir: str) -> str:
    bn = os.path.basename(member)
    if not bn:
        raise RuntimeError("Invalid zip member name")
    out = os.path.join(dest_dir, bn)
    with zf.open(member) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def choose_bin_from_zip(zf: zipfile.ZipFile, board: str) -> str:
    names = [n for n in zf.namelist() if n.lower().endswith(".bin")]
    if not names:
        raise RuntimeError("No .bin entries found in shim zip")
    lc = [n.lower() for n in names]
    for i, n in enumerate(lc):
        if os.path.basename(n) == f"{board}.bin":
            return names[i]
    for i, n in enumerate(lc):
        if "chromeos" in n or "factory" in n or "recovery" in n:
            return names[i]
    if len(names) == 1:
        return names[0]
    raise RuntimeError(f"Multiple .bin files in zip; cannot auto-choose. Provide explicit shim.")


def extract_bin(path: str, dest_dir: str, board: str) -> str:
    if path.endswith(".bin"):
        return path
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"{os.path.basename(path)} is not a valid zip")
    with zipfile.ZipFile(path, "r") as zf:
        try:
            member = choose_bin_from_zip(zf, board)
        except Exception:
            nested = [n for n in zf.namelist() if n.lower().endswith(".zip")]
            if len(nested) == 1:
                inner = _safe_extract_to(zf, nested[0], dest_dir)
                return extract_bin(inner, dest_dir, board)
            raise
        return _safe_extract_to(zf, member, dest_dir)


# ── GPT / partition helpers ───────────────────────────────────────────────────

def read_uint64_le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def _run_checked(cmd: list[str], board: str, accepted_codes: tuple[int, ...] = (0,)) -> None:
    print(f"[build:{board}] $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"[build:{board}] missing utility: {cmd[0]}") from exc
    if proc.returncode not in accepted_codes:
        raise RuntimeError(f"[build:{board}] command failed (exit {proc.returncode}): {cmd[0]}")


def _read_gpt(path: str) -> tuple[bytearray, list[dict], int, int]:
    sector = 512
    fsize  = os.path.getsize(path)
    if fsize < 34 * sector:
        raise RuntimeError("Image too small to contain GPT")
    with open(path, "rb") as f:
        f.seek(sector)
        header = bytearray(f.read(92))
        if header[:8] != b"EFI PART":
            raise RuntimeError("No GPT header found")
        entry_lba  = struct.unpack_from("<Q", header, 72)[0]
        count      = struct.unpack_from("<I", header, 80)[0]
        entry_size = struct.unpack_from("<I", header, 84)[0]
        if count <= 0 or entry_size < 128 or entry_size > 4096:
            raise RuntimeError("Invalid GPT dimensions")
        table_bytes = count * entry_size
        f.seek(entry_lba * sector)
        table = f.read(table_bytes)
    entries = []
    for i in range(count):
        raw   = bytearray(table[i * entry_size:(i + 1) * entry_size])
        if len(raw) < 128:
            break
        first = struct.unpack_from("<Q", raw, 32)[0]
        last  = struct.unpack_from("<Q", raw, 40)[0]
        if first == 0 and last == 0:
            continue
        if first > last or last * sector >= fsize:
            raise RuntimeError(f"Invalid GPT bounds at entry {i}")
        name = raw[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        entries.append({"index": i, "raw": raw, "start": first, "end": last, "name": name})
    entries.sort(key=lambda e: (e["start"], e["index"]))
    return header, entries, entry_lba, entry_size


def _crc32c(data: bytes, crc: int = 0xFFFFFFFF) -> int:
    crc &= 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if (crc & 1) else 0)
    return crc ^ 0xFFFFFFFF


def _restore_chromeos_ro_compat(path: str, original_bits: int, board: str) -> None:
    if not original_bits:
        return

    def patch_superblock(offset: int) -> None:
        with open(path, "r+b") as f:
            f.seek(offset)
            data = bytearray(f.read(1024))
            if len(data) != 1024 or struct.unpack_from("<H", data, 0x38)[0] != 0xEF53:
                return
            current = struct.unpack_from("<I", data, 0x64)[0]
            struct.pack_into("<I", data, 0x64, current | original_bits)
            if current & 0x00000400:
                struct.pack_into("<I", data, 0x3FC, 0)
                struct.pack_into("<I", data, 0x3FC, _crc32c(bytes(data[:0x3FC])))
            f.seek(offset)
            f.write(data)

    patch_superblock(1024)
    try:
        proc = subprocess.run(
            ["dumpe2fs", "-h", path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        for line in proc.stdout.splitlines():
            if "Superblock backups stored on blocks:" not in line:
                continue
            for token in line.split(":", 1)[1].strip().split():
                try:
                    block = int(token.rstrip(","))
                except ValueError:
                    continue
                if block > 0:
                    with open(path, "rb") as f:
                        f.seek(1024)
                        sb = bytearray(f.read(1024))
                    block_size = 1024 << struct.unpack_from("<I", sb, 0x18)[0]
                    patch_superblock(block * block_size)
            break
    except FileNotFoundError:
        pass


def _filesystem_min_size(rootfs_path: str, board: str) -> int:
    if shutil.which("e2fsck") is None or shutil.which("resize2fs") is None:
        raise RuntimeError("e2fsprogs required (e2fsck and resize2fs)")
    with open(rootfs_path, "rb") as f:
        f.seek(1024 + 0x64)
        original_ro_compat = struct.unpack("<I", f.read(4))[0]
    chromeos_bits = original_ro_compat & 0xFF000000
    if chromeos_bits:
        with open(rootfs_path, "r+b") as f:
            f.seek(1024 + 0x64)
            f.write(struct.pack("<I", original_ro_compat & 0x00FFFFFF))
        print(f"[build:{board}] temporarily cleared ChromeOS ext4 marker 0x{chromeos_bits:08x} for resize")
    try:
        _run_checked(["e2fsck", "-fy", rootfs_path], board, accepted_codes=(0, 1))
        _run_checked(["resize2fs", "-M", rootfs_path], board)
    finally:
        if chromeos_bits:
            _restore_chromeos_ro_compat(rootfs_path, chromeos_bits, board)
    size = os.path.getsize(rootfs_path)
    return (size + 511) // 512


def find_root_lba(path: str) -> Tuple[int, int]:
    fsize = os.path.getsize(path)
    if fsize < 2048:
        raise RuntimeError("Image too small to contain GPT")
    with open(path, "rb") as f:
        header = f.read(34 * 512)
    if header[512:520] != b"EFI PART":
        raise RuntimeError("No valid GPT signature found at LBA1")
    gpt      = header[512:512 + 92]
    part_lba = read_uint64_le(gpt, 72)
    num      = struct.unpack_from("<I", gpt, 80)[0]
    esz      = struct.unpack_from("<I", gpt, 84)[0]
    if num == 0 or esz == 0:
        raise RuntimeError("Invalid GPT partition header values")
    with open(path, "rb") as f:
        f.seek(part_lba * 512)
        entries = f.read(num * esz)
    roots = []
    for i in range(min(num, 128)):
        off   = i * esz
        if off + esz > len(entries):
            break
        e     = entries[off:off + esz]
        start = struct.unpack_from("<Q", e, 32)[0]
        end   = struct.unpack_from("<Q", e, 40)[0]
        name  = e[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        if start and end and start < end and end * 512 <= fsize and "ROOT" in name:
            roots.append((end - start, start, end, name))
    if not roots:
        raise RuntimeError("No valid ROOT partition found in GPT")
    root_a = [r for r in roots if r[3].upper() == "ROOT-A"]
    _, start, end, _ = root_a[0] if root_a else sorted(roots, key=lambda x: x[0], reverse=True)[0]
    return int(start), int(end)


# ── ext2 helper ───────────────────────────────────────────────────────────────

class Ext2FS:
    def __init__(self, img: bytearray):
        self.img = img
        if len(img) < 2048:
            raise RuntimeError("Image too small for ext2 superblock")
        sb = img[1024:1024 + 256]
        s_log_block_size = struct.unpack_from("<I", sb, 24)[0]
        self.BS  = 1024 << (s_log_block_size & 0xFFFFFFFF)
        self.ipg = struct.unpack_from("<I", sb, 40)[0]
        self.isz = struct.unpack_from("<H", sb, 88)[0]
        self.gdt = struct.unpack_from("<I", sb, 20)[0] + 1
        if self.BS <= 0 or self.isz <= 0:
            raise RuntimeError("Invalid ext2 block or inode size")

    def _bo(self, n: int) -> int:  return n * self.BS
    def _rb(self, n: int) -> bytes:
        o = self._bo(n)
        if o + self.BS > len(self.img):
            raise RuntimeError("Block read out of range")
        return bytes(self.img[o:o + self.BS])

    def _ioff(self, ino: int) -> int:
        g      = (ino - 1) // self.ipg
        idx    = (ino - 1) % self.ipg
        gd_off = self.gdt * self.BS + g * 32
        if gd_off + 32 > len(self.img):
            raise RuntimeError("Group descriptor out of range")
        gd    = self.img[gd_off:gd_off + 32]
        block = struct.unpack_from("<I", gd, 8)[0]
        o     = block * self.BS + idx * self.isz
        if o + self.isz > len(self.img):
            raise RuntimeError("Inode table out of range")
        return o

    def _ri(self, ino: int) -> bytes:
        o = self._ioff(ino)
        return bytes(self.img[o:o + self.isz])

    def _blk(self, inode: bytes) -> list:
        blocks = []
        for i in range(12):
            b = struct.unpack_from("<I", inode, 40 + i * 4)[0]
            if b:
                blocks.append(b)
        single = struct.unpack_from("<I", inode, 40 + 12 * 4)[0]
        if single:
            data  = self._rb(single)
            count = self.BS // 4
            for i in range(count):
                ptr = struct.unpack_from("<I", data, i * 4)[0]
                if ptr:
                    blocks.append(ptr)
        return blocks

    def _dir(self, bn: int):
        blk = self._rb(bn)
        entries = []
        off = 0
        while off + 8 <= len(blk):
            ino = struct.unpack_from("<I", blk, off)[0]
            rec = struct.unpack_from("<H", blk, off + 4)[0]
            if rec == 0:
                break
            nl = blk[off + 6]
            if ino:
                name = blk[off + 8:off + 8 + nl].decode("utf-8", "replace")
                entries.append((ino, name))
            off += rec
        return entries

    def find(self, path: str) -> Optional[int]:
        curr = 2
        for part in [p for p in path.split("/") if p]:
            found = False
            inode = self._ri(curr)
            for b in self._blk(inode):
                for ino, name in self._dir(b):
                    if name == part:
                        curr  = ino
                        found = True
                        break
                if found:
                    break
            if not found:
                return None
        return curr

    def read(self, ino: int) -> bytes:
        inode = self._ri(ino)
        size  = struct.unpack_from("<I", inode, 4)[0]
        data  = b"".join(self._rb(b) for b in self._blk(inode))
        return data[:size]

    def cap(self, ino: int) -> int:
        return len(self._blk(self._ri(ino))) * self.BS

    def write(self, ino: int, data: bytes, preserve_size: bool = False) -> None:
        inode  = self._ri(ino)
        blocks = self._blk(inode)
        if len(data) > len(blocks) * self.BS:
            raise ValueError("data too large for slot")
        rem = bytearray(data)
        for blk in blocks:
            o     = self._bo(blk)
            chunk = bytes(rem[:self.BS]).ljust(self.BS, b"\x00")
            if o + self.BS > len(self.img):
                raise RuntimeError("Write out of range")
            self.img[o:o + self.BS] = chunk
            rem = rem[self.BS:]
            if not rem:
                break
        if not preserve_size:
            struct.pack_into("<I", self.img, self._ioff(ino) + 4, len(data))


# ── slot scanning ─────────────────────────────────────────────────────────────

def _scan_shell_slots(fs: Ext2FS):
    results = []
    visited: set[int] = set()

    def _walk(ino: int, path: str) -> None:
        if ino in visited:
            return
        visited.add(ino)
        try:
            inode = fs._ri(ino)
            mode  = struct.unpack_from("<H", inode, 0)[0]
            ftype = mode & 0xF000
            if ftype == 0x8000:
                if fs._blk(inode):
                    try:
                        if fs.read(ino)[:2] == b"#!":
                            results.append((fs.cap(ino), path, ino))
                    except Exception:
                        pass
            elif ftype == 0x4000:
                for blk in fs._blk(inode):
                    for cino, cname in fs._dir(blk):
                        if cname not in (".", ".."):
                            _walk(cino, path + "/" + cname)
        except Exception:
            pass

    _walk(2, "")
    results.sort(reverse=True)
    return results


def _pick_slot(fs: Ext2FS, menu_size: int):
    slots      = _scan_shell_slots(fs)
    candidates = [(cap, path, ino) for cap, path, ino in slots
                  if cap >= menu_size and path not in _SLOT_BLOCKLIST]
    if not candidates:
        raise RuntimeError("No shell slot large enough for menu")
    prefer_index = {p: i for i, p in enumerate(_SLOT_PREFER)}
    candidates.sort(key=lambda x: (prefer_index.get(x[1], len(_SLOT_PREFER)), -x[0]))
    return candidates[0][1], candidates[0][2]


# ── rootfs patching ───────────────────────────────────────────────────────────

def patch_rootfs(img: bytearray, menu: bytes, startup: bytes, banner: bytes, board: str) -> bytearray:
    fs = Ext2FS(img)
    slot_path, slot_ino = _pick_slot(fs, len(menu))
    print(f"[build:{board}] menu -> {slot_path} ({fs.cap(slot_ino)} b, {len(menu)} b needed)")
    fs.write(slot_ino, menu)

    real_stub = f"#!/bin/bash\nexec {slot_path}\n".encode()
    for stub_path in ["/usr/sbin/factory_install.sh", "/usr/sbin/factory_reset.sh"]:
        if stub_path == slot_path:
            continue
        ino = fs.find(stub_path)
        if ino:
            try:
                fs.write(ino, real_stub, preserve_size=True)
                print(f"[build:{board}] stub -> {stub_path}")
            except ValueError:
                pass

    ino_i = fs.find("/etc/issue")
    if ino_i and fs.cap(ino_i) >= len(banner):
        fs.write(ino_i, banner)
        print(f"[build:{board}] banner -> /etc/issue")

    s_ino = fs.find("/etc/init/startup.conf")
    if s_ino and fs.cap(s_ino) >= len(startup):
        fs.write(s_ino, startup)
        print(f"[build:{board}] startup.conf patched")

    return img


# ── kernel version patch ──────────────────────────────────────────────────────

def patch_kernel_version(path: str, board: str) -> None:
    sector = 512
    _, entries, _, _ = _read_gpt(path)
    kern_entry = next((e for e in entries if e["name"].upper() in ("KERN-A", "KERNEL-A")), None)
    if kern_entry is None:
        print(f"[build:{board}] WARNING: no KERN-A partition found, skipping kernel version patch")
        return

    kern_start = kern_entry["start"] * sector
    kern_size  = (kern_entry["end"] - kern_entry["start"] + 1) * sector

    with open(path, "rb") as f:
        f.seek(kern_start)
        head = f.read(4096)

    if len(head) < 48:
        print(f"[build:{board}] WARNING: KERN-A too small to parse, skipping")
        return

    VBOOT1_MAGIC = b"CHROMEOS"
    VBOOT2_MAGIC = b"VBKP"

    if head[:8] == VBOOT1_MAGIC:
        # VbKeyBlockHeader: magic(8) + padding(32) + keyblock_size(8) at offset 40
        keyblock_size = struct.unpack_from("<Q", head, 40)[0]
        if keyblock_size < 64 or keyblock_size > 1024 * 1024:
            print(f"[build:{board}] WARNING: vboot1 keyblock_size {keyblock_size} out of range, skipping")
            return
        kernel_version_off = keyblock_size + 0x20
        print(f"[build:{board}] vboot1 keyblock detected, keyblock_size={keyblock_size}")
    elif head[:4] == VBOOT2_MAGIC:
        kernel_version_off = 0x10
        print(f"[build:{board}] vboot2 preamble detected (VBKP)")
    else:
        print(f"[build:{board}] WARNING: unknown vboot magic ({head[:16].hex()}), skipping kernel version patch")
        return

    read_size = min(max(kernel_version_off + 8, 4096), kern_size)
    with open(path, "rb") as f:
        f.seek(kern_start)
        kern_blob = bytearray(f.read(read_size))

    if kernel_version_off + 8 > len(kern_blob):
        print(f"[build:{board}] WARNING: could not read full preamble, skipping")
        return

    current_version = struct.unpack_from("<Q", kern_blob, kernel_version_off)[0]
    if current_version == 1:
        print(f"[build:{board}] KERN-A version already 1, no patch needed")
        return

    print(f"[build:{board}] KERN-A kernel version: {current_version} -> 1 (keyroll fix)")
    struct.pack_into("<Q", kern_blob, kernel_version_off, 1)
    with open(path, "r+b") as f:
        f.seek(kern_start)
        f.write(kern_blob)


# ── output validation ─────────────────────────────────────────────────────────

def verify_zip(out_zip: str, inner_name: str, board: str) -> None:
    if not zipfile.is_zipfile(out_zip):
        raise RuntimeError(f"output zip is not valid: {out_zip}")
    with zipfile.ZipFile(out_zip, "r") as zf:
        entries = [n for n in zf.namelist() if n.lower().endswith(".bin")]
        if len(entries) != 1:
            raise RuntimeError(f"unexpected .bin count in output zip: {len(entries)}")
        if entries[0] != inner_name:
            raise RuntimeError(f"wrong inner bin name: {entries[0]} (expected {inner_name})")

        # A ChromeOS disk image is not the same thing as a ROOT-A filesystem
        # partition. Require a protective MBR and GPT header before calling a
        # .bin a disk image. This is a format sanity check, not proof that the
        # image is signed, bootable, or appropriate for a particular board.
        with zf.open(entries[0], "r") as image:
            header = image.read(1024)
        if len(header) < 1024 or header[510:512] != b"\x55\xaa" or header[512:520] != b"EFI PART":
            raise RuntimeError(
                "refusing to label this artifact a ChromeOS disk image: the packaged .bin "
                "does not contain a protective MBR and GPT header. The current build path "
                "packages ROOT-A alone, which is a filesystem partition, not a complete "
                "bootable disk image. No bootable image was produced."
            )


# ── main build ────────────────────────────────────────────────────────────────

def build(board_in: str, local_shim: Optional[str] = None) -> str:
    boards_cfg = read_boards_file()
    board = sanitize_board(board_in)

    if board not in boards_cfg:
        raise RuntimeError(f"[build:{board}] Board '{board}' not found in boards/boards.json")

    for p in [MENU_PATH, STARTUP_PATH, STUB_PATH, BANNER_PATH]:
        if not os.path.exists(p):
            raise FileNotFoundError(f"[build:{board}] Missing required file: {p}")

    ensure_free_space(SCRIPT_DIR)
    os.makedirs(DIST_DIR, exist_ok=True)

    out_zip  = os.path.join(DIST_DIR, f"multiple_{board}.zip")
    temp_dir = tempfile.mkdtemp(prefix=f"multiple-{board}-")

    try:
        # get shim
        if local_shim:
            if not os.path.exists(local_shim):
                raise FileNotFoundError(f"local shim not found: {local_shim}")
            shim_bin = extract_bin(local_shim, temp_dir, board)
        else:
            shim_zip = download_shim(board, temp_dir)
            shim_bin = extract_bin(shim_zip, temp_dir, board)

        shim_size = os.path.getsize(shim_bin)
        if shim_size < 1024:
            raise RuntimeError(f"shim too small: {shim_bin}")
        print(f"[build:{board}] shim: {shim_bin} ({shim_size // 1024 // 1024} MB)")

        # find ROOT-A and load it
        root_start, root_end = find_root_lba(shim_bin)
        p3_start = root_start * 512
        p3_size  = (root_end - root_start) * 512
        if p3_start < 0 or p3_start + p3_size > shim_size:
            raise RuntimeError(f"ROOT-A bounds out of range: start={p3_start} size={p3_size}")

        with open(shim_bin, "rb") as f:
            f.seek(p3_start)
            img = bytearray(f.read(p3_size))

        # patch rootfs
        with open(MENU_PATH,    "rb") as f: menu    = f.read()
        with open(STARTUP_PATH, "rb") as f: startup = f.read()
        with open(BANNER_PATH,  "rb") as f: banner  = f.read()
        img = patch_rootfs(img, menu, startup, banner, board)

        # write back
        with open(shim_bin, "r+b") as f:
            f.seek(p3_start)
            f.write(img)

        # ext2 sanity check
        with open(shim_bin, "rb") as f:
            f.seek(p3_start + 0x438)
            magic = struct.unpack("<H", f.read(2))[0]
        if magic != 0xEF53:
            raise RuntimeError(f"ext2 magic check failed: 0x{magic:04x}")

        # patch kernel version (keyroll fix)
        patch_kernel_version(shim_bin, board)

        # extract and shrink ROOT-A
        root_start2, root_end2 = find_root_lba(shim_bin)
        roota_start = root_start2 * 512
        roota_size  = (root_end2 - root_start2) * 512
        roota_tmp   = os.path.join(temp_dir, f"roota_{board}.bin")
        with open(shim_bin, "rb") as f:
            f.seek(roota_start)
            with open(roota_tmp, "wb") as dst:
                remaining = roota_size
                while remaining:
                    chunk = f.read(min(16 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    remaining -= len(chunk)

        try:
            shrunk_sectors = _filesystem_min_size(roota_tmp, board)
            shrunk_size    = shrunk_sectors * 512
            with open(roota_tmp, "rb+") as f:
                f.truncate(shrunk_size)
            print(f"[build:{board}] ROOT-A shrunk: {roota_size//1024//1024} MB -> {shrunk_size//1024//1024} MB")
        except RuntimeError as exc:
            print(f"[build:{board}] WARNING: could not shrink ROOT-A ({exc}), using full partition")

        # package output
        inner_name = f"chromeos_multiple_{board}.bin"
        tmp_out    = out_zip + f".tmp{os.getpid()}"
        try:
            with zipfile.ZipFile(tmp_out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as zf:
                zf.write(roota_tmp, inner_name)
            verify_zip(tmp_out, inner_name, board)
            os.replace(tmp_out, out_zip)
        except Exception:
            # A failed validation must not leave an apparently usable artifact.
            try:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
            except OSError:
                pass
            raise

        print(f"[build:{board}] done: {out_zip} ({os.path.getsize(out_zip)//1024//1024} MB)")
        return out_zip

    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MULTIPLE build script")
    parser.add_argument("board", help="Board name (must match boards/boards.json)")
    parser.add_argument("local_shim", nargs="?", default=None,
                        help="Optional local shim .zip or .bin (skips download)")
    parser.add_argument("--local", action="store_true",
                        help="Run outside CI")
    args = parser.parse_args()

    in_ci = os.getenv("GITHUB_ACTIONS") == "true"
    if not in_ci and not args.local:
        print(
            "This script is intended for CI. Pass --local to run here:\n"
            "  python3 build.py <board> --local",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        build(args.board, args.local_shim)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
