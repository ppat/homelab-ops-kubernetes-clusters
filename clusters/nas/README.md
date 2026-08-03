# nas cluster

Secondary cluster co-located with the NAS, backed by NFS storage. Runs
Bitwarden, the Harbor container registry, and an Authentik SSO outpost for
cross-cluster authentication. Runs the **Baseline** Pod Security Standard (see
[policies/README.md](../../policies/README.md)).

For how modules get wired in (sources/kustomizations/dependsOn/patches), see
[DESIGN.md](../../DESIGN.md). For what each module itself provides, follow the
links below into the [apps repo](https://github.com/ppat/homelab-ops-kubernetes-apps).

## Infrastructure modules

| Module | Kustomization(s) | Provides |
| --- | --- | --- |
| [security-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/security-core/README.md) | `infra-security-core` | cert-manager, external-secrets, trust-manager, Kyverno, Policy Reporter |
| [storage-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/storage-core/README.md) | `infra-storage-csi-driver-nfs`, `infra-storage-minio` | NFS CSI driver, MinIO — deployed as two separate `Kustomization`s pointing at submodule paths (`storage-core/csi-driver-nfs`, `storage-core/minio`) instead of one, since this cluster has no Longhorn |
| [networking-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/networking-core/README.md) | `infra-networking-core` | MetalLB, external-dns, Traefik (patched to run as a 2-replica `Deployment` instead of a `DaemonSet` for redundancy) |
| [kubernetes-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/kubernetes-core/README.md) | `infra-kubernetes-core` | CoreDNS, Node Feature Discovery, Vertical Pod Autoscaler |
| [database-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/database-core/README.md) | `infra-database-core` | CloudNativePG, Dragonfly operator (Redis-compatible cache instances) |
| [clusterops-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/clusterops-core/README.md) | `infra-clusterops-core` | Flux CD, system-upgrade-controller, Reloader |

This cluster runs no `*-extra` infrastructure modules and no `observability-core`.

## Applications

| Module | Kustomization | Provides |
| --- | --- | --- |
| [bitwarden](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/apps/subsystems/bitwarden/README.md) | `apps-bitwarden` | Self-hosted password vault |
| [harbor](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/apps/subsystems/harbor/README.md) | `apps-harbor` | Container image registry with vulnerability scanning |

## Cluster-specific resources

Unlike `homelab`, this cluster has no `services/` directory — there's no
`config-services` umbrella Kustomization here either. Instead, content
authored directly in this repo (rather than pulled from the apps repo as a
versioned module) lives as its own top-level directory, each with its own
dedicated `Kustomization` sourced from `root`, not a module `GitRepository`:

| Directory | Purpose |
| --- | --- |
| `outpost/` | Deploys a remote Authentik outpost, with the routing/ingress to reach it, so this cluster's Ingress can authenticate against the Authentik instance running on `homelab` without running Authentik itself here |
| `harbor-dockerio-mirror/` | An `Ingress` + rewrite `Middleware` pair fronting `harbor-core` on its own hostname (`dockerio-harbor.${domain_name}`), rewriting `/v2/<repo>/...` to `/v2/docker.io/<repo>/...` so Docker Hub pulls from any Docker client on the network land in Harbor's `docker.io` proxy-cache project. Exists because Docker's `registry-mirrors` has no equivalent of containerd's `overridePath`, so it can only work against a registry that accepts unprefixed `/v2/...` paths — see `apps-harbor-dockerio-mirror`'s `Kustomization` and ppat/homelab-ops-kubernetes-experiments#226 |

This exists because SSO (`components/sso`) is used across both clusters, but
Authentik itself (`security-extra`) only runs on `homelab` — the outpost lets
`nas`'s apps (Harbor, Bitwarden) participate in the same SSO domain. The
`docker.io` mirror ingress exists for the unrelated reason above, and is
listed here purely because it shares the same "content authored directly in
this repo, sourced from `root`" shape as `outpost/`.

## Module dependency graph

```mermaid
flowchart TB
    classDef core fill:#dcfce7,stroke:#059669,color:#064e3b
    classDef apps fill:#93c5fd,stroke:#2563eb,color:#1e3a8a
    classDef outpost fill:#fde68a,stroke:#d97706,color:#92400e
    classDef mirror fill:#fecaca,stroke:#dc2626,color:#7f1d1d

    subgraph Core["Infrastructure (Core)"]
        sec[security-core]:::core
        nfs[storage: csi-driver-nfs]:::core
        minio[storage: minio]:::core
        k8s[kubernetes-core]:::core
        net[networking-core]:::core
        db[database-core]:::core
        ops[clusterops-core]:::core
    end

    subgraph Apps["Applications"]
        bw[bitwarden]:::apps
        harbor:::apps
    end

    out[Authentik outpost]:::outpost
    dio[docker.io mirror ingress]:::mirror

    k8s --> sec
    net --> sec & nfs
    minio --> nfs
    db --> net & nfs
    out --> sec & net
    dio --> net & harbor

    Core --> Apps
```

`ops` (clusterops-core) has no module dependencies — it bootstraps Flux itself.
`out` (Authentik outpost) and `dio` (docker.io mirror ingress) aren't apps-repo
modules — they're the top-level, repo-authored Kustomizations described in
[Cluster-specific resources](#cluster-specific-resources) above, included here
because they carry real `dependsOn` edges of their own. Exact per-module
`dependsOn` lists are in each `kustomizations/*.yaml`.
