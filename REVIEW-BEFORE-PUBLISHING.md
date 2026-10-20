# Proposed public Intel dependency build

Status: prepared locally; NOT published or approved.

Destination if approved: the existing public
`teal92/ClipBotV2-setup-guide` repository, in a new reviewed commit. The public
workflow would run only the third-party Intel ONNX CPU build. This avoids spending
the private repository's remaining 113.3 included minutes. Standard GitHub-hosted
public runners are documented as free; the owner's $0 spending block remains.

The exact 22 existing generic helper/pin/test files are listed with sizes and
SHA-256 hashes in PUBLIC-FILE-MANIFEST.json, plus this review file and the new
`.github/workflows/intel-cpu-build.yml`. They include source verification,
architecture identification, pinned Python/package inputs, safe extraction,
compiler commands and focused tests. The published build helper would allow at
most 60 minutes for CPU compilation; the whole manual job is bounded at 80 minutes.
The failed private run was bounded at 30 compiler minutes and produced no wheel.

No server/frontend/billing/auth/customer/AI-personalization implementation,
credentials, customer records, media, device tokens, pairing secrets or private
connection settings are included. `worker/` contains only package initializers
and the generic OS/CPU identity helper. This is nevertheless publication of RSSI's
own generic build tooling, so owner approval is required before uploading it.

The job has read-only repository permission, immutable action pins, no production
secrets, no pull-request or push triggers, and one-day internal artifact retention.
It cannot deploy the website, charge customers, call Claude, publish social posts,
or enable an Intel customer download. No new service subscription is proposed.

A successful build would still need actual artifact/source review and a separate
complete Intel companion package/acceptance path. This proposal solves only the
long third-party dependency compilation step. It does not guarantee the remaining
private minutes will cover full package assembly. Additional free build isolation
or a trusted Intel Mac may still be needed; do not quietly make ClipBot application
source public to obtain free runners.

Published Windows 0.2.10 and Apple Silicon 0.3.0-beta.1 downloads remain available
and unchanged. Linux remains paused.
