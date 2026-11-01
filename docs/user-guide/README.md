# Alport website user guide

The user requires guide updates whenever website additions or changes affect users. The existing Alport website manager coordinates follow-up refreshes after deployments; do not create a second watcher.

## Build

Install the authoring-only dependencies `reportlab` and `Pillow`, then run:

    python docs/user-guide/build_guide.py

The script writes `private_docs/Alport-Website-User-Guide.pdf`. These authoring dependencies are not required by the web server. The PDF is delivered through the login-protected `/user-guide/download` route; it must not be moved under `static/`.

## Refresh procedure

1. Confirm the relevant website revision is deployed. Read the current UI and source before describing workflows. Do not copy feature descriptions from unmerged PRs.
2. Capture the affected screens through the browser using a signed-in session. Use empty or demonstration data; never publish credentials, personal guest/staff details, chat messages or private financial records in the guide.
3. Replace the matching JPEGs in `screenshots/`. Crop browser chrome and irrelevant empty space while keeping useful navigation context. The builder uses the prepared JPEGs directly, so screen resolution does not dictate fixed crop coordinates.
4. Update text and diagrams in `build_guide.py`. Add pages for new workflows and keep the contents and landing page accurate. Update `revision.json` with the deployed commit and capture time.
5. Build, render every PDF page, and visually inspect screenshots, line wrapping, page breaks and footers. Check extracted text and total page count as a secondary check.
6. Verify the authenticated landing page and download, and the unauthenticated redirect. Confirm the live PDF after deployment.
7. Report to the Alport website manager which deployment was captured, what changed, validation, and any pending UI changes.

Changes that do not affect user instructions still require an explicit guide-impact review, but do not require fabricated screenshot changes. Keep a fresh guide update queued for every feature that is still awaiting deployment.
