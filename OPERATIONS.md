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
needs `/dev/kvm` present on whichever node schedules those VMs — this
runbook verifies a node is actually capable and gives it a declarative label
so KubeVirt VMs can be scheduled onto it deliberately (`nodeSelector`), not
just wherever a node happens to be capable.

The label key below, `homelab-ops.internal/virtualization`, is a fixed
constant the `infra-virtualization-core` module itself hardcodes (see that
module's README in the apps repo) — it's this cluster's scheduling contract
with the module, not a value chosen per-cluster, so every command in this
runbook uses it verbatim.

**Target nodes:**

| Node | IP | CPU | Memory | Why it qualifies |
| --- | --- | --- | --- | --- |
| `beelink-ser8-1` | 192.168.8.69 | AMD Ryzen, 16 vCPU | ~122Gi | Largest headroom, AMD-V (`SVM`) |
| `minisforum-nab9-1` | 192.168.8.68 | Intel, 20 vCPU | ~62Gi | Second-largest headroom, VT-x (`VMX`) |

The two GMKtec nodes are deliberately excluded — roughly 7.5Gi of headroom
each isn't enough for either VM.

**Which path applies to you:**

- **Both target nodes, today:** the pre-flight below has already been run
  against `beelink-ser8-1` and `minisforum-nab9-1` and everything KubeVirt
  needs is already present — modules loaded, `/dev/kvm` there, cgroup v2,
  AppArmor active. Read [step 1](#1-pre-flight-verification-gate--do-not-skip)
  to know what was checked, then skip straight to
  [Path A](#path-a--these-two-nodes-today) in step 2 — it's one label.
- **A fresh/rebuilt node, or a third node added later:** read step 1, run
  the gate for real, then follow [Path B](#path-b--a-fresh-or-rebuilt-node)
  in step 2 — the full sequence.

Conflating the two paths means draining/cycling a node — and briefly
shrinking etcd quorum — for zero reason on a node that already has
everything it needs.

### 1. Pre-flight verification (gate — do not skip)

Node Feature Discovery already reports `feature.node.kubernetes.io/cpu-cpuid.SVM=true`
on `beelink-ser8-1` and `feature.node.kubernetes.io/cpu-cpuid.VMX=true` on
`minisforum-nab9-1` (confirmed directly against the live `Node` objects while
writing this runbook). That's a CPUID read — it proves the CPU *can* do
hardware virtualization, not that the kernel module is loaded and `/dev/kvm`
exists.

**If `/dev/kvm` cannot be made to exist on both nodes, stop — do not proceed
with the KubeVirt plan.** The fallback is KubeVirt's `useEmulation: true`
(pure QEMU, no hardware acceleration), which works but is roughly an order of
magnitude slower and is documented upstream as a dev/test-only setting, not
something to run either of these VMs on long-term.

Run this on each target node (SSH in, no changes made — safe to run any
time). It **exits non-zero on any FAIL** so it can gate a wrapper script, not
just print a warning that scrolls past:

```bash
#!/usr/bin/env bash
# preflight-kubevirt-node.sh -- read-only; makes no changes. Exits non-zero
# if any gating check fails.
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

# Informational only -- never gates. See "IOMMU" note below.
info() {
  local desc="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "INFO  $desc: yes"
  else
    echo "INFO  $desc: no"
  fi
}

check "/dev/kvm exists"             "[ -e /dev/kvm ]"
check "kvm module loaded"           "lsmod | awk '{print \$1}' | grep -qx kvm"
check "kvm_intel or kvm_amd loaded" "lsmod | awk '{print \$1}' | grep -qxE 'kvm_intel|kvm_amd'"
# /dev/net/tun and /dev/vhost-net are checked by existence, not lsmod: on a
# stock Ubuntu kernel both modules are wired to autoload the first time
# something opens the device node (a udev "devname" alias), so the node can
# be present and fully working before either module ever shows up in lsmod.
# Checking lsmod here produces a false FAIL on a node that is actually fine
# -- see "Why no kernel-module persistence step" below.
check "/dev/vhost-net device node exists" "[ -e /dev/vhost-net ]"
check "/dev/net/tun device node exists"   "[ -e /dev/net/tun ]"
check "cgroup v2 unified hierarchy" "[ \"\$(stat -fc %T /sys/fs/cgroup)\" = cgroup2fs ]"
check "AppArmor active"             "systemctl is-active --quiet apparmor"
# IOMMU isn't required by anything in this runbook's plan, but it's what
# makes PCI/GPU passthrough a real future option on a node -- recorded here
# so a future reader doesn't have to re-derive it from virt-host-validate.
info "IOMMU enabled by kernel"      "[ -d /sys/kernel/iommu_groups ] && [ -n \"\$(ls -A /sys/kernel/iommu_groups 2>/dev/null)\" ]"

echo
if $pass; then
  echo "All gating checks passed."
  exit 0
else
  echo "One or more gating checks FAILED -- do not schedule KubeVirt VMs on" \
       "this node until every check above passes."
  exit 1
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

Running this against both target nodes produced an all-PASS result with
exactly two `WARN`s: a missing `/dev/cpu/0/msr` and "unknown if this
platform has Secure Guest support". **Neither is part of the gate script
above, and neither should become one:**

- `/dev/cpu/0/msr` is raw Model-Specific-Register access, used by
  CPU-introspection tooling (`turbostat`, `msr-tools`, some CPU-model
  detection paths) — it has nothing to do with the KVM/QEMU virtualization
  datapath, and KubeVirt's `virt-handler`/`virt-launcher` never touch it.
  It's generic libvirt-host advice, not a KubeVirt requirement.
- The Secure Guest warning is about confidential-computing support (AMD
  SEV/SEV-ES/SEV-SNP, Intel TDX) — irrelevant unless a VM here is ever
  configured to actually request a confidential-computing launch, which
  neither the dockerd VM nor the Talos sandbox does.

Gating on either would be a false positive: an operator would see a halted
gate for a condition that doesn't affect anything in this plan, learn to
distrust the gate, and start skipping it — the opposite of the goal. Expect
both `WARN`s on this hardware and don't chase them.

If `/dev/kvm` is missing, check first whether virtualization is disabled in
firmware (BIOS/UEFI "SVM Mode" / "Intel Virtualization Technology") before
assuming a kernel-side problem — that's the most common reason a CPU that
reports `SVM`/`VMX` still has no `/dev/kvm`.

### 2. The change

#### Path A — these two nodes, today

Both nodes already pass every gating check — nothing about `/dev/kvm`, the
kernel modules, cgroups, or AppArmor needs to change (see "Why no
kernel-module persistence step" below for why that's true and expected to
stay true across reboots). The only thing missing is the declarative label,
and — per the k3s caveat below — that needs both the git-visible drop-in
*and* one imperative `kubectl label`, applied directly, with no cordon and no
`systemctl` restart:

```bash
# 1. Drop the declarative k3s config in place (see the block below for
#    contents). This alone does NOT label an already-registered node -- see
#    the caveat -- but it's what makes the label git-visible desired-state
#    and what applies automatically on this node's next real registration
#    (reinstall, disaster recovery). Plain file write: no k3s restart
#    needed, no live effect until some *future* registration event.
scp 90-kubevirt.yaml beelink-ser8-1:/tmp/
ssh beelink-ser8-1 'sudo install -o root -g root -m 0644 -D /tmp/90-kubevirt.yaml /etc/rancher/k3s/config.yaml.d/90-kubevirt.yaml'

# 2. Apply the label directly -- this is the part that actually takes effect
#    today, since k3s only reads config.yaml.d node-label(+) at registration
#    time and both nodes are already registered.
kubectl label node beelink-ser8-1 homelab-ops.internal/virtualization=enabled

# Repeat both steps for minisforum-nab9-1.
```

No `kubectl cordon`, no `systemctl stop/start k3s` — this path makes no
change that a running k3s process, or anything scheduled on the node, would
ever observe.

#### Path B — a fresh or rebuilt node

Applies to a node reinstalled from scratch, a disaster-recovery rejoin, or a
third node added to the KubeVirt pool later.

1. **If the node is currently live and about to be taken down for
   rebuild**, cordon it first — ordinary node-maintenance hygiene, not
   specific to this runbook. Reuse the pattern this cluster's
   `system-upgrade-controller` already applies for k3s upgrades: cordon,
   *no* drain, because the operation completes fast enough that pods don't
   migrate. Don't introduce a second, slower convention for the same kind of
   operation:

   ```bash
   kubectl cordon <node>
   ```

2. Bake `/etc/rancher/k3s/config.yaml.d/90-kubevirt.yaml` into however the
   node gets provisioned (image, cloud-init, Ansible, whatever replaces the
   retired Packer/Ansible pipeline) so it's in place **before** `k3s` first
   starts:

   ```yaml
   # 90-kubevirt.yaml
   # A '+' suffix is required on node-label (verified against the current
   # k3s config docs: https://docs.k3s.io/installation/configuration).
   # config.yaml.d files merge by key, last-file-wins, UNLESS the key carries
   # a '+' suffix, in which case it appends instead of replacing -- and every
   # file that sets this key from then on must also use '+' or it silently
   # reverts to overwrite. A bare `node-label:` here would mean any later
   # drop-in that also sets `node-label:` (bare) erases this label with no
   # error -- precisely the disaster-recovery re-registration scenario this
   # file exists to serve.
   node-label+:
     - "homelab-ops.internal/virtualization=enabled"
   ```

3. Let the node join normally. Registration applies the label automatically
   — no manual `kubectl label` step needed on a genuine fresh join, unlike
   Path A.
4. Once the node reports `Ready`, run the pre-flight script from step 1
   against it before trusting it. The "no kernel-module step" reasoning
   below was verified against stock Ubuntu kernel packaging on these two
   specific nodes — confirm it still holds on whatever base image actually
   provisioned this one before assuming it for granted.
5. `kubectl uncordon <node>` if step 1 applied.

**Caveat verified against current k3s docs, and it's why Path A and Path B
differ:** k3s's `--node-label` (and therefore the `config.yaml.d`
`node-label`/`node-label+` drop-in above) "only add[s] labels ... at
registration time, so they can only be set when the node is first joined to
the cluster" — [k3s Advanced Options docs](https://docs.k3s.io/advanced).
Both target nodes are already registered, so the drop-in alone will
**not** retroactively add the label to them (Path A's reason for the extra
`kubectl label` step); a genuinely fresh or rejoining node picks it up
automatically at registration (Path B's reason it doesn't need that step).
Keep the drop-in and the `kubectl label` in sync on already-registered nodes
— if one is ever removed, remove the other too.

#### Why no kernel-module persistence step

An earlier draft of this runbook also installed
`/etc/modules-load.d/kubevirt.conf` listing `kvm`, `kvm_intel`, `kvm_amd`,
`vhost_net`, `tun`. Running the pre-flight against the real nodes showed
that's unnecessary — and, for two of the five, actively worse than doing
nothing:

- **`kvm_intel`/`kvm_amd` already autoload without any config.** Both
  modules ship a `MODULE_DEVICE_TABLE(x86cpu, ...)` CPU-feature match (VMX /
  SVM), which the kernel uses to auto-probe and load the correct one at boot
  via udev's CPU coldplug — no `modprobe`, no `modules-load.d` entry. That's
  exactly what the pre-flight found: `kvm_intel`+`kvm` loaded on the Intel
  node, `kvm_amd`+`kvm`(+`ccp`) on the AMD node, with
  `/etc/modules-load.d/*.conf` containing nothing but the stock "this file
  is obsolete" comment. **Listing both vendor modules in a static
  `modules-load.d` file is a regression, not a no-op:** no CPU has both VMX
  and SVM, so exactly one of the two will fail to load on every boot,
  forever, on both nodes — turning `systemd-modules-load.service` into a
  unit that's permanently "failed" for a condition that isn't a problem.
  That's the same false-positive-gate failure mode as the `msr`/Secure-Guest
  `WARN`s above, just aimed at `systemctl --failed` instead of a human eye.
- **`tun`/`vhost_net` also autoload, on first use.** Neither showed up in
  `lsmod` on either node, yet `virt-host-validate` found `/dev/net/tun` and
  `/dev/vhost-net` both present and passing. That combination — device node
  present, module not (yet) resident — is the signature of the kernel's
  "devname" module-alias mechanism: udev pre-creates the device node from
  the module's alias metadata at boot, and the kernel `request_module()`s
  the real module the first time something opens that node (which is
  exactly what `virt-launcher` does when a VM starts). No persisted config
  is needed for either the node or the eventual module load to happen.

Net effect: the modules are already handled by the OS, on this specific
hardware and kernel packaging, without this runbook's help. Declaring them
in `modules-load.d` doesn't make anything more available — it implies a
dependency that isn't real, and for the vendor-specific pair it actively
breaks a systemd unit on every boot. That's why Path A above changes nothing
about kernel modules, and why Path B doesn't either.

### 3. Post-change verification

```bash
# Label present on both nodes:
kubectl get nodes -l homelab-ops.internal/virtualization=enabled
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

Both independent primitives from this runbook map to one field each in
Talos's machine config, so this becomes a translation, not a rediscovery,
when the migration happens:

```yaml
machine:
  kernel:
    modules:
      - name: kvm
      - name: kvm_intel # benign failure on the AMD node, same caveat as k3s
      - name: kvm_amd    # benign failure on the Intel node, same caveat as k3s
      - name: vhost_net
      - name: tun
  nodeLabels:
    homelab-ops.internal/virtualization: enabled
```

**Unlike the Ubuntu nodes above, don't assume this module list is
unnecessary on Talos without checking.** The "no kernel-module step"
finding for `beelink-ser8-1`/`minisforum-nab9-1` rests on stock Ubuntu
kernel packaging and udev's coldplug/devname-alias autoload machinery —
Talos is a minimal, immutable OS with its own kernel build and no udev, so
neither autoload path is guaranteed to carry over. Verify with the
equivalent of the pre-flight script (or the Talos node's own `lsmod`) on
first use before dropping this list; don't port the Ubuntu finding by
assumption. Two more things worth knowing before relying on this in the
actual migration (also verify on first use — sources disagree on the
second one):

- `machine.nodeLabels` is applied live by Talos's own controller — no
  restart needed — but is still subject to the same
  `NodeRestriction` admission-controller boundary Kubernetes applies
  everywhere: a kubelet cannot self-assign labels under a handful of
  reserved prefixes (`kubernetes.io/`, `k8s.io/`, etc.). `homelab-ops.internal/*`
  isn't one of them, so this isn't a blocker here, but it's the reason a
  reserved-prefix label would need a different mechanism.
- Whether `machine.kernel.modules` changes apply immediately or require a
  reboot was not conclusively confirmed from documentation alone; `talosctl
  apply-config` reports whether a reboot is required for a given change, so
  treat that reported mode as authoritative on first use rather than
  assuming either way.

### 5. Rollback

Path A/B above never touch kernel modules, so there's nothing module-related
to unwind — only the label and its declarative drop-in:

```bash
# On the node:
sudo rm -f /etc/rancher/k3s/config.yaml.d/90-kubevirt.yaml

# Remove the imperative label (Path A nodes only -- a Path B node that
# picked the label up at registration only carries it via the drop-in
# above, so removing the file is enough there):
kubectl label node beelink-ser8-1 homelab-ops.internal/virtualization-
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
