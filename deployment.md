# Deployment: migrating off `sda` and writing the recovered image

Context: see `notes.md` for the full recovery story. `/root/boot-drive.img` is the
completed recovered server boot drive image (~97 GB real data + free space, ~500 GB
sparse file), verified clean via `e2fsck -fn`.

You have 3 identical 500GB SSDs:

- `sda` — currently this desktop's own Arch boot drive (the "spare" of the three).
- `sdd` — the original failing server drive (the ddrescue source). **Never write to
  this one — it's the only copy of the original data until the replacement is
  verified.**
- a third drive — not currently attached, will receive a clone of `sda`.

Goal: move this desktop off `sda` onto the third drive, then repurpose the
now-free `sda` as the actual replacement boot drive for the server (write
`boot-drive.img` onto it).

## Step 1 — Clone `sda` onto the third drive

1. Attach the third 500GB SSD to this desktop.
2. Identify it with `lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN` and confirm, before doing
   anything else:
   - size matches `sda` (~465.8 GiB / ≥ 500,107,862,016 bytes)
   - its serial is **not** `sda`'s or `sdd`'s (`234211077701449` is `sdd` — never
     target that)
   - it doesn't hold anything you still need (it will be completely overwritten)
3. Clone the whole disk (ESP + swap + root, no LVM on this desktop, so a raw
   whole-disk copy is simplest and needs no repartitioning or bootloader reinstall):

   ```text
   sync
   sudo dd if=/dev/sda of=/dev/sdX bs=64M status=progress conv=fsync
   sync
   ```

   Replace `/dev/sdX` with whatever `lsblk` showed for the third drive. Doing this
   while `sda` is live/mounted carries a small consistency risk only for files
   actively written _during_ the copy — low risk for an otherwise-idle desktop.

## Step 2 — Boot from the clone with the original disconnected

The clone has identical partition/filesystem UUIDs to `sda`. To avoid any ambiguity
about which disk the bootloader/kernel picks:

1. Shut down.
2. **Physically disconnect the original `sda`.**
3. Boot from the new drive only.
4. Confirm it's genuinely running from the new disk (`df -h`, `lsblk`, check the
   drive serial matches the new one) before proceeding.

## Step 3 — Repurpose the old `sda` as the recovery target

1. Reattach the original drive (desktop is now booted from the clone).
2. **Re-identify it with `lsblk`** — don't assume it keeps the same `/dev/sdX`
   letter after a reattach.
3. Write the recovered image to it:

   ```text
   sudo dd if=/root/boot-drive.img of=/dev/sdX bs=64M status=progress conv=fsync
   sync
   ```

   (`/root/boot-drive.img` will already be present on the new boot drive too, since
   it's a full clone of `sda`.) Because the image is a sparse file, the ~403 GB of
   untouched free space reads back as zeros, so this write takes roughly as long as
   a full ~466 GB sequential write at the drive's speed (likely 10-20 min on an SSD),
   even though only ~97 GB is real data.
4. This drive is now the server's replacement boot drive. Boot it in the actual
   server (or verify first with `e2fsck -fy` / a loop-device check if you want the
   journal-replay/orphan-inode cleanup done ahead of time — see `notes.md`'s
   verification section for what to expect there).
