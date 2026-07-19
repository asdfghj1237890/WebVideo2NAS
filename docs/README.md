# Documentation Index

Documentation for WebVideo2NAS is split into two trees. This page is the map —
each topic has **one** source of truth; follow the link rather than duplicating.

## User-facing reference (this folder)

Comprehensive reference for operators and integrators.

| Document | Source of truth for |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System architecture, data-flow diagrams, DB schema overview, Redis structures, multi-worker layout, deployment topology |
| [SPECIFICATION.md](SPECIFICATION.md) | API endpoint contracts, request/response models, job status vocabulary, technology stack, directory layout |
| [PRIVACY_SECURITY.md](PRIVACY_SECURITY.md) | Data handling, secrets, storage locations, browser-side safety model, disclosure notes |
| [THREAT_MODEL.md](THREAT_MODEL.md) | Adversary model, trust boundaries, and mitigations *(maintained separately)* |

## Developer / internal docs

Implementation deep-dives for people changing the code live in
[`development/`](development/) — see its [index](development/README.md). Chapters:
getting started, architecture internals, chrome extension, worker pipeline,
API + DB internals, testing, CI/release, and bug case studies.

## Deployment & operations

- **Install / configure / troubleshoot / changelog** → root [README.md](../README.md)
- **Synology copy-paste command reference** → [`video-downloader/docker/SYNOLOGY_DEPLOY_COMMANDS.md`](../video-downloader/docker/SYNOLOGY_DEPLOY_COMMANDS.md)
- **Annotated environment variables** → [`video-downloader/docker/.env.example`](../video-downloader/docker/.env.example)

## Avoiding drift

When two documents would describe the same thing, the owner above wins and the
other should link to it. When you change behaviour, update the owning document
in the same change. Test counts, service/container names, and env-var defaults
are the usual drift culprits — verify against the actual source file before
copying a number or name into prose.
