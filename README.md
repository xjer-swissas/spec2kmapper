# Spec2500 preprocessing

The wizard compares uploaded records with the tenant's cached AMOS configuration.
Exact part numbers are preselected. Normalized identifiers, fuzzy matches, and
matches supported by descriptions are suggestions requiring a user selection.
The confirmed root is excluded from subpart review and is applied to all its XML
references during export.

## Matching

- RapidFuzz compares identifiers and description text.
- FastEmbed runs `BAAI/bge-small-en-v1.5` locally on CPU to compare description
  meaning when an identifier is not already recognized. Exact and normalized
  identifiers rank ahead of description-based candidates.
- AMOS descriptions already come from `amos.part.description`. The parser reads
  `PartDescription`, `Description`, `Nomenclature`, or `PartName` beside XML part
  numbers, or directly under their `PartDetails` record. Missing descriptions
  leave identifier matching available.
- The score is a ranking signal, not a calibrated probability or proof of part
  interchangeability. Validate suggestions against engineering requirements.
- Suggested subparts come from the selected root's cached configuration. The
  root suggestion pool includes configured assemblies and recognized identifiers
  to avoid embedding unrelated master descriptions across large catalogues. The
  searchable “Choose from all AMOS parts” dialog can select any cached master
  part; choices outside the root configuration are labeled explicitly.

`SEMANTIC_MATCHING=false` disables description embedding inference. `SEMANTIC_MODEL` selects a
FastEmbed model; `SEMANTIC_CACHE_DIR` sets its disk cache. The Docker image
preloads the default model. Inference does not send descriptions to a remote
API. If the model is unavailable, matching falls back to text similarity and
shows a warning. Description vectors have a bounded in-process cache.

For more advanced retrieval, Sentence Transformers supports embedding model
training and cross-encoder reranking. Domain-specific evaluation with confirmed
part mappings should guide model or scoring changes.

## Sessions and navigation

Expired or rejected access tokens return to sign-in. This app does not issue
refresh tokens. The open tab retains the wizard state; signing into the same
tenant resumes it. Backend restarts clear uploaded sessions, and browser reloads
clear unsaved wizard state. Back navigation preserves mappings for the same
root; confirming a different root resets them. Start over clears the wizard.

## AMOS cache and sync

Cached AMOS data is searchable by category: master parts/descriptions, part
relationships, positions, and documents. The settings panel shows counts,
current sync phase, last successful refresh, and failures. Manual sync returns
immediately and runs in a background task. Automatic sync checks every five
minutes and respects each tenant's refresh interval. Repeated sync requests do
not queue a second job for an already-running tenant. Cache replacement and the
success status commit in one transaction; a failure retains the previous cache.

The deployment uses one backend worker. Startup marks interrupted sync jobs for
retry; a multi-worker deployment would need a durable job queue and shared
upload-session storage before increasing the worker count.

## AMOS CSV imports

Customers without a direct AMOS connection can open **AMOS DB → SQL / CSV import**.
Copy and run the three provided queries in AMOS PostgreSQL, then export parts,
assembly configurations, and documents as UTF-8 comma-separated CSV files with
headers. Upload all three files together (25 MB maximum each). Preserve identifiers
as text; do not let spreadsheet tools remove leading zeros. Empty configuration
and document results should be exported with headers only; parts cannot be empty.

The app validates all files before replacing the tenant cache in one transaction.
It builds the same inherited relationships, functional positions, and documents
as direct sync and refreshes the search index. Failed imports retain existing data.
A successful import selects CSV mode and disables scheduled/direct database sync.
To restore direct syncing, select **Direct database** under **Connection settings**
and save. Restart the backend after updating to apply the data-source migration.

## Run and verify

```sh
docker compose up -d --build backend
python -m unittest discover -s tests -v
```

`tests/browser_smoke.py` uses Playwright Chromium and mocked API responses to
verify the UI. `tests/postgres_smoke.py` tests the real cache queries in an
isolated schema and rolls back all its changes. Both are separate from the unit
test suite. Browser testing requires Playwright and its browser dependencies;
PostgreSQL testing uses `DATABASE_URL`.

## Hybrid catalogue search

In the all-parts picker, choose **Part number + description meaning**. Enter a
part number, a description, or both. Identifiers use exact, normalized, and fuzzy
matching; only descriptions are vectorized. Results show separate evidence and
root-configuration membership and require explicit selection.

A tenant-local, exact cosine vector index is built in the background at startup
and after a successful AMOS sync. Repeated descriptions share vectors. Embedding
batches persist in SQLite under `SEMANTIC_INDEX_DIR` (default
`uploads/semantic-index`, on the existing Docker uploads volume). Changed or new
descriptions are embedded once; obsolete descriptions do not produce results
for parts removed from the current catalogue. Models and tenant indexes are
isolated. Completed batches are searchable during construction, with partial
coverage clearly labeled. AMOS DB shows progress and an Update index action.

The first build can take substantial CPU time; subsequent starts reuse saved
vectors. Text/PN search stays available if the model or index is unavailable.

## Per-customer matching settings

AMOS DB → Settings lets administrators select Current scoring or Reciprocal Rank
Fusion (RRF). Defaults preserve existing behavior. RRF sums `1 / (60 + rank)`
across available PN, description-text, and semantic rankings; equal scores have
equal ranks. Exact and normalized identifiers retain priority. The selected mode
applies to root/subpart suggestions and hybrid catalogue search. FP/document
matching is unchanged. Settings take effect on new analyses/searches.

Confirmed-mapping collection and evaluation/tuning are separately disabled by
default. With collection enabled, new subpart analyses provide explicit review
controls for a selected AMOS part or no valid match. These capture the reviewer,
source description, root, target and up to 50 saved suggestions. Exporting or
selecting a part does not confirm it. Reconfirming the same source/root/description
updates the saved example. Disabling collection retains existing examples.

Evaluation compares weighted ranking with RRF on up to 1,000 saved examples.
Trial PN weights affect evaluation only. Applying a weight requires entering it
as the live PN weight and explicitly saving settings. It applies to the weighted
root/subpart dropdown, not the hybrid picker's existing default scoring. These
are retrospective results on captured suggestion pools, not held-out accuracy
or automatic training. No-match cases and targets outside saved pools are
reported separately. No automatic tuning is performed.

## Optional description reranker

AMOS DB → Settings → **Description reranker** offers `none` (default) and
`cross_encoder`, independently of weighted/RRF ranking. It applies to new root
and subpart analyses and hybrid catalogue searches. Plain catalogue text search,
functional positions and documents are unchanged.

The initial ranking supplies a bounded shortlist. Within its first 50 candidates,
non-exact parts with nonempty descriptions are reordered by the local
`BAAI/bge-reranker-base` cross-encoder using description pairs only. Exact and
normalized identifiers, parts missing descriptions, and candidates outside that
window retain their positions. Equal cross-encoder scores preserve initial order.
Missing query descriptions or fewer than two eligible candidates skip inference.
The reranker never changes automatic selection or review requirements.

`RERANK_MODEL` selects the FastEmbed cross-encoder. `RERANK_TOP_N` sets the window
(default 50, allowed 2–100). `RERANK_CACHE_DIR` defaults to
`uploads/reranker-models`, persisted by the Docker uploads volume. The first use
may download approximately 1 GB of public model files and take extra time;
subsequent inference is local. No descriptions are sent to a remote service.
Model failures preserve the initial order, display a warning, and defer retries
for five minutes. Cross-encoder scores are raw relevance signals, not probabilities,
and remain separate from PN, semantic, weighted and RRF scores.

Restart the backend after upgrading: startup adds the tenant `reranker` column
with `none` for existing tenants. No existing tenant is automatically opted in.
The retrospective evaluation screen still compares only weighted/RRF initial
rankings on saved pools; it does not evaluate cross-encoder accuracy.
