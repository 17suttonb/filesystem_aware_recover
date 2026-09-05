#!/usr/bin/env python3
"""Build a GNU ddrescue domain-mapfile from a set of "wanted" absolute byte
ranges on the raw disk, using the parse_gdt.py JSON output plus a caller-
supplied list of always-recover ranges (partition table, LVM metadata, etc).

A domain-mapfile is just an ordinary ddrescue mapfile; ddrescue's
--domain-mapfile treats only the ranges marked '+' as "in domain" and
everything else as excluded, regardless of the real rescue mapfile's own
status for those bytes (that comparison happens automatically when ddrescue
actually runs).

Usage:
  build_domain.py --gdt gdt.json --lv-offset BYTES --disk-size BYTES \
      [--extra START:SIZE ...] -o domain.map
"""
import argparse
import json


def merge_ranges(ranges):
    """ranges: list of (start, size) -> sorted, merged (start, end) list."""
    intervals = sorted((s, s + n) for s, n in ranges if n > 0)
    merged = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def write_mapfile(path, merged, disk_size):
    """merged: sorted non-overlapping (start,end) 'wanted' intervals, in bytes."""
    with open(path, "w") as f:
        f.write("# Mapfile. Created by build_domain.py (filesystem-aware domain)\n")
        f.write("# current_pos  current_status\n")
        f.write("0x00000000     -\n")
        f.write("#      pos        size  status\n")
        pos = 0
        for s, e in merged:
            if s > pos:
                f.write(f"0x{pos:08X}  0x{s - pos:08X}  -\n")
            f.write(f"0x{s:08X}  0x{e - s:08X}  +\n")
            pos = e
        if pos < disk_size:
            f.write(f"0x{pos:08X}  0x{disk_size - pos:08X}  -\n")


def parse_extra(spec):
    start, size = spec.split(":")
    return int(start, 0), int(size, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gdt", required=True, help="parse_gdt.py --json output")
    ap.add_argument("--lv-offset", type=int, required=True,
                     help="absolute byte offset of the filesystem start on the raw disk")
    ap.add_argument("--disk-size", type=int, required=True)
    ap.add_argument("--extra", action="append", default=[],
                     help="extra always-wanted range as START:SIZE (bytes, any base)")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    with open(args.gdt) as f:
        gdt = json.load(f)

    block_size = gdt["block_size"]
    itable_blocks = gdt["inode_table_blocks_per_group"]

    ranges = [parse_extra(e) for e in args.extra]

    for g in gdt["groups"]:
        # block bitmap: 1 block, always needed (no group here has block_uninit set,
        # but even if some did, reading 4KB we don't strictly need is negligible)
        ranges.append((args.lv_offset + g["block_bitmap"] * block_size, block_size))
        if not g["inode_uninit"]:
            # this group actually has live inodes -> need its inode bitmap + full table
            ranges.append((args.lv_offset + g["inode_bitmap"] * block_size, block_size))
            ranges.append((args.lv_offset + g["inode_table"] * block_size,
                            itable_blocks * block_size))

    merged = merge_ranges(ranges)
    total = sum(e - s for s, e in merged)
    print(f"{len(ranges)} raw ranges -> {len(merged)} merged extents, "
          f"{total} bytes ({total / 1e6:.1f} MB) in domain")

    write_mapfile(args.output, merged, args.disk_size)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
