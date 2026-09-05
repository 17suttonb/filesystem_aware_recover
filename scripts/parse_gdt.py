#!/usr/bin/env python3
"""Parse an ext4 group descriptor table directly, bypassing dumpe2fs/debugfs.

Both of those tools eagerly validate/read things (journal contents, bitmap
checksums) that aren't available yet on a partially-rescued image and simply
refuse to run at all. The GDT itself sits right after the superblock (a few KB
into the filesystem) and is plain, well-documented binary data, so we read it
ourselves instead.

Superblock parameters below were taken from `dumpe2fs -h` on the same
filesystem (see notes.md) rather than re-parsed here, since that part of
dumpe2fs works fine on a partial image.

Usage: parse_gdt.py <device-or-image> [--offset BYTES] [--json OUT]
"""
import argparse
import json
import struct
import sys

# --- superblock-derived constants (from dumpe2fs -h on lvroot) ---
BLOCK_SIZE = 4096
FIRST_DATA_BLOCK = 0
BLOCKS_PER_GROUP = 32768
INODES_PER_GROUP = 8192
INODE_SIZE = 256
GROUP_DESC_SIZE = 64  # 64bit feature -> extended (64-byte) group descriptors
RESERVED_GDT_BLOCKS = 1024
BLOCK_COUNT = 121962496
INODE_TABLE_BLOCKS_PER_GROUP = (INODES_PER_GROUP * INODE_SIZE) // BLOCK_SIZE  # 512

BG_INODE_UNINIT = 0x1
BG_BLOCK_UNINIT = 0x2
BG_INODE_ZEROED = 0x4

GDT_START_BLOCK = FIRST_DATA_BLOCK + 1  # block right after the superblock's block


def num_groups():
    import math
    return math.ceil(BLOCK_COUNT / BLOCKS_PER_GROUP)


GDT64_FORMAT = "<IIIHHHHIHHHHIIIHHHHIHHI"  # 64-byte ext4_group_desc, little-endian


def parse_group_desc(buf):
    # 64-byte ext4 group descriptor
    (bb_lo, ib_lo, it_lo, fb_lo, fi_lo, ud_lo, flags, exb_lo,
     bbcsum_lo, ibcsum_lo, itu_lo, checksum,
     bb_hi, ib_hi, it_hi, fb_hi, fi_hi, ud_hi, itu_hi, exb_hi,
     bbcsum_hi, ibcsum_hi, reserved) = struct.unpack_from(GDT64_FORMAT, buf, 0)
    block_bitmap = bb_lo | (bb_hi << 32)
    inode_bitmap = ib_lo | (ib_hi << 32)
    inode_table = it_lo | (it_hi << 32)
    free_blocks = fb_lo | (fb_hi << 16)
    free_inodes = fi_lo | (fi_hi << 16)
    return {
        "block_bitmap": block_bitmap,
        "inode_bitmap": inode_bitmap,
        "inode_table": inode_table,
        "free_blocks_count": free_blocks,
        "free_inodes_count": free_inodes,
        "flags": flags,
        "block_uninit": bool(flags & BG_BLOCK_UNINIT),
        "inode_uninit": bool(flags & BG_INODE_UNINIT),
        "inode_zeroed": bool(flags & BG_INODE_ZEROED),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("device")
    ap.add_argument("--offset", type=int, default=0,
                     help="byte offset of the filesystem start within the given file (default 0; use 0 when pointed at a loop device already isolating the LV)")
    ap.add_argument("--json", help="write full per-group data as JSON to this path")
    args = ap.parse_args()

    n = num_groups()
    gdt_bytes = n * GROUP_DESC_SIZE
    gdt_blocks = (gdt_bytes + BLOCK_SIZE - 1) // BLOCK_SIZE
    gdt_byte_offset = args.offset + GDT_START_BLOCK * BLOCK_SIZE

    print(f"groups: {n}, GDT: blocks {GDT_START_BLOCK}..{GDT_START_BLOCK + gdt_blocks - 1} "
          f"({gdt_blocks} blocks, {gdt_bytes} bytes) at byte offset {gdt_byte_offset}",
          file=sys.stderr)

    with open(args.device, "rb") as f:
        f.seek(gdt_byte_offset)
        raw = f.read(gdt_blocks * BLOCK_SIZE)
    if len(raw) < gdt_bytes:
        print(f"WARNING: short read of GDT ({len(raw)} < {gdt_bytes} bytes) — "
              f"GDT region may not be fully recovered yet", file=sys.stderr)

    groups = []
    for g in range(n):
        off = g * GROUP_DESC_SIZE
        if off + GROUP_DESC_SIZE > len(raw):
            break
        groups.append(parse_group_desc(raw[off:off + GROUP_DESC_SIZE]))

    n_block_uninit = sum(1 for g in groups if g["block_uninit"])
    n_inode_uninit = sum(1 for g in groups if g["inode_uninit"])
    n_inode_zeroed = sum(1 for g in groups if g["inode_zeroed"])
    print(f"parsed {len(groups)} group descriptors", file=sys.stderr)
    print(f"  block_uninit (bitmap is synthetic-all-free, no read needed): {n_block_uninit}", file=sys.stderr)
    print(f"  inode_uninit (inode table has no live inodes):               {n_inode_uninit}", file=sys.stderr)
    print(f"  inode_zeroed (inode table blocks never written on disk):     {n_inode_zeroed}", file=sys.stderr)

    if args.json:
        with open(args.json, "w") as f:
            json.dump({
                "block_size": BLOCK_SIZE,
                "blocks_per_group": BLOCKS_PER_GROUP,
                "inode_table_blocks_per_group": INODE_TABLE_BLOCKS_PER_GROUP,
                "block_count": BLOCK_COUNT,
                "num_groups": n,
                "groups": groups,
            }, f)
        print(f"wrote {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
