#!/usr/bin/env python3
"""Read the actual block-bitmap content (now that it's been rescued) and compute
the real used-data-block extents for the filesystem, as absolute byte ranges on
the raw disk.

ext4 block bitmap convention: bit=1 means the block is allocated/in-use,
bit=0 means free. This naturally also covers the filesystem's own metadata
blocks (the bitmap block, inode bitmap block, and inode table blocks of a
group are themselves marked used in that group's bitmap), so we don't need to
special-case them here -- trust the bitmap as ground truth, as decided in the
plan.

Usage: compute_used_blocks.py --gdt gdt.json --image work.img --lv-offset BYTES
           --extra START:SIZE [--extra ...] -o used-blocks.map --disk-size BYTES
"""
import argparse
import json


def merge_ranges(ranges):
    intervals = sorted((s, s + n) for s, n in ranges if n > 0)
    merged = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def write_mapfile(path, merged, disk_size):
    with open(path, "w") as f:
        f.write("# Mapfile. Created by compute_used_blocks.py (filesystem-aware domain)\n")
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


def blocks_used_in_bitmap(bitmap_bytes, blocks_in_group):
    """Yield (start_block_in_group, count) runs of used (bit=1) blocks.

    Uses a big-integer bit trick (x & -x to find/skip a run of zeros, then
    x ^ (x+1) to measure the following run of ones) so the cost is
    O(number of runs) rather than O(number of bits) -- no numpy available,
    and a plain bit-by-bit loop over ~122M bits would be too slow.
    """
    x = int.from_bytes(bitmap_bytes, "little")
    pos = 0
    while x and pos < blocks_in_group:
        low = x & -x
        tz = low.bit_length() - 1  # skip this many trailing zero bits (free blocks)
        pos += tz
        x >>= tz
        if pos >= blocks_in_group:
            break
        run_len = (x ^ (x + 1)).bit_length() - 1  # length of the following run of 1 bits
        run_len = min(run_len, blocks_in_group - pos)
        yield pos, run_len
        pos += run_len
        x >>= run_len


def parse_extra(spec):
    start, size = spec.split(":")
    return int(start, 0), int(size, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gdt", required=True)
    ap.add_argument("--image", required=True, help="the rescued image (work.img)")
    ap.add_argument("--lv-offset", type=int, required=True)
    ap.add_argument("--disk-size", type=int, required=True)
    ap.add_argument("--extra", action="append", default=[])
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    with open(args.gdt) as f:
        gdt = json.load(f)

    block_size = gdt["block_size"]
    blocks_per_group = gdt["blocks_per_group"]
    n_groups = gdt["num_groups"]
    block_count = gdt["block_count"]

    ranges = [parse_extra(e) for e in args.extra]

    used_blocks_total = 0
    short_reads = 0
    with open(args.image, "rb") as f:
        for gi, g in enumerate(gdt["groups"]):
            blocks_in_group = min(blocks_per_group, block_count - gi * blocks_per_group)
            bitmap_bytes_needed = (blocks_in_group + 7) // 8
            f.seek(args.lv_offset + g["block_bitmap"] * block_size)
            bitmap = f.read(bitmap_bytes_needed)
            if len(bitmap) < bitmap_bytes_needed:
                short_reads += 1
                # pad with zeros (treat unread tail as free) rather than crash;
                # short_reads counter flags this so we notice if it's non-zero
                bitmap = bitmap + b"\x00" * (bitmap_bytes_needed - len(bitmap))
            for start_blk, count in blocks_used_in_bitmap(bitmap, blocks_in_group):
                abs_block = gi * blocks_per_group + start_blk
                ranges.append((args.lv_offset + abs_block * block_size, count * block_size))
                used_blocks_total += count

    print(f"groups: {n_groups}, used data blocks (raw, pre-merge): {used_blocks_total} "
          f"({used_blocks_total * block_size / 1e9:.2f} GB)")
    if short_reads:
        print(f"WARNING: {short_reads} group(s) had a short bitmap read -- "
              f"metadata-fetch pass may be incomplete for those groups")

    merged = merge_ranges(ranges)
    total = sum(e - s for s, e in merged)
    print(f"-> {len(merged)} merged extents, {total} bytes ({total / 1e9:.2f} GB) in final domain")

    write_mapfile(args.output, merged, args.disk_size)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
