# Ceph boot-drive recovery — context notes

## Situation

- `/dev/sdd` is the boot drive of an Ubuntu server that's also part of a Ceph cluster
  (hosts mgr/osd daemon state — not necessarily raw OSD bluestore storage itself).
  Otherwise a standard Ubuntu + LVM install.
- Mounting the filesystem or activating its LVM causes the ATA driver to retry queued
  commands until it disables the device. The only access method that has worked so
  far is `ddrescue -d` (direct raw reads) against the whole block device.
- SMART: 0 errors of any kind reported, 30% write-endurance used → the NAND itself is
  almost certainly healthy. This points to a link/protocol-layer issue (cable,
  backplane, controller, or firmware quirk under certain command patterns) rather
  than failing flash.
- Estimated actual filesystem usage: ~70–80 GB out of a ~466 GB (500,107,862,016 byte)
  drive. Most LBAs were never written in the drive's lifetime.

## Recovery host

- Arch Linux desktop (`arch-test`), x86_64, regular Intel hardware. This is where
  ddrescue is being run directly against `/dev/sdd` and where all image analysis
  happens.
- `/root` (where `boot-drive.img`/`boot-drive.map` live) is on `/dev/sda3`, ext4,
  453G total / 329G free at the time of writing. No btrfs/xfs available, so `cp
  --reflink=auto` falls back to an ordinary (but still sparse-aware) copy.
- Tooling confirmed present: ddrescue, ddrescuelog, lvm2 (pvs/vgs/lvs/pvck), dumpe2fs,
  debugfs, e2fsck, fdisk/sfdisk/parted, blkid, cryptsetup (not needed — no LUKS),
  python3. `kpartx` and `xxd` are **not** installed (not required — `losetup -P`/
  `partx` and `python3`/`od` cover the same ground).
- No LUKS anywhere in the stack (confirmed). Filesystem(s) expected to be ext4
  (Ubuntu default) — to be verified per-volume during recovery, not just assumed.
  Swap LV, if present, is to be **fully recovered**, not skipped — the only space we
  skip is space we can positively prove is free/unwritten.

## ddrescue status as of the pause (2026-09-05 21:16 UTC-ish, session-local time)

Files: `/root/boot-drive.img` (466 GiB apparent, ~29 GiB actual on-disk / sparse),
`/root/boot-drive.map` (+ a `.map.bak` from a few minutes earlier — already being
careful).

```
$ ddrescuelog -t boot-drive.map
   current pos:   12129 MB,  current status: trimming
mapfile extent:  500107 MB,  in      11 area(s)
     non-tried:        0 B,  in       0 area(s)  (  0%)
       rescued:   30326 MB,  in       6 area(s)  (  6.06%)
   non-trimmed:  469781 MB,  in       5 area(s)  ( 93.94%)
   non-scraped:        0 B,  in       0 area(s)  (  0%)
    bad-sector:        0 B,  in       0 area(s)  (  0%)
```

**Important finding**: `non-tried` is already 0 — ddrescue has attempted every byte of
the disk at least once. What's eating all the time is that ~470 GB is stuck in
`non-trimmed` status with **zero** actual bad sectors or read errors recorded. In
ddrescue's model, a region only gets marked non-trimmed when a large-block read didn't
cleanly complete as expected (error, or looks-like-an-error timing), and the region
then needs to be recursively bisected ("trimmed") to isolate exactly what's wrong —
even though, per the final tally, nothing ever turns out to actually be bad. This
means the observed 2.5–3.5 MB/s throughput isn't just "the drive reads slowly" — most
of the wall-clock time is going into repeated bisection/retry overhead across almost
the entire disk, triggered by transient slowness/latency that looks like an error to
ddrescue's heuristics but always eventually returns good data. Restricting the domain
to only the real used blocks won't just cut bytes read from ~470 GB to ~80 GB — it
should cut a large chunk of this trim/bisect overhead too, since most of the
non-trimmed span falls in space we don't need to touch at all.

## Goal / approach

Use the ~30 GB already rescued (plus small additional targeted reads) to read LVM +
ext4 metadata *offline*, compute which LBA ranges are real filesystem metadata / in-use
data blocks (+ swap, fully), express that as a ddrescue **domain-mapfile**, and re-run
ddrescue restricted to that domain instead of the whole disk. All analysis happens
against the image file / loop devices backed by it — never against `/dev/sdd` outside
of ddrescue's own direct reads, to avoid re-triggering the ATA fault.

Full step-by-step plan lives at
`/home/ben/.claude/plans/i-m-currently-trying-to-fuzzy-abelson.md`.

## Safety rule

All exploratory work (loop devices, LVM/ext4 metadata parsing, bitmap analysis, domain
building) happens on `work.img`/`work.map` copies of the real files. The real
`boot-drive.img`/`boot-drive.map` are only touched by the final validated,
domain-restricted ddrescue run — and ddrescue itself only ever adds data to them, never
discards what's already rescued.

## LVM / filesystem layout discovered so far

- GPT: 1 EFI System partition (512 MiB, FAT32, `/dev/loop0p1`) + 1 big Linux
  filesystem partition (465.3 GiB, `/dev/loop0p2`) = the single LVM PV. No separate
  `/boot` partition outside the VG.
- VG `vgroot`, PV `pv0` (`dev_size=975708160` sectors, `pe_start=2048`,
  `extent_size=8192` sectors = 4 MiB PEs, `pe_count=119104`).
- **Only one LV**: `lvroot`, `start_extent=0`, `extent_count=119104` — i.e. it spans
  the *entire* PV. No separate swap LV, no separate `/boot` LV, no thin/mirror/cache
  (`segment_type = "striped"`, `stripe_count = 1` → plain linear). This simplifies
  things a lot: everything (root fs, and presumably a swapfile rather than a swap LV)
  lives inside this one ext4 filesystem, so "which blocks are used" is a single
  per-file-system computation, not a union across several LVs.
- `lvroot` absolute byte range on the raw disk/image:
  start = `538968064` (`0x20200000`), size = `499558383616` bytes (≈499.56 GB), end =
  `500097351680` (`0x7470200000`). (`part_start_sector(1050624) + pe_start(2048)` sectors
  in, LV starts at extent 0 so no further offset.)
- ext4 on `lvroot`: label `root`, mounted at `/`, block size 4096, `121962496` blocks,
  `30490624` inodes, `64bit` + `flex_bg` (flex group size 16) + `metadata_csum` +
  `has_journal` + `needs_recovery` (unclean shutdown pending journal replay — expected,
  the drive died before a clean unmount) + `extent`-mapped files. Free blocks
  `103111310` → **used ≈ (121962496 − 103111310) × 4096 ≈ 77.2 GB**, matching the
  user's ~70–80 GB estimate closely.
- Group descriptor table is the extended 64-byte form (`64bit` feature): 3723 groups
  (`ceil(121962496 / 32768)`), GDT occupies blocks 1–59 (right after the superblock),
  comfortably inside the already-rescued region.
- **`dumpe2fs` (full) and `debugfs` both refuse to work against this partially-rescued
  image**: `dumpe2fs` aborts once it tries to read the (not-yet-recovered) journal
  inode's extent tree ("Corrupt extent header while reading journal super block") before
  it ever prints the per-group listing; `debugfs` eagerly checksum-validates block
  bitmaps at open time and refuses to open at all ("Block bitmap checksum does not
  match... Filesystem not open") since most bitmaps aren't recovered yet. Both are too
  eager/strict for our use case, so `scripts/parse_gdt.py` reads the superblock-derived
  constants (from the one `dumpe2fs -h` call that *does* work) and parses the GDT
  directly as binary data instead of relying on either tool for the group listing.
- **udisks2 automount hazard**: creating a loop device for `lvroot` (even read-only)
  triggered the desktop's automount (udisks2/gvfs) to attempt an actual kernel mount,
  which tried to replay the dirty journal, failed (loop device is read-only, and the
  journal data itself isn't recovered yet so reads as zeroed garbage), and produced a
  user-visible "mount failed" desktop notification. No data was written (loop device
  read-only ⇒ no write path existed), confirmed harmless, but noisy/risky enough that
  we stopped udisks2 (`sudo systemctl stop udisks2`) for the remainder of this session.
  **Remember to `sudo systemctl start udisks2` again once the recovery work is done.**

## ddrescue phase-model gotcha (important for the final pass too)

Attempting to fetch our scattered metadata domain (bitmaps/inode tables spread across
the disk) directly via `--domain-mapfile` did **not** work at first, and it's worth
understanding why since it applies to the final big pass too:

- `ddrescue`'s "trimming" phase (which handles `*` / non-trimmed status) only shrinks
  existing non-trimmed regions **inward from their real edges** — it can't jump into
  an arbitrary domain-restricted interior position of one large pre-existing
  non-trimmed block. Since almost the whole disk was one giant non-trimmed region from
  the original full-disk run, and our wanted bytes were scattered deep inside it,
  trimming made ~zero progress regardless of `-r` (retries only apply to confirmed
  `bad-sector` status, not non-trimmed, so `-r1` alone didn't help either).
- **Fix**: reclassify the exact target byte ranges from `*` (non-trimmed) to `?`
  (non-tried) using `ddrescuelog -a '*,?' -m <domain> boot-drive.map`, restricted to
  just the domain we care about (verified first on a copy — `ddrescuelog` never
  modifies its input, always writes the transformed map to stdout). Once reclassified
  as non-tried, ddrescue's normal "copying" phase picked them up directly and
  correctly respected the domain restriction.
- Backed up the real map first (`boot-drive.map.pre-reclassify.<timestamp>`) before
  applying the same transform to it.
- Result: the domain-restricted re-run finished the entire 758MB metadata domain in
  **3 seconds**, at rates up to 100 MB/s (the configured cap), with **zero read errors,
  zero bad areas**. This strongly confirms the drive's media/link is fine for actual
  data transfer — the extreme slowness in the original full-disk run was ddrescue's
  trim/bisect overhead on a drive that occasionally looks erroring to its heuristics,
  not a true throughput problem. **The same *→? reclassification trick will likely be
  needed again for the final used-data-blocks domain**, for the same reason.

## Final used-data domain

`scripts/compute_used_blocks.py` reads the now-rescued block bitmaps directly (fast
big-integer run-finding, no numpy needed: ~0.15s for all 3722 groups) and computes the
real used-data-block extents:

- Raw used data blocks: 18,838,928 blocks × 4096 B = **77.16 GB** — matches the
  free-block-derived estimate (≈77.2 GB) almost exactly.
- Merged with the always-recover extras (disk head, ext4 superblock/GDT, backup GPT):
  **77.70 GB across 18,080 extents** = the final domain (`used-blocks.map`).
- Of that, 11.16 GB was already sitting in previously-rescued chunks; **~66.5 GB
  needed genuinely new reading** (`ddrescuelog -a '*,?' -m used-blocks.map` showed
  66,545 MB flipping from non-trimmed to non-tried, in 15,630 areas).
- Real map backed up again (`boot-drive.map.pre-final-reclassify.<timestamp>`) before
  applying the same `*→?` reclassification used for the metadata pass.
- Final pass: `ddrescue -d -r1 --max-read-rate=100M --domain-mapfile=used-blocks.map
  /dev/sdd boot-drive.img boot-drive.map`, sustaining rates up to 100 MB/s
  (average ~65-85 MB/s), **zero read errors** — consistent with the metadata pass and
  further confirming the drive's media/link is fine for actual transfers; the original
  full-disk slowness was ddrescue's trim/bisect overhead, not real throughput/error
  issues. ETA reported ~15-17 minutes for the full ~66.5 GB.

## Final pass result — core recovery goal reached

The final domain-restricted pass completed: **100% of the 77.70 GB used-data domain
rescued in 17m 17s**, average rate 64.1 MB/s (peaks at the 100 MB/s cap), **zero read
errors** throughout. Real map now shows:

```text
rescued:   97088 MB,  in  15_578 area(s)  ( 19.41%)
non-trimmed:  403019 MB,  in  15_577 area(s)  ( 80.59%)   <- deliberately-excluded free space, untouched
```

So: all filesystem metadata + all real used data (≈77.7 GB net of the ~11 GB that
happened to already be rescued) is now recovered, while the ~403 GB of genuinely free
space was correctly left untouched. Went from an estimated 2+ days for a full-disk
rescue to under 20 minutes of actual new reading, with zero errors observed at any
point once the trim/bisect overhead was worked around.

Remaining work: verify filesystem consistency (`e2fsck -fn` against a loop device, on
a copy), then write the final image to the replacement drive. `udisks2` is still
stopped — restart it (`sudo systemctl start udisks2`) once loop-device work is done.

## Verification: e2fsck -fn (read-only) result — clean

Ran `e2fsck -fn` (force check, no fixes) against `lvroot` isolated as its own loop
device from the refreshed `work.img` (verified byte-identical to `boot-drive.img`
over all 97.09 GB of rescued extents first). Findings, and why none of them indicate
data loss from the domain-restricted recovery:

- `Warning: skipping journal recovery because doing a read-only filesystem check` +
  a batch of `Inodes that were part of a corrupted orphan linked list... IGNORED` +
  one `Deleted inode ... has zero dtime` — all expected consequences of the drive's
  pending `needs_recovery` state (it died mid-session, journal never replayed). A
  normal (writable) mount or `e2fsck -fy` replays the journal and processes the
  orphan list automatically; this isn't something our recovery caused or could have
  avoided, and isn't present in `-n` mode by design.
- A batch of `Inode ... extent tree could be shorter/narrower. Optimize?` — cosmetic
  suggestions only, not corruption.
- **Pass 5**: `Block bitmap differences` (~12,258 blocks) and `Free blocks count
  wrong (103111310, counted=103123568)`, similarly for the inode bitmap. This is the
  check that actually matters for our approach, and it points the same direction as
  everything else: these are blocks the on-disk bitmap still marks *used* that
  e2fsck's live inode walk no longer finds referenced — i.e. blocks belonging to the
  orphaned (being-deleted/truncated) inodes above, not yet released because the
  journal hasn't replayed. Since we trusted the on-disk bitmap as ground truth (the
  documented design decision), **we over-included these blocks rather than missing
  anything** — the safe direction of error. No sign anywhere in the output of an
  actually-missing/corrupt block (no I/O errors, no "illegal block", no unresolvable
  extent errors).
- Exit code 4 ("errors left uncorrected") is expected for `-n` — a real `e2fsck -fy`
  or just booting normally on the replacement drive will clean these up via the
  ordinary journal replay + orphan processing that never got to run.

**Conclusion: no evidence of data loss from the filesystem-aware domain restriction.**
All discrepancies found trace to the drive's pre-existing unclean shutdown, not to
anything skipped by the recovery approach.

## Log

- 2026-09-05: ddrescue paused by user (Ctrl-C) after ~20 min, ~30 GB rescued, due to
  infeasible ETA (~2 days) and concern about compounding wear/risk on an already
  flaky drive. Switching to filesystem-aware domain-restricted approach per the plan
  above. `work.img`/`work.map` copies created before any analysis began.
- 2026-09-05: Verified `work.img` against `boot-drive.img` by hashing every extent the
  mapfile marks `+` (rescued) — all 6 extents (30.33 GB total) match exactly
  (`scripts/verify_copy.py`). `du` reported a smaller actual-block count for the copy
  (17G vs 29G) than the source purely due to sparse-file layout/allocation
  differences from the copy — not data loss, confirmed by the hash check.
  `/root` and its files require `sudo` to access (directory is `drwxr-x---
  root:root`); analysis commands below run under sudo accordingly.
