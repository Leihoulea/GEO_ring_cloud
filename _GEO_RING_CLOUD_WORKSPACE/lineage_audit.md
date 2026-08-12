# GEO-ring Cloud Lineage Audit

Audit date: `2026-07-28`.

## Current finding

The following recent run records are scientifically useful, but their Git
lineage is incomplete. This table does not infer missing source content.

| stage | manifest time | recorded commit | script present in recorded commit | factual limitation |
| --- | --- | --- | --- | --- |
| `stage_09i` | `2026-07-24T07:24:02Z` | none | not applicable | The manifest identifies an inline environment repair and smoke rerun, not a saved generating script. Exact executable source is unavailable. |
| `stage_09j` | `2026-07-24T09:39:00Z` | none | not verifiable | The current script was modified on 2026-07-26, after the manifest. The exact script content used on 2026-07-24 is not preserved. |
| `stage_10r` | `2026-07-24T08:03:50Z` | `a90c7214` | no | Commit `a90c7214` exists, but its tree does not contain the manifest's generating-script path. |
| `stage_10s` | `2026-07-26T15:29:38Z` | `0904e0f9` | no | Commit `0904e0f9` exists, but its tree does not contain the manifest's generating-script path. It therefore cannot fully represent that run. |
| `data_download_orchestrator` | `2026-08-10T13:46:28Z` process start | none in a run manifest | not verifiable | The Himawari-9 April download was started from the working-tree path before component run lineage was implemented. The file was later modified while the process remained alive, and no start-time script hash or Git blob was preserved. Do not retroactively assign commits `0f4d8e8b` or later to this run. |

Historical manifests are not rewritten to imply provenance that did not exist.
The code commit remains a repository-state anchor, not a content proof.

## Enforcement added

`geo_ring_cloud.lineage.write_manifest` now records:

- repository HEAD and its explicit scope;
- generating-script SHA-256;
- Git tracked/modified/untracked state;
- worktree and commit blob IDs;
- `commit_represents_script`;
- a visible lineage warning when the commit does not represent the script.

New stage scripts must use this helper. The governance check rejects new stage
scripts that omit it.

Operational downloader, transfer, upload, and notification components now use
the same generating-script state semantics. This applies to new processes only;
an already-running Python process cannot be made reproducible by changing or
committing its source file after launch.
