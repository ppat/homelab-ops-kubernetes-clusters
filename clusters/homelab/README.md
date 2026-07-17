# homelab cluster

The primary cluster: full observability/networking/security/storage/database
core stack, plus end-user applications (media, downloaders, home automation,
AI, remote dev environments). Runs the **Restricted** Pod Security Standard
(see [policies/README.md](../../policies/README.md)).

For how modules get wired in (sources/kustomizations/dependsOn/patches), see
[DESIGN.md](../../DESIGN.md). For what each module itself provides, follow the
links below into the [apps repo](https://github.com/ppat/homelab-ops-kubernetes-apps).

## Infrastructure modules

| Module | Kustomization | Provides |
| --- | --- | --- |
| [security-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/security-core/README.md) | `infra-security-core` | cert-manager, external-secrets, trust-manager, Kyverno, Policy Reporter |
| [storage-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/storage-core/README.md) | `infra-storage-core` | Longhorn, MinIO, NFS CSI driver |
| [networking-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/networking-core/README.md) | `infra-networking-core` | MetalLB, external-dns, Traefik |
| [kubernetes-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/kubernetes-core/README.md) | `infra-kubernetes-core` | CoreDNS, Node Feature Discovery, Vertical Pod Autoscaler |
| [database-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/database-core/README.md) | `infra-database-core` | CloudNativePG |
| [observability-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/observability-core/README.md) | `infra-observability-core` | Prometheus, Loki, Grafana, Goldilocks |
| [clusterops-core](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/clusterops-core/README.md) | `infra-clusterops-core` | Flux CD, system-upgrade-controller, Reloader |
| [security-extra](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/security-extra/README.md) | `infra-security-extra` | Authentik (SSO identity provider) |
| [networking-extra](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/networking-extra/README.md) | `infra-networking-extra` | Pi-hole, Unbound, Tailscale operator, FreeRADIUS (Cloudflared DoH is patched out on this cluster*) |
| [observability-extra](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/observability-extra/README.md) | `infra-observability-extra` | Node Problem Detector, SNMP Exporter, Syslog-ng, UniFi Poller |
| [kubernetes-extra](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/infrastructure/subsystems/kubernetes-extra/README.md) | `infra-kubernetes-extra` | descheduler |

\* `infra-networking-extra`'s `cloudflared-doh` `Deployment` and `pihole-secrets`
`ExternalSecret` are patched out on this cluster (see
`kustomizations/infra-networking-extra.yaml`).

## Applications

| Module | Kustomization | Provides |
| --- | --- | --- |
| [ai](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/apps/subsystems/ai/README.md) | `apps-ai` | OpenWebUI (Ollama's own `HelmRelease` is deleted on this cluster — see note below) |
| [coder](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/apps/subsystems/coder/README.md) | `apps-coder` | Remote development workspaces |
| [downloaders](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/apps/subsystems/downloaders/README.md) | `apps-downloaders` | autobrr, Sonarr, Radarr, Lidarr, Prowlarr, qBittorrent, qui, SABnzbd, Seerr |
| [home-automation](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/apps/subsystems/home-automation/README.md) | `apps-home-automation` | Home Assistant |
| [media](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/apps/subsystems/media/README.md) | `apps-media` | Agregarr, Plex, Jellyfin, FreeTube, Tautulli |
| [misc](https://github.com/ppat/homelab-ops-kubernetes-apps/blob/main/apps/subsystems/misc/README.md) | `apps-misc` | Maddy (SMTP relay) |

Note: `apps-ai`'s `ollama-release` `HelmRelease` is deleted via patch on this
cluster — Ollama runs elsewhere and this cluster's OpenWebUI points at it.

## Cluster-specific services

Resources under `services/` that aren't modules — see
[DESIGN.md#the-services-directory](../../DESIGN.md#the-services-directory)
for the general pattern.

| Path | Kind | Purpose |
| --- | --- | --- |
| `services/downloaders/downloaders-gluetun-config.yaml` | `ConfigMap` (`gluetun-config`) | VPN provider/server selection and port-forwarding hooks, read by name by the `gluetun` sidecar inside `apps-downloaders` |
| `services/downloaders/downloaders-gluetun-secrets.yaml` | `ExternalSecret` (`gluetun-secrets`) | WireGuard private key for the same `gluetun` sidecar |
| `services/tailscale/connector.yaml` | Tailscale `Connector` | Subnet router (`192.168.8.0/24`) + exit node, a custom resource for the CRD the `networking-extra` module's Tailscale operator installs — not something that module ships an instance of itself |
| `services/tailscale/proxyclass.yaml` | Tailscale `ProxyClass` | Grants the Tailscale proxy pod `NET_ADMIN` + TUN device access needed for the connector above |

## Module dependency graph

```mermaid
flowchart TB
    classDef core fill:#dcfce7,stroke:#059669,color:#064e3b
    classDef extra fill:#fca5a5,stroke:#dc2626,color:#7f1d1d
    classDef apps fill:#93c5fd,stroke:#2563eb,color:#1e3a8a

    subgraph Core["Infrastructure (Core)"]
        direction LR
        sec[security-core]:::core
        store[storage-core]:::core
        k8s[kubernetes-core]:::core
        net[networking-core]:::core
        db[database-core]:::core
        obs[observability-core]:::core
        ops[clusterops-core]:::core
    end

    subgraph Extra["Infrastructure (Extra)"]
        direction LR
        sec-x[security-extra]:::extra
        net-x[networking-extra]:::extra
        k8s-x[kubernetes-extra]:::extra
        obs-x[observability-extra]:::extra
    end

    subgraph Apps["Applications"]
        direction LR
        ai:::apps
        coder:::apps
        downloaders:::apps
        home-auto[home-automation]:::apps
        media:::apps
        misc:::apps
    end

    store --> sec
    k8s --> sec
    net --> sec & store & k8s
    db --> net & store
    obs --> sec & net & store

    sec-x --> sec & store & db
    net-x --> sec & store & net
    k8s-x --> k8s & net & store
    obs-x --> obs

    Core --> Extra --> Apps
```

`ops` (clusterops-core) has no module dependencies — it bootstraps Flux itself.
Exact per-module `dependsOn` lists are in each `kustomizations/*.yaml`.
