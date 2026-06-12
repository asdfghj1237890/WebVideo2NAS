# Privacy & Security Disclosure

Last updated: 2026-06-13

This document describes what WebVideo2NAS handles when you run the Chrome
extension and the self-hosted NAS service. It is an engineering disclosure, not
legal advice.

## Product Scope

WebVideo2NAS is a self-hosted tool for sending detected web media URLs from
Chrome to a NAS-side downloader. There is no developer-operated cloud service
in the normal product path: the extension talks to the NAS endpoint you
configure, and Docker images are pulled from GHCR.

Use this project only for content you are allowed to access and archive. The
project does not support DRM bypass and is not designed for public, multi-tenant
hosting.

## Data The Extension Can Access

Because the extension detects media requests and supports browser-session
downloads, it requests broad Chrome permissions including host access,
`webRequest`, `cookies`, `declarativeNetRequest`, and `offscreen`.

Depending on the workflow, the extension may handle:

- Media URLs, manifest URLs, segment URLs, and source page URLs.
- Request metadata such as Referer, Origin, User-Agent, and selected custom
  request headers needed to reproduce the media request.
- Cookie headers for NAS-direct jobs when Chrome cookies are needed by the NAS
  worker.
- Browser cookies implicitly used by browser-side fetches with
  `credentials: "include"`.
- NAS endpoint, API key, UI preferences, output subdirectory, trusted CDN
  suffixes, and bounded local task/history state.

The extension does not intentionally collect analytics, advertising identifiers,
or browsing data unrelated to the user-facing media detection/download feature.

## Data Sent To The NAS

The configured NAS API may receive:

- Download job metadata: URL, title, source page, referer, selected headers,
  output subdirectory, and format hints.
- For browser-side HLS/DASH jobs: the manifest text or manifest URL, segment
  plan, init segments, media segments, AES key responses when required by the
  stream, and finalize/abort messages.
- Authentication to the NAS API via `Authorization: Bearer <API_KEY>`.

The NAS service stores job rows in Postgres, queue state in Redis, temporary
browser-side staging files under `.staging`, and completed media files in the
configured downloads directory. Cleanup jobs prune old job metadata and remove
partial files for failed/cancelled work; completed output files remain until
the operator deletes them.

## Secrets And Logging

Treat these as secrets:

- `API_KEY`
- Cookie headers and authentication cookies
- Authorization headers or custom media access tokens
- Signed media URLs and source URLs containing tokens
- NAS hostnames or private IPs if your deployment is not public

The code redacts Cookie and Authorization values in debug logs. Logs can still
contain URLs, hostnames, header names, sizes, error details, and job IDs. Review
logs before sharing them in public issues.

## Storage Locations

- Chrome `storage.sync`: NAS endpoint, API key, UI preferences, trusted CDN
  suffixes, output subdirectory, and related settings. Chrome may sync this
  storage through the user's signed-in Chrome profile.
- Chrome `storage.local`: bounded browser-side job recovery state and local
  task/history state.
- NAS Postgres/Redis: job metadata, status, queues, and operational counters.
- NAS filesystem: staged segments, temporary partial files, and final output
  media.

Uninstalling the extension removes its local extension storage. To remove NAS
data, delete the Docker volumes/database rows, staging files, logs, and output
files according to your deployment needs.

## Network Security

The default deployment is intended for LAN or VPN access. Do not expose the API
directly to the public internet.

Recommended production posture:

- Put the service behind a VPN such as Tailscale or WireGuard, or a trusted
  HTTPS reverse proxy.
- Keep `API_KEY` strong and private.
- Set `ALLOWED_CLIENT_CIDRS` for known client networks where practical.
- Enable `SSRF_GUARD=true` for NAS-direct jobs when your source set permits it.
- Prefer HTTPS/WSS for any path carrying personal or sensitive user data beyond
  a trusted local machine or private network.

Chrome's user-data guidance requires privacy disclosure and secure handling for
extensions that handle sensitive data such as cookies, URLs, website content,
and browsing activity. See Chrome's official policy guidance:
https://developer.chrome.com/docs/webstore/program-policies/user-data-faq

## Browser-Side Safety Model

Browser-side mode exists because some authorized streams are bound to the
browser's cookies, IP, or short-lived signed URLs. This mode is intentionally
restricted:

- Manifest fetches are HTTPS-only.
- Localhost, private, link-local, reserved, and special-use IP literals are
  rejected.
- Cross-site manifests/segments require same-site trust or an explicit trusted
  CDN suffix.
- DNR rules are scoped to the extension initiator and only apply to trusted URL
  groups.
- The API revalidates browser-side plans before accepting staged uploads.

Trusted CDN suffixes are a powerful exception. Add only host suffixes you
control or explicitly trust.

## Third Parties

Normal operation does not send video URLs, cookies, media bytes, or browsing
history to the project maintainer. Third parties may still be involved through
your own infrastructure choices, such as:

- GitHub/GHCR for releases and Docker image pulls.
- Your VPN, DNS, reverse proxy, NAS vendor, browser profile sync, or hosting
  provider.
- The source websites you already visit in Chrome.

The project does not use third-party analytics or advertising SDKs.

## Vulnerability Reporting

Please report security issues through GitHub Security Advisories instead of
public issues:

https://github.com/asdfghj1237890/WebVideo2NAS/security/advisories/new

Include affected versions, reproduction steps, relevant logs with secrets
redacted, and impact assessment when possible.
