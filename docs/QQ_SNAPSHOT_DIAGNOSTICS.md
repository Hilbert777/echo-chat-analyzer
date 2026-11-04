# QQ snapshot page-count diagnostics

The 2026-10-26 failure reached `plaintext_validation` after capture succeeded.
The reported file size matched the selected committed page count, while
`page_count_valid=false`. This establishes a nonzero header page count with
equal file-change and version-valid-for counters that differs from the selected
commit count. It does not establish which source page or decryption behavior
caused that discrepancy.

These diagnostics are included in the **0.2.5 · 0.3 feature preview** update.
If QQ acquisition still fails after that update, deliver the resulting verified
fix in a separate small version; do not defer it until another large feature release.
This change does not claim to fix the affected machine's connection failure.

## Fields and interpretation

All fields go through the existing diagnostic allowlist. There are no database
fingerprints, raw header bytes, raw counters, keys or key hashes, account values,
chat content, paths or raw exception text in these fields.

| Fields | Meaning and next action |
|---|---|
| `selected_commit_frame`, `committed_pages`, `shm_pages`, `main_pages` | Connect the output's Pager page count to the fixed boundary B and its SHM witness; it can exceed the valid in-header logical size with preallocated capacity. Main physical size is context, not an alternative commit boundary. Stage-specific SHM `boundary` fields may advance after B. |
| `page_one_source=main/wal`, `page_one_in_committed_wal`, `page_one_frame` | Identify the last committed page one within B, or frame zero for main. A page one from an earlier transaction is legitimate; its valid logical size can be smaller than the Pager size. Uncommitted frames and frames after B are excluded. |
| `merged_page_one_matches_selected` | Compare only the owned merged image's encrypted page one with the page selected during capture. `false` directs investigation to selection/writing or subsequent staging changes. Missing evidence is not `true`. This observation does not replace any guard. |
| `header_pages`, `header_count_nonzero`, `header_counters_equal`, `header_pages_match_commit` | Report the size relationship, not a corruption verdict. `header_pages_match_commit` remains an exact equality observation. Following SQLite 3.54.1 `lockBtree()`, `page_count_valid` accepts a valid header size no greater than the Pager size, or falls back to the image size when the header count is zero or its counters differ. |
| `key_change_since_previous_decrypt`, `key_change_during_decrypt` | Fixed values `changed`, `unchanged`, or `unknown`. Compare only the key owner's process-local revision between acquisition attempts and across the API call. The first attempt and unavailable/malformed revision evidence are `unknown`. An unchanged key is not proof of a correct key. |

The existing NapCat key-capture owner increments its revision only when observed
key bytes change. It compares in memory before the existing assignment, keeps
no additional persistent copy or key hash, and ignores empty deliveries as
before. Best-effort revision observation must not prevent assignment. The
snapshot helper never retrieves the key. The API-call interval includes the
await continuation; a change there alone does not prove which key the native
function consumed.

Capture observations are retained on the final plaintext-validation failure so
one generation's selected source, counts and output header can be correlated.
They remain diagnostics, not additional claims of snapshot integrity.

## Boundaries and validation

Keep the existing WAL checksums/salts, fixed boundary, SHM/checkpoint witnesses,
main identity/content checks, staging-input witness, cleanup ownership, bounded
retry budget and atomic publication requirements. Do not repair an unexplained
mismatch by overwriting the header count, using main only, or skipping SQLite
validation. NapCat writes the plaintext SQLite signature and page size itself,
so those checks alone do not prove that the key or the remaining header is right.

Fictional tests cover main/WAL page-one provenance on a rejected header, key
revision changes and unavailable evidence, key arrival ordering, privacy-safe
logging, failure cleanup and existing capture/publication guards. Runtime pins
must match the templates and the full patched official source before packaging.
Synthetic tests do not replace Frozen/Setup verification or real QQ acceptance.

Remaining product gates are identity revalidation and manifest/atomic
publication, SQLite `quick_check`, session loading, and analysis/report creation.
The 2026-10-18 `quick_check` failure and the later pre-publication header failure
are separate observations; neither establishes the other's cause.
