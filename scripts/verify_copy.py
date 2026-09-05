#!/usr/bin/env python3
"""Verify that a ddrescue image copy is byte-identical to the source over the
extents a mapfile marks as rescued ('+'). Only the rescued extents actually
contain meaningful data (everything else is sparse/untried), so this is a fast,
sufficient integrity check for a "just copied the files" sanity pass.

Usage: verify_copy.py <mapfile> <source-img> <copy-img>
"""
import sys
import hashlib

CHUNK = 64 * 1024 * 1024


def parse_rescued_extents(mapfile_path):
    extents = []
    with open(mapfile_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pos_s, size_s, status = line.split()
            if status != "+":
                continue
            extents.append((int(pos_s, 16), int(size_s, 16)))
    return extents


def hash_extent(path, pos, size):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(pos)
        remaining = size
        while remaining > 0:
            n = min(CHUNK, remaining)
            data = f.read(n)
            if not data:
                raise IOError(f"short read at {pos} in {path}, wanted {n} more bytes")
            h.update(data)
            remaining -= len(data)
    return h.hexdigest()


def main():
    mapfile, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    extents = parse_rescued_extents(mapfile)
    total = sum(size for _, size in extents)
    print(f"{len(extents)} rescued extent(s), {total / 1e9:.2f} GB total to verify")

    mismatches = 0
    checked = 0
    for pos, size in extents:
        h_src = hash_extent(src, pos, size)
        h_dst = hash_extent(dst, pos, size)
        checked += size
        status = "OK" if h_src == h_dst else "MISMATCH"
        if status != "OK":
            mismatches += 1
        print(f"  0x{pos:x} +0x{size:x} ({size/1e9:.2f} GB): {status}")
        if status != "OK":
            print(f"    src={h_src}\n    dst={h_dst}")

    print(f"\nChecked {checked / 1e9:.2f} GB across {len(extents)} extents.")
    if mismatches:
        print(f"FAIL: {mismatches} extent(s) mismatched.")
        sys.exit(1)
    print("PASS: copy matches source over all rescued extents.")


if __name__ == "__main__":
    main()
