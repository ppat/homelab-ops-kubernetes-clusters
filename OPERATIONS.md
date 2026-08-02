# Operations

This document holds manual, host-level runbooks: procedures that happen
*outside* Flux, against a physical node or an external registry, because they
can't be expressed as a reconciled manifest. Everything else in this repo is
declarative and covered by [DESIGN.md](./DESIGN.md) instead — start here only
for the operations listed below.

These runbooks used to be automated by a Packer/Ansible pipeline that is now
unmaintained and being archived; until the planned k3s → Talos migration
removes the need for host-level node prep entirely, they're accepted as
manual, occasional, human-run procedures instead.

## Runbook: prepare a node for KubeVirt

**Why:** two VMs are being onboarded under KubeVirt — an Ubuntu VM running
`dockerd` (so unprivileged Coder workspace pods have a real Docker Engine to
point `DOCKER_HOST` at) and a single-node Talos sandbox VM for AI agents,
which doubles as a rehearsal of the eventual k3s → Talos migration. KubeVirt
needs `/dev/kvm` and a few kernel modules present on whichever node schedules
those VMs — this runbook gets a node ready and gives it a declarative label
so KubeVirt VMs can be scheduled onto it deliberately (`nodeSelector`), not
just wherever a node happens to have the modules loaded.

**Target nodes:**

| Node | IP | CPU | Memory | Why it qualifies |
| --- | --- | --- | --- | --- |
| `beelink-ser8-1` | 192.168.8.69 | AMD Ryzen, 16 vCPU | ~122Gi | Largest headroom, AMD-V (`SVM`) |
| `minisforum-nab9-1` | 192.168.8.68 | Intel, 20 vCPU | ~62Gi | Second-largest headroom, VT-x (`VMX`) |

The two GMKtec nodes are deliberately excluded — roughly 7.5Gi of headroom
each isn't enough for either VM.

### 1. Pre-flight verification (gate — do not skip)

Node Feature Discovery already reports `feature.node.kubernetes.io/cpu-cpuid.SVM=true`
on `beelink-ser8-1` and `feature.node.kubernetes.io/cpu-cpuid.VMX=true` on
`minisforum-nab9-1` (confirmed directly against the live `Node` objects while
writing this runbook). That's a CPUID read — it proves the CPU *can* do
hardware virtualization, not that the kernel module is loaded and `/dev/kvm`
exists. Whether `/dev/kvm` exists on these two nodes today is genuinely
unknown and must be checked before anything else.

**If `/dev/kvm` cannot be made to exist on both nodes, stop — do not proceed
with the KubeVirt plan.** The fallback is KubeVirt's `useEmulation: true`
(pure QEMU, no hardware acceleration), which works but is roughly an order of
magnitude slower and is documented upstream as a dev/test-only setting, not
something to run either of these VMs on long-term.

Run this on each target node (SSH in, no changes made — safe to run any
time):

```bash
#!/usr/bin/env bash
# preflight-kubevirt-node.sh -- read-only; makes no changes.
set -uo pipefail

echo "== KubeVirt host pre-flight: $(hostname) =="
pass=true

check() {
  local desc="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "PASS  $desc"
  else
    echo "FAIL  $desc"
    pass=false
  fi
}

check "/dev/kvm exists"             "[ -e /dev/kvm ]"
check "kvm module loaded"           "lsmod | awk '{print \$1}' | grep -qx kvm"
check "kvm_intel or kvm_amd loaded" "lsmod | awk '{print \$1}' | grep -qxE 'kvm_intel|kvm_amd'"
check "vhost_net module loaded"     "lsmod | awk '{print \$1}' | grep -qx vhost_net"
check "tun module loaded"           "lsmod | awk '{print \$1}' | grep -qx tun"
check "cgroup v2 unified hierarchy" "[ \"\$(stat -fc %T /sys/fs/cgroup)\" = cgroup2fs ]"
check "AppArmor active"             "systemctl is-active --quiet apparmor"

echo
if $pass; then
  echo "All gating checks passed."
else
  echo "One or more gating checks FAILED -- do not schedule KubeVirt VMs on" \
       "this node until every check above passes."
fi
```

Optional, more thorough cross-check: `virt-host-validate qemu` (from libvirt)
runs additional sanity checks (KVM device, cgroup controllers, secure-guest
support) in one pass. It ships in the `libvirt-clients` package, not the full
`libvirt-daemon-system` — installing just `libvirt-clients` doesn't pull in
or start a `libvirtd` service, so it's safe to install temporarily and purge
afterward rather than leaving libvirt permanently on the node:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y libvirt-clients
virt-host-validate qemu
# once satisfied, remove it again -- this node doesn't run libvirt itself,
# KubeVirt's virt-handler talks to /dev/kvm directly:
sudo apt-get purge -y libvirt-clients
sudo apt-get autoremove -y
```

If `/dev/kvm` is missing, check first whether virtualization is disabled in
firmware (BIOS/UEFI "SVM Mode" / "Intel Virtualization Technology") before
assuming a kernel-side problem — that's the most common reason a CPU that
reports `SVM`/`VMX` still has no `/dev/kvm`.

### 2. The change

Applied one node at a time, so the other three nodes keep etcd quorum and
serve traffic while a given node is briefly out of rotation.

`prepare-kubevirt-node.sh` (copy to the node and run as root):

```bash
#!/usr/bin/env bash
# prepare-kubevirt-node.sh -- idempotent; safe to re-run.
set -euo pipefail

LABEL_KEY="homelab.nikara.net/virtualization"
LABEL_VALUE="enabled"

echo "== Preparing $(hostname) for KubeVirt =="

# 1. Kernel modules to load on every future boot.
install -o root -g root -m 0644 /dev/stdin /etc/modules-load.d/kubevirt.conf <<'EOF'
kvm
kvm_intel
kvm_amd
vhost_net
tun
EOF

# 2. Load them now -- no reboot needed. kvm_intel fails benignly on the AMD
#    node, kvm_amd fails benignly on the Intel node: exactly one of the two
#    is expected to succeed per node, the other's failure is not an error.
for mod in kvm kvm_intel kvm_amd vhost_net tun; do
  modprobe "$mod" 2>/dev/null || true
done

# 3. Declarative node label via a k3s config drop-in, so a fresh reinstall or
#    disaster-recovery rejoin of this node picks the label back up
#    automatically instead of relying on someone remembering to `kubectl
#    label` it again. See the runbook's caveat below: on an *already*
#    registered node (this one), this alone is not sufficient.
mkdir -p /etc/rancher/k3s/config.yaml.d
install -o root -g root -m 0644 /dev/stdin \
  /etc/rancher/k3s/config.yaml.d/90-kubevirt.yaml <<EOF
node-label:
  - "${LABEL_KEY}=${LABEL_VALUE}"
EOF

echo "Done."
```

Full per-node sequence (repeat for `beelink-ser8-1`, then
`minisforum-nab9-1`):

```bash
# From wherever you have kubectl access to the homelab cluster:
kubectl cordon beelink-ser8-1

# On the node itself:
ssh beelink-ser8-1 'sudo systemctl stop k3s'
scp prepare-kubevirt-node.sh beelink-ser8-1:/tmp/
ssh beelink-ser8-1 'sudo bash /tmp/prepare-kubevirt-node.sh'
ssh beelink-ser8-1 'sudo systemctl start k3s'

# Wait for the node to report Ready again, then uncordon:
kubectl wait --for=condition=Ready node/beelink-ser8-1 --timeout=180s
kubectl uncordon beelink-ser8-1
```

**Caveat verified against current k3s docs, and it changes the plan:** k3s's
`--node-label` (and therefore the `config.yaml.d` `node-label` drop-in above)
"only add[s] labels ... at registration time, so they can only be set when
the node is first joined to the cluster" — [k3s Advanced Options docs](https://docs.k3s.io/advanced).
Both target nodes are already registered, so restarting k3s with the drop-in
in place will **not** retroactively add the label. Apply it once, directly,
right after the restart above:

```bash
kubectl label node beelink-ser8-1 homelab.nikara.net/virtualization=enabled
```

Do this once per node. The `config.yaml.d` drop-in isn't wasted effort even
though it doesn't take effect immediately here — it's what makes the label
declarative desired-state (git-visible, not `kubectl`-applied drift) and it's
what will actually apply automatically the next time either node re-registers
from scratch (reinstall, disaster recovery). Keep the drop-in and the
`kubectl label` in sync — if one is ever removed, remove the other too.

No reboot is required anywhere in this runbook: `modprobe` loads the kernel
modules immediately, and `/etc/modules-load.d/kubevirt.conf` only governs
what happens on some *future* boot.

### 3. Post-change verification

```bash
# Label present on both nodes:
kubectl get nodes -l homelab.nikara.net/virtualization=enabled

# Modules loaded (run on the node):
lsmod | grep -E '^(kvm|kvm_intel|kvm_amd|vhost_net|tun)\b'
```

`devices.kubevirt.io/kvm`, `devices.kubevirt.io/tun`, and
`devices.kubevirt.io/vhost-net` will **not** appear under the node's
`Allocatable` yet — those are advertised by KubeVirt's `virt-handler` device
plugin, which only runs once KubeVirt itself is installed on the cluster.
Their absence at this point is expected, not a sign this runbook failed.
Once KubeVirt is deployed:

```bash
kubectl describe node beelink-ser8-1 | sed -n '/Allocatable/,/System Info/p'
```

### 4. Talos equivalent

This is exactly why the change was split into "kernel modules" and "node
label" as two independent, narrow primitives — both map to one field each in
Talos's machine config, so this becomes a translation, not a rediscovery,
when the migration happens:

```yaml
machine:
  kernel:
    modules:
      - name: kvm
      - name: kvm_intel # benign failure on the AMD node, same as k3s
      - name: kvm_amd    # benign failure on the Intel node, same as k3s
      - name: vhost_net
      - name: tun
  nodeLabels:
    homelab.nikara.net/virtualization: enabled
```

Two things worth knowing before relying on this in the actual migration
(verify on first use — sources disagree on the second one):

- `machine.nodeLabels` is applied live by Talos's own controller — no
  restart needed — but is still subject to the same
  `NodeRestriction` admission-controller boundary Kubernetes applies
  everywhere: a kubelet cannot self-assign labels under a handful of
  reserved prefixes (`kubernetes.io/`, `k8s.io/`, etc.). `homelab.nikara.net/*`
  isn't one of them, so this isn't a blocker here, but it's the reason a
  reserved-prefix label would need a different mechanism.
- Whether `machine.kernel.modules` changes apply immediately or require a
  reboot was not conclusively confirmed from documentation alone; `talosctl
  apply-config` reports whether a reboot is required for a given change, so
  treat that reported mode as authoritative on first use rather than
  assuming either way.

### 5. Rollback

```bash
# On the node (reverses step 2's persistence + immediate-effect changes):
sudo rm -f /etc/modules-load.d/kubevirt.conf
sudo rm -f /etc/rancher/k3s/config.yaml.d/90-kubevirt.yaml
# Only unload the modules if no VM is currently using /dev/kvm on this node:
sudo modprobe -r vhost_net tun kvm_intel kvm_amd kvm 2>/dev/null || true

# Remove the declarative label:
kubectl label node beelink-ser8-1 homelab.nikara.net/virtualization-

# k3s doesn't need restarting to drop a removed config.yaml.d file's
# already-applied label -- the label removal above already reflects the
# node's true state going forward. Restart it anyway if you want the node's
# process config to match the file state exactly:
ssh beelink-ser8-1 'sudo systemctl restart k3s'
```

## Runbook: build and publish the Talos containerDisk

**Why:** the single-node Talos sandbox VM boots from a KubeVirt
`containerDisk` — an ordinary OCI image with the VM disk file baked in. Talos
[Image Factory](https://factory.talos.dev) (`factory.talos.dev`) serves
bootable disk images (`nocloud-amd64.raw.xz`, ISO, PXE) over **plain HTTPS
only** — never as an OCI artifact. Its `installer/` images *are* OCI, but
they're upgrade payloads meant to be applied from inside an already-running
Talos node, not bootable disks — they can't be used as a `containerDisk`
directly. So a `containerDisk` has to be built locally: download the raw
Factory image, convert it to a smaller format, wrap it in a minimal OCI
image, and push it somewhere KubeVirt can pull it from.

That somewhere is the homelab's own Harbor, which transparently proxy-caches
`docker.io`/`ghcr.io`/`quay.io` pulls via k3s's `registries.yaml`. Harbor
sits behind a router with no open inbound ports and is reachable only over
Tailscale — fine for a rare, manual push, not something to put in CI.

A community image (`docker.io/containercraft/talos`) already exists and was
explicitly rejected in favor of building from Talos's own official artifact:
it's more work, but it avoids depending on one person's unmaintained image
and is a more realistic rehearsal of the eventual Talos migration.

### Choosing the build tool: `crane`, no daemon required

KubeVirt's `containerDisk` requirement (verified against
[kubevirt.io/user-guide/storage/disks_and_volumes](https://kubevirt.io/user-guide/storage/disks_and_volumes/)
and the [KubeVirt container-disk-images doc](https://github.com/kubevirt/kubevirt/blob/main/containerimages/container-disk-images.md))
is simple: `FROM scratch`, the disk file placed under `/disk/` inside the
image, owned by UID:GID `107:107` (the `qemu` user KubeVirt's `virt-launcher`
runs as). No base image, no other files. `qcow2` is explicitly the
recommended format over raw ("Qcow2 is recommended in order to reduce the
container image's size") and is fully supported as a `containerDisk` payload.

Building that doesn't need a Dockerfile at all — it's one layer, one file.
[`crane`](https://github.com/google/go-containerregistry) builds and pushes
an OCI image from a tarball without a container daemon, and is already
resolvable via `mise` in this workspace (`aqua:google/go-containerregistry`,
confirmed via `mise registry`) — `mise use -g crane` installs it, no `apt`,
no `docker`, no root. Since it has no daemon dependency, it also sidesteps
the chicken-and-egg this task would otherwise have: the plan is to build this
image *before* the Docker VM exists (see below), and a Docker- or
buildah-based build would need exactly the container tooling that doesn't
exist yet anywhere in this environment. `crane` needing nothing but the
downloaded qcow2 and network access is what makes that possible.

**Fallback if `crane` isn't available:** `buildah` (rootless, no daemon, but
needs `newuidmap`/`newgidmap` and configured subuid/subgid ranges — more
host setup, and it's not in this workspace's `mise`/`aqua` registries, so
it'd need an `apt install buildah`) with an actual two-line Dockerfile
(`FROM scratch` / `COPY --chown=107:107 talos.qcow2 /disk/`), then `buildah
build` + `buildah push`. `skopeo` copies/inspects existing images but doesn't
build one from a local file, so it isn't a substitute here.

**Where to run this:** this Coder workspace has no Docker (no
docker-in-docker), but since `crane` needs no daemon, that's not actually a
blocker — install `crane` here via `mise use -g crane` and run the whole
script in this workspace. It's equally fine to run it later from the Docker
VM once that exists, since `crane` has no dependency on which came first. If
you'd rather build with Docker/`buildah` rather than `crane` for some future
image, bootstrap order matters: the Docker VM needs to exist first, since
nothing else in this environment currently has a container build daemon.

### What a schematic is, and how to get one

A **schematic** is Image Factory's name for "a Talos image customization" —
system extensions to bake in, extra kernel args, and similar. You POST a
small YAML description of the customization to the Factory API and get back
a deterministic, content-addressed **schematic ID**: the same customization
always produces the same ID, and the ID never changes retroactively (a
different set of extensions is a different ID, not a mutation of this one).
That ID is then part of the download URL for every asset built from it, at
every Talos version.

Two ways to get one:

- **Web UI** — <https://factory.talos.dev>, walk through hardware type →
  Talos version → system extensions, and it hands you the resulting
  schematic ID plus ready-made download links.
- **API**, for scripting or when you already know exactly what you want:

  ```bash
  cat > schematic.yaml <<'EOF'
  customization:
    systemExtensions:
      officialExtensions: []   # e.g. siderolabs/iscsi-tools, siderolabs/intel-ucode
  EOF

  curl -sX POST --data-binary @schematic.yaml https://factory.talos.dev/schematics
  # -> {"id":"<schematic-id>"}
  ```

This is the part of the runbook that actually rehearses the migration — when
you later want an iSCSI extension, a GPU driver, or anything else on the
sandbox, you add it to `officialExtensions` and get a new schematic ID; you
don't hand-patch a boot image.

For a completely stock image with no extensions, the empty schematic above
resolves to a well-known ID
(`376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba`, seen
consistently across Talos's own documentation and examples while researching
this runbook) — but generate your own from the exact `schematic.yaml` you
intend to use rather than hardcoding that value, since it's only correct for
the *empty* customization.

### The build script

Takes the Talos version and schematic ID as required inputs — no defaults,
so this never silently builds a stale version. Registry host is a required
variable too, per this repo's convention of never hardcoding a domain.

`build-talos-containerdisk.sh`:

```bash
#!/usr/bin/env bash
# build-talos-containerdisk.sh -- downloads a Talos Image Factory boot asset,
# converts it to qcow2, wraps it as a KubeVirt containerDisk OCI image, and
# pushes it to a registry. Non-interactive; safe to re-run (each run uses its
# own temp dir and overwrites the destination tag on push).
set -euo pipefail

usage() {
  cat <<EOF
Usage: TALOS_VERSION=v1.8.3 SCHEMATIC_ID=<id> REGISTRY=harbor.example.com \\
       [IMAGE_PATH=talos/containerdisk] [HARBOR_USER=...] [HARBOR_PASSWORD=...] \\
       $0
EOF
  exit 1
}

: "${TALOS_VERSION:?TALOS_VERSION is required, e.g. v1.8.3}"
: "${SCHEMATIC_ID:?SCHEMATIC_ID is required -- see OPERATIONS.md for how to get one}"
: "${REGISTRY:?REGISTRY is required, e.g. harbor.example.com (never hardcode this)}"
IMAGE_PATH="${IMAGE_PATH:-talos/containerdisk}"
IMAGE_REF="${REGISTRY}/${IMAGE_PATH}:${TALOS_VERSION}-${SCHEMATIC_ID:0:12}"

command -v crane >/dev/null || { echo "crane not found -- mise use -g crane" >&2; exit 1; }
command -v qemu-img >/dev/null || { echo "qemu-img not found -- apt install qemu-utils" >&2; exit 1; }
command -v xz >/dev/null || { echo "xz not found -- apt install xz-utils" >&2; exit 1; }

if [ -n "${HARBOR_USER:-}" ] && [ -n "${HARBOR_PASSWORD:-}" ]; then
  crane auth login "${REGISTRY}" -u "${HARBOR_USER}" --password-stdin <<<"${HARBOR_PASSWORD}"
fi

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

asset_url="https://factory.talos.dev/image/${SCHEMATIC_ID}/${TALOS_VERSION}/nocloud-amd64.raw.xz"
echo "Downloading ${asset_url}"
curl -fL -o "${workdir}/talos.raw.xz" "${asset_url}"

echo "Decompressing"
xz -d "${workdir}/talos.raw.xz"

echo "Converting to sparse qcow2"
qemu-img convert -O qcow2 "${workdir}/talos.raw" "${workdir}/talos.qcow2"
du -h "${workdir}/talos.raw" "${workdir}/talos.qcow2"

echo "Building containerDisk layer (disk/talos.qcow2, owned 107:107)"
mkdir -p "${workdir}/layer/disk"
cp "${workdir}/talos.qcow2" "${workdir}/layer/disk/talos.qcow2"
tar --owner=107 --group=107 -C "${workdir}/layer" -cf "${workdir}/layer.tar" disk

echo "Pushing ${IMAGE_REF}"
crane append -f "${workdir}/layer.tar" -t "${IMAGE_REF}"

echo
echo "containerDisk image: ${IMAGE_REF}"
```

Notes on specific lines:

- `qemu-img convert -O qcow2` produces a sparse file by default (no
  preallocation flag needed) — this is what keeps the pushed image small.
  The ~100–200MB qcow2 vs ~1GB raw figures from the design brief are
  estimates, not something measured while writing this runbook — confirm
  actual sizes on the first real run via the `du -h` line above.
- `crane append` with no `-b` (base image) flag builds a new image from an
  empty/scratch base per its own docs — exactly what `FROM scratch` means in
  Dockerfile terms.
- The Harbor **project** in `IMAGE_PATH` (e.g. `talos`) must already exist —
  Harbor doesn't auto-create projects on push the way some registries do.
- `crane auth login` is skipped entirely if `HARBOR_USER`/`HARBOR_PASSWORD`
  aren't set, in which case `crane` falls back to whatever's already in
  `~/.docker/config.json` (e.g. a prior interactive `docker login` or `crane
  auth login`). Either path is non-interactive.

Reference the resulting image in a `VirtualMachine` manifest as a
`containerDisk` volume, e.g.:

```yaml
spec:
  domain:
    devices:
      disks:
        - name: talos-system
          disk: {}
  volumes:
    - name: talos-system
      containerDisk:
        image: harbor.example.com/talos/containerdisk:v1.8.3-376567988ad3
```

### Talos version bumps are now a manual re-run

There's no CI wired up for this — bumping Talos means re-running the script
above with the new `TALOS_VERSION` (same `SCHEMATIC_ID` unless the
customization also changed) and updating whichever `VirtualMachine` manifest
references the resulting tag. Renovate *can* be configured to watch
`siderolabs/talos` GitHub releases and open an informational PR the same way
it's already annotated for k3s
(`# renovate: datasource=github-releases depName=k3s-io/k3s` in
`clusters/homelab/cluster/kubernetes-version/server-upgrade.yaml`) — but
nothing actually builds or pushes the image automatically, and setting up
that watch is a separate follow-up, not done as part of this runbook. Accept
this as a standing, real cost of building from the official artifact instead
of relying on someone else's automated image.
