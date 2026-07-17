# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start here

- [README.md](./README.md) — what this repo is, how it relates to the sibling apps repo, per-cluster catalog links
- [DESIGN.md](./DESIGN.md) — directory anatomy, how a module gets wired into a cluster (sources/kustomizations/dependsOn/components/postBuild/patches), the `services/` pattern, secrets/RBAC/storage model, CI, versioning
- [clusters/homelab/README.md](./clusters/homelab/README.md) / [clusters/nas/README.md](./clusters/nas/README.md) — what's actually deployed on each cluster, with links to each module's own README in the apps repo
- [policies/README.md](./policies/README.md) — Kyverno policy groups and what's enforced where

This is a GitOps repo (FluxCD) of Kustomize manifests — there is no application
code to build. "Development" here means authoring/editing YAML and validating
it. This repo defines *no modules of its own* — it only pins versions of, and
configures, modules that live in the sibling apps repo (see below).

## Repository layout

- `clusters/<name>/sources/` — one Flux `GitRepository` per module, pinned to a released tag of the apps repo
- `clusters/<name>/kustomizations/` — one Flux `Kustomization` per module (the actual deploy wiring: `dependsOn`, `components`, `postBuild`, `patches`); `root.yaml` is the Flux bootstrap entry point
- `clusters/<name>/cluster/` — cluster-wide, not module-specific: k8s version upgrades, RBAC bindings, Bitwarden-backed cluster secrets
- `clusters/<name>/storage/` — `PersistentVolume`/`PersistentVolumeClaim`/`StorageClass` split `infra/`/`apps/`, pre-provisioned ahead of the module that claims them
- `clusters/<name>/services/` — cluster-specific extras that aren't modules: either config/secrets a module looks up by name, or standalone CRs with no module awareness. See [DESIGN.md#the-services-directory](./DESIGN.md#the-services-directory)
- `clusters/nas/outpost/` — nas-specific: an Authentik remote outpost, authored directly in this repo (not sourced from the apps repo)
- `policies/` — shared, cluster-agnostic Kyverno `ClusterPolicy`/`ClusterCleanupPolicy` definitions, applied to both clusters
- `ci/validation/` — base kustomization + `.env` of dummy post-build substitution variables used by kubeconform validation

## How this repo consumes the apps repo (cross-repo)

Modules — infrastructure subsystems, applications, cross-cutting components —
are built, versioned, and released independently in the sibling
`homelab-ops-kubernetes-apps` repo, checked out alongside this one
(`../homelab-ops-kubernetes-apps`). This repo has no modules of its own; every
`sources/<module>.yaml` + `kustomizations/<module>.yaml` pair pins a released
tag of that repo and configures it for one specific cluster. The same module
can be referenced by both clusters at different versions with different
components/patches/variables. When changing something here, check the
matching module's README in the apps repo for its prerequisites and
dependencies before assuming a config change is valid.

Full mechanics — what `dependsOn`/`components`/`postBuild`/`patches` do and a
worked example — are in [DESIGN.md](./DESIGN.md#wiring-a-module-into-a-cluster).

## Commands

### Lint / validate locally (same checks CI enforces on every PR)

```bash
pre-commit run --all-files      # yamllint, markdownlint, shellcheck, commitlint, kubeconform, etc.
```

Individual checks can be run standalone: `yamllint --strict <path>`,
`markdownlint-cli2 --fix --config .markdownlint-cli2.yaml <path>`, `shellcheck <script>`.

Kubernetes manifest validation (kubeconform via the `validate-kubernetes-manifests`
pre-commit hook) uses `ci/validation/kustomization.yaml` as the base
kustomization and `ci/validation/.env` for dummy post-build substitution
values, restricted to `clusters/*` and `policies/*` (excludes `components/*`
and `.archive/*`).

### CI enforcement

Every PR runs `.github/workflows/lint.yaml`, which fans out to per-file-type
jobs (commit-messages via commitlint, github-actions, kubernetes-manifests via
kubeconform, markdown, pre-commit, renovate-config-check, shellcheck, yaml) —
each gated on whether matching files changed. Separately,
`.github/workflows/diff-changes.yaml` checks out the apps repo alongside
before/after versions of this repo and comments a rendered `flux-diff` of
affected `HelmRelease`/`Kustomization` resources on any PR touching
`clusters/**` or `policies/**`. Full breakdown in
[DESIGN.md#ci-and-validation](./DESIGN.md#ci-and-validation).

## Commit conventions

Conventional Commits, enforced by commitlint (`commitlint.config.js`):

- header max 120 chars; scope must be one of: `cluster-homelab`, `cluster-nas`, `dev-tools`, `github-actions`, `kubernetes-api`, `policies`, `renovate`, `release`
- version bumps to a module use scope matching the cluster, e.g. `chore(cluster-homelab): deploy infra-security-core (v0.2.5 -> v0.2.6)`

## Dependency updates

Renovate manages module version bumps (`clusters/*/sources/*.yaml`) and k3s
version bumps (`cluster/kubernetes-version/server-upgrade.yaml`). Module bumps
never auto-merge (config-split across `.github/renovate/*.json`); k3s patches
auto-merge after a 7-day soak, major/minor require review. See
[DESIGN.md#versioning-and-updates](./DESIGN.md#versioning-and-updates).
