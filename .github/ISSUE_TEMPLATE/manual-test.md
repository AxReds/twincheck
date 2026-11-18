---
name: Manual acceptance test
about: Track manual release acceptance testing on disposable media copies
title: "Manual test: vX.Y.Z"
labels: ["testing"]
---

[Full checklist: actions, expected results and safety rules](https://github.com/AxReds/twincheck/blob/main/docs/manual-test-checklist.md).

## 0 Test data preparation

- [ ] 0.1 Isolate the test deployment.
- [ ] 0.2 Keep an untouched recovery baseline.
- [ ] 0.3 Prepare exact duplicates and non-duplicates.
- [ ] 0.4 Prepare metadata and review fixtures.
- [ ] 0.5 Prepare videos, companions and destination collisions.
- [ ] 0.6 Record the starting inventory.

## 1 Users and sign-in

- [ ] 1.1 Complete first setup on the empty test instance.
- [ ] 1.2 Reject incorrect sign-in credentials.
- [ ] 1.3 Create the test accounts and sources.
- [ ] 1.4 Require the first password change.
- [ ] 1.5 Enforce role and file isolation.
- [ ] 1.6 Reject overlapping source assignments.
- [ ] 1.7 Apply account and group activation.
- [ ] 1.8 End the session explicitly.

## 2 Scan

- [ ] 2.1 Check the deployment information.
- [ ] 2.2 Scan without changing media.
- [ ] 2.3 Repeat an unchanged scan.
- [ ] 2.4 Reconcile changed test files.
- [ ] 2.5 Honor scan exclusions.

## 3 Duplicates

- [ ] 3.1 Find only the known exact duplicate group.
- [ ] 3.2 Keep different content out of exact duplicate groups.
- [ ] 3.3 Choose the keeper and export the list.
- [ ] 3.4 Cancel a duplicate-move preview.
- [ ] 3.5 Reject a stale duplicate plan.
- [ ] 3.6 Execute a fresh duplicate-move plan.

## 4 Metadata and review

- [ ] 4.1 Analyze without writing metadata.
- [ ] 4.2 Complete only missing fields from reliable sources.
- [ ] 4.3 Send uncertain dates to review.
- [ ] 4.4 Confirm metadata writes explicitly.
- [ ] 4.5 Display photo and video previews.
- [ ] 4.6 Save a decision without writing the file.
- [ ] 4.7 Keep review decisions distinct.
- [ ] 4.8 Limit bulk review decisions to the selected scope.
- [ ] 4.9 Preview and write saved decisions.

## 5 Organize

- [ ] 5.1 Preview the organization without moving files.
- [ ] 5.2 Resolve same-name collisions without overwriting.
- [ ] 5.3 Include linked files in the plan.
- [ ] 5.4 Protect a destination occupied after planning.
- [ ] 5.5 Execute and inspect organization.

## 6 Undo and verification

- [ ] 6.1 Download the manifest and verify the current state.
- [ ] 6.2 Reverse moves only after confirmation.
- [ ] 6.3 Refuse an occupied original location during undo.
- [ ] 6.4 Restore metadata through its own preview.
- [ ] 6.5 Detect a deliberate verification mismatch.

## 7 Interruptions (container restart during jobs)

- [ ] 7.1 Recover a scan across a container restart.
- [ ] 7.2 Recover duplicate moves across a container restart.
- [ ] 7.3 Recover organization across a container restart.
- [ ] 7.4 Recover metadata writes across a container restart.
- [ ] 7.5 Verify consistency after interruptions.

## 8 Administration

- [ ] 8.1 Reset the admin password from the console.
- [ ] 8.2 Exercise the generated-password reset.
- [ ] 8.3 Inspect the administrative audit trail.
- [ ] 8.4 Persist a user's language independently.
- [ ] 8.5 Set the default language as admin.
- [ ] 8.6 Test the database connection without switching.
- [ ] 8.7 Preview an encrypted export.
- [ ] 8.8 Execute and download the export.
- [ ] 8.9 Restrict database transfers to admin.

## 9 Backup and restore

- [ ] 9.1 Take a consistent full test backup.
- [ ] 9.2 Restore the complete backup into isolated empty storage.
- [ ] 9.3 Validate the restored application and media.
- [ ] 9.4 Verify the export by importing into an empty database.
- [ ] 9.5 Reject invalid import inputs safely.
- [ ] 9.6 Switch to the verified import and check recovery.

## Problems found

<!-- Record the tested version/commit, image tag, architecture, browser, database
and test date here. Link bug issues with the affected step IDs; explain any
failed or untested steps. Do not attach media, databases, archives or secrets. -->
