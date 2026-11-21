# TwinCheck roadmap

How to vote: give a 👍 to the issues you care about.
[Most wanted items](https://github.com/AxReds/twincheck/issues?q=is:open+label:roadmap+sort:reactions-+1-desc)
New ideas: open a discussion in the **Ideas** category.

Safety rules that apply to every item: never overwrite, never delete (move only), explicit confirmation before changes, every change can be undone.

## 1. Exact duplicates – DONE (v1.0.0)
Same size + SHA-256 + byte-by-byte comparison. Duplicates are moved to `Duplicates`, never deleted. "Identical" means everything inside the file is identical (pixels, EXIF, XMP, embedded thumbnails); name, folder, file system dates and sidecars may differ.

## 1b. Exact duplicates with different sidecars – TODO
Issue: [#1](https://github.com/AxReds/twincheck/issues/1)

Sidecars: `.xmp`, `.aae`, Google Takeout `.json`.
- Prefer as keeper the copy with sidecars / more information.
- If a duplicate has a sidecar that differs from the keeper's, do not auto-move it: send it to review.
- Later: optionally merge sidecar information into the keeper, after confirmation.

## 1c. File system dates follow the capture date – TODO
Issue: [#2](https://github.com/AxReds/twincheck/issues/2)

- After keeping, organizing, moving to `Duplicates` or writing metadata, set the file modification (and access) time to the capture date stored inside the file: `DateTimeOriginal` for images, QuickTime creation date (UTC) for videos.
- Time zone: use `OffsetTimeOriginal` when present, otherwise an instance time zone setting (default `Europe/Rome`).
- Files without an internal date are left unchanged.
- Sidecars and the video part of Live Photos get the same time.
- Record the old modification time in the manifest so undo restores it; update the indexed mtime so the next scan does not rehash the file.
- Enabled by default, with an admin toggle.
- Creation (birth) time and ctime cannot be set from Linux: document it.
- If the file system refuses the change (some SMB shares), continue and list the file as "date not updated".
- Note for users: backup tools that compare size and date (rsync, Hyper Backup) will copy changed files once.

## 2. Same content, different metadata – TODO
Issue: [#3](https://github.com/AxReds/twincheck/issues/3)

- Use ExifTool `ImageDataHash` (hash of image/video data, metadata excluded); verify which formats are supported.
- Review-only at first; decide the keeper (e.g. richest metadata), optionally merge metadata after confirmation.

## 3. Similar images and videos – TODO
Issue: [#4](https://github.com/AxReds/twincheck/issues/4)

- Perceptual hash computed with ffmpeg (already in the image, no new compiled dependencies); sample frames for videos.
- Report for review only, never auto-move (false positives such as burst shots are possible).

## 4. Network shares (NAS) – TODO
Target: Synology/QNAP NAS, several shares, other clients (phone backup, Synology Photos) may write at the same time. The container never mounts shares (that would need CAP_SYS_ADMIN): the host mounts them.

### 4a. Documentation
Issue: [#5](https://github.com/AxReds/twincheck/issues/5)

- Host mounts: NFS v4.1 (preferred: hard links, atomic rename, POSIX permissions) and SMB as an alternative; credentials file with mode 600; `_netdev,nofail`.
- UID mapping: PUID/PGID must match the NAS user (Synology users usually start at 1026) or the NFS squash mapping.
- One bind mount `/mnt/nas` → `/data/nas` with `rslave` propagation, so shares mounted after the container starts are visible.
- Organize each share within itself (same-share moves are renames; cross-share moves are copy + verify + delete and slow).
- `/state` must always stay on a local disk.
- Warning: catalog apps (Synology Photos) re-index after moves and may lose album/face data tied to old paths – test on a copy first.
- First scan reads every byte over the network (about 3 hours per TB on 1 Gbit).

### 4b. Safeguards (in this order)
Issue: [#6](https://github.com/AxReds/twincheck/issues/6)

1. Source marker file `.twincheck-source`: if missing (share not mounted), the source is "not available" and no job runs on it; files are never marked missing because of it.
2. Default exclusions for NAS system folders: `@eaDir`, `#recycle`, `#snapshot`, `.@__thumb`, `@Recycle`, `.SynologyWorkingDirectory` (editable by the admin).
3. Network errors (`ENOTCONN`, `ESTALE`, `EHOSTDOWN`, `ETIMEDOUT`, `EIO`) pause the job ("source offline") instead of failing items; resume when the share is back.
4. No-overwrite move without hard links: create an exclusive placeholder at the destination, then rename over it; re-check that the source has not changed since the plan.
5. Show the file system type of each source (from `/proc/self/mountinfo`) and related warnings in the UI.
Until 4b is done, use network shares for scan and preview only.

### 4c. CI tests
Issue: [#7](https://github.com/AxReds/twincheck/issues/7)

- Samba and NFS servers in containers, shares mounted on the runner.
- Fault tests: share unmounted during a job, concurrent writer at the destination.

## Cross-cutting
- Run duplicate detection before metadata writes (writing EXIF changes the bytes): enforce the order or warn in the UI. Issue: [#8](https://github.com/AxReds/twincheck/issues/8)

## Suggested order
4a → 1c → 1b → 4b → 2 → 4c → 3

## 5. Settings and preferences – TODO
Issue: [#24](https://github.com/AxReds/twincheck/issues/24)

- Admin Settings: libraries, source/destination folders and assignments (admin included
  by default), default folder for new users, existing database/export controls and
  allowed user policies.
- User Preferences in the user menu: own folders, organisation patterns (`YYYY/MM/DD`,
  `YYYY/MM`, ...), duplicate handling, language, theme and technical details.
- Users can choose only within admin-defined limits. Timestamp settings depend on
  [#2](https://github.com/AxReds/twincheck/issues/2).
- Tracking only: no new settings model is included in the v1.0.1 bug fixes.

## 6. Duplicates folder management – DESIGN REQUIRED
Issue: [#25](https://github.com/AxReds/twincheck/issues/25)

- List and preview duplicates and restore them to their original paths.
- Proposed permanent deletion would require admin access, double confirmation,
  a kept original that still exists and matches by SHA-256, and an audit record.
- **Blocked on a design discussion:** permanent deletion conflicts with the current
  "never delete" rule. This roadmap entry does not change that rule or authorize deletion.
- Coordinate allowed policies with [#24](https://github.com/AxReds/twincheck/issues/24).

## 7. User folders and administrator root analysis – DESIGN REQUIRED
Issue: [#32](https://github.com/AxReds/twincheck/issues/32)

- On user creation choose an existing folder or create `/data/users/<username>/`
  in the UI and assign it as the source automatically.
- A mistakenly created folder may be removed only when empty (including hidden
  files and sidecars), unassigned and with no running job.
- Proposed administrators-group root analysis would exclude Duplicates, Organize
  destinations, `.twincheck-trash` and NAS system folders such as `@eaDir` and
  `#recycle`. Root source assignment remains intentionally prohibited today.
- Phase 1 is analysis only: cross-user reports, no moves. Phase 2 requires no user
  jobs, locks on user folders and review for every cross-user duplicate.
- Tracking only; coordinate permissions and settings with
  [#24](https://github.com/AxReds/twincheck/issues/24),
  [#25](https://github.com/AxReds/twincheck/issues/25) and NAS safeguards
  [#6](https://github.com/AxReds/twincheck/issues/6).

## 8. Administrator file-system management and trash – DESIGN REQUIRED
Issue: [#33](https://github.com/AxReds/twincheck/issues/33)

- Administrators-group UI: create/rename folders, remove only empty folders,
  background size jobs with progress/cached results, and read-only UID/GID,
  permissions, file count, modification date and free-space properties.
- Rename requires no running job, atomic updates of all stored source/index/undo
  paths, and rollback on failure.
- "Empty folder" means move contents into `/data/.twincheck-trash/` preserving
  original paths. Exclude trash from scans and hide it from non-admins.
- Restore to the original path; an occupied target stops the action without
  overwriting. No automatic trash expiry; show its size.
- Proposed "Empty trash" would be the only permanent deletion, administrators only,
  with selected/all-item preview (count, size, trashed date and actor), double
  confirmation and audit. **Blocked on design: the current never-delete rule
  remains unchanged; duplicates are moved, never deleted.**
- Every action needs preview, confirmation, audit and no jobs on that folder;
  recheck network shares immediately before acting. Coordinate with
  [#25](https://github.com/AxReds/twincheck/issues/25),
  [#24](https://github.com/AxReds/twincheck/issues/24) and
  [#32](https://github.com/AxReds/twincheck/issues/32). Tracking only.
