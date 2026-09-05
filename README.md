# filesystem-aware-recover

Tools and notes for rescuing a failing ext4-on-LVM boot drive by teaching
[GNU ddrescue](https://www.gnu.org/software/ddrescue/) which parts of the disk are
actually worth reading, instead of imaging the whole thing byte-for-byte.

## The problem

A boot drive was dying in a way that made `ddrescue -d` (direct raw access) the only
way to read it at all — mounting the filesystem or activating its LVM would cause the
kernel's ATA driver to retry commands until it disabled the device. A plain
`ddrescue -d` full-disk pass was technically working, but at ~2.5-3.5 MB/s, imaging
the whole ~500 GB disk would have taken **2+ days**, all while continuing to stress an
already-misbehaving drive — even though SMART reported zero errors and the actual
filesystem usage was only ~70-80 GB out of ~500 GB.

## The approach

1. Let ddrescue grab an initial chunk of the disk sequentially (enough to reach the
   partition table and LVM metadata).
2. Parse the LVM layout and ext4 superblock/group-descriptor-table **offline**, from
   that partial image — never touching the real device except through ddrescue's own
   reads.
3. Fetch just the ext4 block/inode bitmaps and live inode tables (tiny, scattered)
   with a small domain-restricted ddrescue pass.
4. Read those bitmaps to compute exactly which data blocks are actually in use.
5. Build a ddrescue **domain-mapfile** covering only that real, in-use data (plus all
   filesystem/LVM metadata), and run the final rescue pass restricted to it.

Result on the drive this was built for: the real "used data" domain came out to
**77.7 GB** (vs. ~500 GB total), of which the final restricted pass fetched the
remaining ~66.5 GB **in 17 minutes at up to 100 MB/s with zero read errors** —
confirming the original slowness was ddrescue's internal trim/retry bookkeeping on a
drive that looks flaky to its heuristics, not an actual media/throughput problem.
Full narrative, including a couple of ddrescue phase-model gotchas that weren't
obvious going in, is in [`notes.md`](notes.md).

## Repo layout

- [`notes.md`](notes.md) — full running log: drive symptoms, the LVM/filesystem
  layout as discovered, ddrescue quirks hit along the way and how they were worked
  around, and the final verification result.
- [`deployment.md`](deployment.md) — steps for getting the recovered image onto a
  physical replacement drive.
- [`scripts/`](scripts) — the actual analysis tools, meant to be run in order:
  - `parse_gdt.py` — parses an ext4 group descriptor table directly from a
    (partially-recovered) image/device, bypassing `dumpe2fs`/`debugfs`, both of which
    refuse to run against an incomplete filesystem.
  - `build_domain.py` — turns GDT info + a few "always recover" byte ranges (GPT,
    LVM metadata, superblock) into a ddrescue domain-mapfile for a small metadata-only
    fetch pass.
  - `compute_used_blocks.py` — once bitmaps are recovered, reads them directly (fast
    big-integer run-finding, no numpy needed) and builds the final domain-mapfile
    covering all real in-use data.
  - `verify_copy.py` — sanity-checks that a copy of the rescue image is byte-identical
    to the source over every already-rescued extent, without needing to read the
    entire (mostly-empty) sparse file.

## Requirements

`ddrescue`/`ddrescuelog`, `lvm2` (for `pvck`), `losetup`, `python3`. Everything else
(`dumpe2fs`, `e2fsck`, `fdisk`, `blkid`, etc.) is standard on most Linux distros.

## Caveats

This was built against one specific drive's layout (single ext4 LV filling the whole
LVM PV, `64bit`+`flex_bg`+`metadata_csum` features, no LUKS). The general approach —
domain-restrict ddrescue to filesystem metadata + actually-used blocks — generalizes,
but `parse_gdt.py`'s superblock-derived constants and `compute_used_blocks.py`'s
group-scanning logic would need adjusting for a different block size, a
`meta_bg`-only ext4 without `64bit`, XFS/other filesystems, or a multi-LV setup.
