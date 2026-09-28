#!/usr/bin/env python3
"""Build one Sega CD disc that plays several separately encoded clips in order.

Every clip is a normal profile: it is simulated, packed and given its own
profile-specialized player exactly like a single-movie disc. This tool builds
each profile as clip ``n`` of ``N`` (``make chain-part``) and collects three
files per clip into one ISO:

``CLIPnnH.DAT``  the clip's ``HEADER.DAT``
``CLIPnnB.DAT``  the clip's ``BODY.DAT``
``CLIPnnP.DAT``  the clip's player: Sub and Main images plus a small header

Clip 1's boot image goes into the disc system area, so the console boots clip
1 normally. At the end of each clip the running player loads the next clip's
``CLIPnnP.DAT``, replaces itself with that player and starts the next clip
from its own startup sequence. The final clip holds its last frame for 15
seconds and then wraps to clip 1.

Usage::

    tools/python.sh tools/build_chain.py --name trailer-chain --debug \\
        profiles/trailer-part1.toml profiles/trailer-part2.toml
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECTOR = 2048

# Player file layout. Keep in sync with chain_next in boot/movieplay_sp.s and
# md_chain_tramp in boot/movieplay_ip.s.
PLAYER_MAGIC = b"CAVP"
PLAYER_VERSION = 1
PLAYER_HEADER = struct.Struct(">4sHHHHLLLLLL")
SP_BASE = 0x6000
SP_LIMIT = 0x7400
SP_CHAIN_VECTOR = 0x2E
IP_LIMIT = 0x4A00
PLAYER_MAX_SECTORS = 16
MAX_CLIPS = 99
# 74-minute CD-R, less a margin for the outer edge.
DISC_WARN_BYTES = 650 * 1024 * 1024


def round_up(value: int, unit: int) -> int:
    return (value + unit - 1) // unit * unit


def build_player_file(index: int, count: int, sp_image: bytes,
                      ip_image: bytes, ip_entry: int) -> bytes:
    """Return the complete, sector-padded CLIPnnP.DAT contents."""
    if not sp_image or len(sp_image) > SP_LIMIT - SP_BASE:
        raise SystemExit(f"clip {index}: Sub image is {len(sp_image)} bytes")
    if sp_image[:4] != b"MAIN":
        raise SystemExit(f"clip {index}: Sub image has no module header")
    if not ip_image or len(ip_image) > IP_LIMIT:
        raise SystemExit(f"clip {index}: Main image is {len(ip_image)} bytes")
    if not 0 < ip_entry < len(ip_image):
        raise SystemExit(f"clip {index}: ip_entry offset 0x{ip_entry:X} is outside the image")
    sp_off = SECTOR
    ip_off = sp_off + round_up(len(sp_image), SECTOR)
    header = PLAYER_HEADER.pack(
        PLAYER_MAGIC, PLAYER_VERSION, index, count, 0,
        sp_off, len(sp_image), ip_off, len(ip_image), ip_entry,
        SP_CHAIN_VECTOR)
    out = bytearray(ip_off + round_up(len(ip_image), SECTOR))
    out[:len(header)] = header
    out[sp_off:sp_off + len(sp_image)] = sp_image
    out[ip_off:ip_off + len(ip_image)] = ip_image
    if len(out) > PLAYER_MAX_SECTORS * SECTOR:
        raise SystemExit(f"clip {index}: player file is {len(out)} bytes")
    return bytes(out)


def profile_stem(python: str, config: Path) -> str:
    return subprocess.run(
        [python, "tools/encode_config.py", str(config), "--print-stem"],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("configs", nargs="+", type=Path, help="clip profiles in play order")
    ap.add_argument("--name", required=True, help="disc name: out/NAME.iso")
    ap.add_argument("--debug", action="store_true", help="DEBUG=1 players with the HUD")
    ap.add_argument("--region", default="jp", help="SECURITY_REGION for every clip")
    ap.add_argument("--python", default="tools/python.sh")
    args = ap.parse_args()

    count = len(args.configs)
    if count > MAX_CLIPS:
        raise SystemExit(f"at most {MAX_CLIPS} clips fit the CLIPnn names")
    python = args.python
    stems = [profile_stem(python, cfg) for cfg in args.configs]
    if len(set(stems)) != len(stems):
        raise SystemExit("each clip needs its own profile; a stem appears twice")

    suffix = ("" if args.region == "jp" else f"_{args.region}") + ("" if args.debug else "_release")
    stage = ROOT / "tmp" / f"chain-{args.name}" / "disc"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    boot_image = stage.parent / "boot.bin"
    make = os.environ.get("MAKE", "make")

    for index, (cfg, stem) in enumerate(zip(args.configs, stems), start=1):
        print(f"== clip {index}/{count}: {cfg}", flush=True)
        subprocess.run(
            [make, "chain-part", f"CONFIG={cfg}", f"CLIP_INDEX={index}",
             f"CLIP_COUNT={count}", f"DEBUG={1 if args.debug else 0}",
             f"SECURITY_REGION={args.region}"],
            cwd=ROOT, check=True)
        stream = ROOT / "out" / stem
        build = ROOT / "tmp" / stem / "build"
        name = f"CLIP{index:02d}"
        shutil.copyfile(stream / "HEADER.DAT", stage / f"{name}H.DAT")
        shutil.copyfile(stream / "BODY.DAT", stage / f"{name}B.DAT")
        ip_entry = int((build / "ip_entry.sym").read_text().split()[0], 16)
        player = build_player_file(
            index, count, (build / "movieplay_sp.bin").read_bytes(),
            (build / "movieplay_ip.bin").read_bytes(), ip_entry)
        (stage / f"{name}P.DAT").write_bytes(player)
        if index == 1:
            shutil.copyfile(build / "movieplay_boot.bin", boot_image)

    (stage / "README.TXT").write_text(f"CAVC multi-clip disc, {count} clips\n")
    iso = ROOT / "out" / f"{args.name}{suffix}.iso"
    cue = iso.with_suffix(".cue")
    iso.unlink(missing_ok=True)
    cue.unlink(missing_ok=True)
    mkisofs = shutil.which("mkisofs") or shutil.which("genisoimage")
    if not mkisofs:
        raise SystemExit("missing mkisofs/genisoimage")
    subprocess.run(
        [mkisofs, "-iso-level", "1", "-G", str(boot_image), "-pad",
         "-V", "SCFMV_CHAIN", "-o", str(iso), str(stage)],
        check=True)
    cue.write_text(f'FILE "{iso.name}" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n')
    size = iso.stat().st_size
    print(f"disc: {iso} ({size / 1048576:.1f} MiB, {count} clips)")
    if size > DISC_WARN_BYTES:
        print("warning: the image exceeds 650 MiB; outer-edge reads are the least reliable",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
