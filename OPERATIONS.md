# Operations

This document holds manual, occasional runbooks: procedures that happen
*outside* Flux — against a physical node, an external registry, or as an
on-demand check of a reconciled resource's actual runtime behavior — because
they can't be expressed as, or fully verified by, a reconciled manifest
alone. Everything else in this repo is declarative and covered by
[DESIGN.md](./DESIGN.md) instead — start here only for the operations listed
below.

The host-level runbooks below used to be automated by a Packer/Ansible
pipeline that is now unmaintained and being archived; until the planned k3s →
Talos migration removes the need for host-level node prep entirely, they're
accepted as manual, occasional, human-run procedures instead.

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

### 6. Sequencing hazard: label before `infra-virtualization-core` reconciles

**As of this writing, no node carries `homelab-ops.internal/virtualization=enabled`
yet** — step 2 above is written but not yet applied. If the
`infra-virtualization-core` `Kustomization` reconciles first, `virt-handler` has
nowhere to schedule and the `KubeVirt` custom resource sits un-`Deployed`
indefinitely. From the outside that looks identical to a real failure
(virt-operator crash-looping, a wrong monitor `ServiceAccount`, a bad
`GitRepository` tag) but is just this precondition — check
`kubectl get nodes -l homelab-ops.internal/virtualization=enabled` before
assuming anything else is wrong. Run [Path A](#path-a--these-two-nodes-today)
first, then let `infra-virtualization-core` reconcile.

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

### The `qemu-img` gap in this environment, and how it's actually solved

`qemu-img` (needed to convert the Factory's raw image to qcow2) is not
available anywhere in a Coder workspace, and can't be made available: these
workspaces run as unprivileged Kubernetes pods — no root, no `sudo`, no `apt`
— and neither `mise registry` nor the `aqua` registry carry it or an
equivalent (checked directly, not assumed). Three ways around that were
considered and rejected before landing on the one below:

- **An unofficial static `qemu-img` binary.** This is the same trade already
  rejected for the disk image itself — the `docker.io/containercraft/talos`
  community image, a single maintainer with no ceremony around it. Trusting a
  random prebuilt binary from outside the distro/project for a tool that
  writes boot disks is the identical risk in a different shape, for no better
  reason than convenience.
- **A pure-Python qcow2 encoder.** A previous attempt at this exact problem
  wrote a pure-Python qcow2 *reader* and got far enough to see how much of
  the format that touches — L1/L2 tables, refcount tables, cluster
  allocation. That's tractable for reading; it is nowhere near enough
  confidence to trust a hand-rolled *writer* with no independent tool
  available to validate its own output. A subtly wrong qcow2 file doesn't
  fail loudly at build time — it fails when Talos tries to boot from it,
  which is exactly the failure mode this runbook exists to avoid.
- **Run `qemu-img` in a throwaway container on the Docker VM**, the obvious
  option since that VM already runs a real `dockerd`. This looked right and
  doesn't work: `qemu-img convert`, and even a trivial `qemu-img create -f
  raw test.raw 1G`, hang indefinitely inside an `alpine:3.20` container on
  that VM — confirmed via `docker top`, which showed 0:00 accumulated CPU
  time across several minutes of wall-clock, not just a slow command. The
  *identical* command against the *identical* disk, run directly on the VM's
  host OS instead of inside a container, completed in under a second. The
  exact mechanism wasn't chased down further, but Docker's default seccomp
  profile blocking a syscall `qemu-img` wants — `io_uring_setup` is the
  leading suspect, a documented target of that default denylist — is the
  most likely explanation. Whatever it is, the fix that was actually proven
  by running it is simpler than working around the container: don't use a
  container.

That leaves the option this runbook now uses: **`qemu-img` runs on the
Docker VM's host OS**, not inside anything — `apt-get install qemu-utils`
(a few MB; the VM already has passwordless `sudo`), reached over SSH
directly from a Coder workspace at
`docker@docker-vm.sandbox-docker.svc.cluster.local:22`. That address
resolves and connects with no `kubectl port-forward` needed, because Coder
workspace pods are explicitly allow-listed for ingress on that port (see
`allow-coder-ingress` in `sandbox-docker/network-policy.yaml`); use `kubectl
port-forward` per "Runbook: reach the sandbox VMs" above instead if running
this from anywhere other than a Coder workspace. Leave `qemu-utils` installed
afterward rather than removing it each run — Talos version bumps are a
recurring manual procedure (see below), so the next run needs it again
regardless.

This does mean the pipeline now spans two machines with different network
reachability, which is a real cost, not a free abstraction: the Coder
workspace can reach the public internet (Image Factory, crane's own tooling)
but not Harbor (no Tailscale in this environment); the Docker VM can also
reach the public internet, but its NetworkPolicy denies all RFC1918 egress —
exactly what reaching Harbor on the LAN would require. No single machine in
this environment can download, convert, build, *and* push in one hop. The
script below is split into phases along exactly that boundary, each one
stating where it has to run and why.

### Is `qcow2` even worth the trouble? Measured, not assumed

The ~100–200MB qcow2 vs ~1GB raw estimate this runbook originally carried
was never actually measured. Running the real pipeline end to end (Talos
v1.13.7, the empty/no-extensions schematic) gives real numbers instead:

| | size |
| --- | --- |
| Factory `nocloud-amd64.raw.xz` download | 204 MiB |
| Decompressed raw disk — logical size / actual allocated blocks | 4.15 GiB / ~205 MiB (already >95% sparse) |
| `qemu-img convert -O qcow2` output | 206 MiB (0.26s) |
| containerDisk image built from that **qcow2** (gzip layer — what actually gets pushed) | 204 MiB |
| containerDisk image built from the **raw** disk instead, same source (gzip layer) | 209 MiB |

The push-size argument barely holds up: gzip crushes the raw file's zeros
almost as effectively as qcow2's own sparse-cluster encoding does, because
the disk is already mostly empty before either format touches it. **The
argument that does hold is what happens after the pull, not before the
push.** Extracting the raw layer with a plain `tar -xzf` — i.e. what a
non-hole-aware unpacker does, and there's no guarantee this cluster's
containerd is hole-aware on this path — materializes the full 4.15GiB on
disk: verified by extracting it and checking the result's actual allocated
blocks (`stat`), which matched its apparent size, not the ~205MiB the source
had. A qcow2 file's compactness, in contrast, is encoded in its own bytes,
not in filesystem holes, so it survives any copy/tar/gzip round-trip
unchanged — verified by hashing the qcow2 before packaging it and again
after a full tar→gzip→crane→un-tar→un-gzip round trip: byte-identical both
times. This cluster has already hit a containerDisk/`emptyDir`-sizing
surprise once — see the exclusion comment in
`policies/best-practices/add-emptydir-sizelimit.yaml` for the `docker-vm`
eviction loop it caused — so a raw containerDisk that might silently balloon
to ~20x its nominal size on a node's local disk isn't a bet worth taking
just to skip a two-line `apt-get install`. qcow2 stays.

### The build script

Takes the Talos version, schematic ID, and Docker VM SSH target as required
inputs — no defaults, so this never silently builds a stale version or
guesses at reachability. Registry host is a required variable too, per this
repo's convention of never hardcoding a domain. The script covers Phases
1–3 (download, convert, build) end to end and stops with a built local
tarball plus the exact command to run next — it deliberately does not push,
since Phase 4 needs Harbor credentials and Tailscale connectivity that a
Coder workspace in this environment doesn't have (see below).

`build-talos-containerdisk.sh`:

```bash
#!/usr/bin/env bash
# build-talos-containerdisk.sh -- downloads a Talos Image Factory boot asset,
# converts it to qcow2 on the sandbox-docker VM's host OS (see OPERATIONS.md
# for why it has to run there, and not locally or in a container), and wraps
# it as a KubeVirt containerDisk OCI image written to a local tarball. Does
# NOT push -- prints the crane push command to run by hand instead. Non-
# interactive; safe to re-run (each run uses its own temp dir).
set -euo pipefail

usage() {
  cat <<EOF
Usage: TALOS_VERSION=v1.13.7 SCHEMATIC_ID=<id> REGISTRY=harbor.example.com \\
       DOCKER_VM_SSH=docker@docker-vm.sandbox-docker.svc.cluster.local \\
       [DOCKER_VM_SSH_KEY=~/.ssh/sandbox_docker_vm] \\
       [IMAGE_PATH=talos/containerdisk] \\
       $0
EOF
  exit 1
}

: "${TALOS_VERSION:?TALOS_VERSION is required, e.g. v1.13.7}"
: "${SCHEMATIC_ID:?SCHEMATIC_ID is required -- see OPERATIONS.md for how to get one}"
: "${REGISTRY:?REGISTRY is required, e.g. harbor.example.com (never hardcode this)}"
: "${DOCKER_VM_SSH:?DOCKER_VM_SSH is required, e.g. docker@docker-vm.sandbox-docker.svc.cluster.local -- qemu-img has to run directly on that VM, see OPERATIONS.md}"
DOCKER_VM_SSH_KEY="${DOCKER_VM_SSH_KEY:-$HOME/.ssh/sandbox_docker_vm}"
IMAGE_PATH="${IMAGE_PATH:-talos/containerdisk}"
IMAGE_REF="${REGISTRY}/${IMAGE_PATH}:${TALOS_VERSION}-${SCHEMATIC_ID:0:12}"

command -v crane >/dev/null || { echo "crane not found -- mise use -g crane" >&2; exit 1; }
command -v xz >/dev/null || { echo "xz not found -- apt install xz-utils" >&2; exit 1; }

ssh_vm() { ssh -i "${DOCKER_VM_SSH_KEY}" -o StrictHostKeyChecking=accept-new "${DOCKER_VM_SSH}" "$@"; }

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

# --- Phase 1: download + decompress (this host; needs only curl/xz) ---
asset_url="https://factory.talos.dev/image/${SCHEMATIC_ID}/${TALOS_VERSION}/nocloud-amd64.raw.xz"
echo "Downloading ${asset_url}"
curl -fL -o "${workdir}/talos.raw.xz" "${asset_url}"

# --- Phase 2: convert to qcow2 on the Docker VM's host OS ---
echo "Ensuring qemu-utils is installed on ${DOCKER_VM_SSH}"
ssh_vm 'command -v qemu-img >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq qemu-utils'

remote_dir="$(ssh_vm mktemp -d)"
echo "Streaming compressed image to ${DOCKER_VM_SSH}:${remote_dir}"
ssh_vm "cat > ${remote_dir}/talos.raw.xz" < "${workdir}/talos.raw.xz"
ssh_vm "cd ${remote_dir} && xz -d talos.raw.xz && qemu-img convert -O qcow2 talos.raw talos.qcow2 && rm -f talos.raw"
echo "Retrieving qcow2"
ssh_vm "cat ${remote_dir}/talos.qcow2" > "${workdir}/talos.qcow2"
ssh_vm "rm -rf ${remote_dir}"
du -h "${workdir}/talos.qcow2"

# --- Phase 3: build the containerDisk image locally (this host; needs only crane) ---
echo "Building containerDisk layer (disk/talos.qcow2, owned 107:107)"
mkdir -p "${workdir}/layer/disk"
cp "${workdir}/talos.qcow2" "${workdir}/layer/disk/talos.qcow2"
tar --owner=107 --group=107 -C "${workdir}/layer" -cf "${workdir}/layer.tar" disk

echo "Building OCI image (local tarball, no push)"
crane append -f "${workdir}/layer.tar" --oci-empty-base -t "${IMAGE_REF}" \
  -o ./talos-containerdisk.tar

echo
echo "Built: ./talos-containerdisk.tar"
echo
echo "--- Phase 4 (by hand, from a machine with Tailscale reachability to"
echo "    Harbor and HARBOR_USER/HARBOR_PASSWORD -- neither exists in a"
echo "    Coder workspace in this environment) ---"
echo "crane auth login ${REGISTRY} -u \$HARBOR_USER --password-stdin <<<\"\$HARBOR_PASSWORD\""
echo "crane push ./talos-containerdisk.tar ${IMAGE_REF}"
```

Notes on specific lines:

- `qemu-img convert -O qcow2` produces a sparse file by default (no
  preallocation flag needed) — see "Is qcow2 even worth the trouble?" above
  for measured sizes.
- `crane append --oci-empty-base` builds a new image from an empty/scratch
  base per its own docs — exactly what `FROM scratch` means in Dockerfile
  terms. `-o` writes that image to a local tarball instead of pushing, which
  is what makes Phase 3 possible without any registry reachability at all —
  verified by inspecting the tarball's `manifest.json` and layer contents
  directly (single layer, `disk/talos.qcow2`, owned `107:107`, no base
  image) rather than trusting it.
- The Harbor **project** in `IMAGE_PATH` (e.g. `talos`) must already exist —
  Harbor doesn't auto-create projects on push the way some registries do.
- `crane push` accepts a local docker-style tarball directly as its `PATH`
  argument (`crane push PATH IMAGE`) — Phase 4 needs nothing from Phases
  1–3 except the tarball itself, so it can run from any machine that has
  `crane`, Harbor reachability, and credentials, not necessarily the one
  that built the image.

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
        image: harbor.example.com/talos/containerdisk:v1.13.7-376567988ad3
```

### Talos version bumps are now a manual re-run

There's no CI wired up for this — bumping Talos means re-running the script
above with the new `TALOS_VERSION` (same `SCHEMATIC_ID` unless the
customization also changed), running the Phase 4 push by hand, and updating
whichever `VirtualMachine` manifest references the resulting tag. Renovate
*can* be configured to watch `siderolabs/talos` GitHub releases and open an
informational PR the same way it's already annotated for k3s
(`# renovate: datasource=github-releases depName=k3s-io/k3s` in
`clusters/homelab/cluster/kubernetes-version/server-upgrade.yaml`) — but
nothing actually builds or pushes the image automatically, and setting up
that watch is a separate follow-up, not done as part of this runbook. Accept
this as a standing, real cost of building from the official artifact instead
of relying on someone else's automated image.

## Runbook: verify the sandbox NetworkPolicy falsifiability probes before/after rollout

**Why:** `clusters/homelab/services/sandbox-docker/` and
`clusters/homelab/services/sandbox-talos/` each ship a
`netpol-falsifiability-probe` Deployment (see each namespace's
`deployment-netpol-falsifiability-probe.yaml`) that asserts the namespace's
NetworkPolicies are actually being enforced, not just present — it probes
targets that should now be unreachable (the prod kube-apiserver, kubelet,
etcd, the UniFi gateway, the in-cluster `kubernetes` Service, and, from
`sandbox-talos`, the `sandbox-docker` namespace's SSH Service) alongside two
that must stay reachable (`api.github.com` and DNS resolution via CoreDNS),
on a recurring loop (`PROBE_INTERVAL_SECONDS`, 300s by default).

A probe that only ever runs *after* the policy exists proves nothing by
itself: if every target were unreachable for some unrelated reason (a
misconfigured CNI, a routing problem, the probe pod itself broken), it would
report success against a namespace with no working isolation at all. The
run documented here is the other half — a baseline taken *before* the
NetworkPolicies exist, where every probed target must be genuinely
reachable. Only a target that flips from "reachable before" to "denied
after" is evidence the policy — not something else — is what changed the
outcome.

This Deployment used to be a CronJob/Job. It isn't anymore, because a
short-lived Job pod can complete *before* k3s's kube-router-derived
NetworkPolicy controller has programmed that pod's own firewall dispatch
rule — an unpoliced pod passes every deny check for free, which is exactly
what happened on this probe's first real run (see the CAVEAT in each
namespace's `network-policy.yaml`, and the "Known limitation" section
below). The Deployment's container runs a warm-up gate before every probe
cycle starts, and refuses to report anything until it has independently
confirmed *this* pod is actually policed — see the comments in
`deployment-netpol-falsifiability-probe.yaml` for the full mechanism. That
gate is per-container-start, not per-cycle, which is also why switching to
a long-lived pod removes the race rather than re-running it every schedule
tick: once warmed up, the same pod stays policed for the rest of its life.

### 1. Baseline: before the NetworkPolicy exists

Apply the namespace and Deployment, but not `network-policy.yaml`, e.g. by
temporarily commenting the `network-policy.yaml` entry out of that
namespace's `kustomization.yaml` `resources:` list before this reconciles,
or by deleting the live `NetworkPolicy` objects in the namespace after a
normal reconcile (Flux will reassert them on its next reconcile, so this
window is short — have the second command below ready before deleting
them).

**With no NetworkPolicy in place, the Deployment's own warm-up gate can
never succeed** — nothing is denied yet, so `UNIFI_GATEWAY:443` never flips
to blocked, and the pod will loop `WARMUP-TIMEOUT` → crash → restart
indefinitely (`kubectl get pods -n sandbox-docker` will show a rising
restart count / `CrashLoopBackOff`). **That crash-loop is the expected,
correct baseline signature now** — it's evidence this pod itself isn't
being denied anything, i.e. a genuinely open network — don't mistake it for
a broken probe and don't wait for it to "pass".

To validate that the individual deny-targets are actually live (the
concern this baseline step exists for — catching a bad IP/port before it
ships, the same class of bug as the Longhorn `:9500` dead-port false pass
documented in `deployment-netpol-falsifiability-probe.yaml`), run a one-off
diagnostic pod with the warm-up gate skipped, using the same image:

```bash
kubectl run netpol-baseline --rm -it --restart=Never -n sandbox-docker \
  --image=busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616 \
  -- sh -c '
    for t in 192.168.8.65:6443 192.168.8.67:6443 192.168.8.68:6443 192.168.8.69:6443 \
             192.168.8.65:10250 192.168.8.67:10250 192.168.8.68:10250 192.168.8.69:10250 \
             192.168.8.65:2379 192.168.8.67:2379 192.168.8.68:2379 192.168.8.69:2379 \
             192.168.8.1:443 10.43.0.1:443 api.github.com:443; do
      host=${t%:*}; port=${t#*:}
      nc -zv -w3 "$host" "$port" 2>&1
    done
    nslookup api.github.com'
```

(swap `sandbox-docker`/its node-IP-only targets for `sandbox-talos` — same
target list plus `docker-vm.sandbox-docker.svc.cluster.local:22`, which is
expected to fail DNS resolution until Phase 4 creates that Service — to
baseline the other namespace).

**Expected result: every target above connects (or resolves) successfully.**
A connection failure here means the probe target itself is wrong (bad IP,
bad port, a target that was never reachable in the first place, i.e. not
actually exposed on a node IP) — fix
`deployment-netpol-falsifiability-probe.yaml`, not the policy, and re-run
this step before moving on.

### 2. Apply the NetworkPolicy

Restore `network-policy.yaml` to the namespace's `kustomization.yaml` (or
let Flux's next reconcile reassert the deleted objects — `config-services-
sandbox-docker`/`config-services-sandbox-talos` run on a 1-minute interval
specifically so this doesn't require a manual nudge).

### 3. Confirm the flip

```bash
kubectl logs -f -n sandbox-docker deploy/netpol-falsifiability-probe
```

If the pod was already crash-looping from step 1, Kubernetes' restart
backoff may delay the next attempt by up to a few minutes; force an
immediate retry against the now-present policy rather than waiting it out:

```bash
kubectl delete pod -n sandbox-docker -l app.kubernetes.io/name=netpol-falsifiability-probe
```

**Expected result:** `WARMUP: waiting for this pod's egress chain to be
programmed...` followed shortly by `WARMUP-OK: denial observed after Ns`,
then a full cycle of `BLOCKED (ok): ...` lines for every deny target,
`REACHABLE (ok): ...` for `api.github.com`/DNS, and a closing
`SUMMARY ... verdict=CLEAN` line. Any target still logging
`REACHABLE (violation)` here means the NetworkPolicy isn't doing what it
looks like it does — a label selector matching zero pods, a typo'd
namespace selector, or the CNI's policy controller not enforcing at all are
the usual causes; this is exactly the "policy that silently stopped
enforcing" failure mode the recurring probe exists to keep catching
automatically after this one-time baseline.

### 4. Clean up

No one-shot Jobs to delete — the ad hoc `netpol-baseline` pod in step 1
already removes itself (`--rm`). If the Deployment crash-looped during step
1, its restart count is cosmetic once step 3 confirms `SUMMARY ...
verdict=CLEAN` cycles are landing; nothing further to clean up.

### Querying the probe's ongoing state

The recurring probe keeps running afterward on its own
`PROBE_INTERVAL_SECONDS` loop (300s by default). There is no `kube_job_failed`
metric anymore (no Job object) — instead, query Loki directly for the verdict
lines (confirmed working against the live `loki` datasource; the probe's pod
logs carry `namespace`/`pod`/`node_name`/`container` labels via promtail):

```logql
# Latest cycle summary per namespace:
{namespace=~"sandbox-docker|sandbox-talos"} |= "SUMMARY"

# Any violation, ever, in the lookback window:
{namespace=~"sandbox-docker|sandbox-talos"} |= "(violation)"

# The pod itself failing to become policed at startup (crash-loop signature):
{namespace=~"sandbox-docker|sandbox-talos"} |= "WARMUP-TIMEOUT"

# The pod losing enforcement mid-life, after having passed the warm-up gate
# (see "Mid-life re-verification" below) -- distinct from WARMUP-TIMEOUT
# because it's a materially more alarming event:
{namespace=~"sandbox-docker|sandbox-talos"} |= "MIDLIFE-ENFORCEMENT-LAPSE"
```

An absent recent `SUMMARY` line is itself a signal worth treating as
suspect — it means the probe stopped producing cycles, not that it found
nothing wrong; a crash-looping pod (`WARMUP-TIMEOUT`, `MIDLIFE-ENFORCEMENT-
LAPSE`, or a killed container) also shows up as
`kube_pod_container_status_restarts_total{namespace=~
"sandbox-docker|sandbox-talos"}` climbing in Prometheus, which is a
reasonable proxy for what `kube_job_failed` used to surface directly. No
AlertManager route is wired to any of this, deliberately: this is a homelab
with no triage layer for alerts yet, so a failure is meant to be found by
querying, not by paging — wiring an alert route is a follow-up for once
that layer exists, not part of this change.

The container also carries a `livenessProbe` that restarts it if its internal
heartbeat file goes stale (`HEARTBEAT_MAX_AGE_SECONDS`, 420s by default) —
this catches the loop hanging mid-cycle on something with no timeout of its
own (concretely, the plain `nslookup` calls in the script). That probe's
result is **not** in this Loki stream: exec probe output goes to kubelet's
probe result, not the container's stdout, so a heartbeat-triggered restart is
visible via `kubectl describe pod` (`Warning  Unhealthy`) and the same
`kube_pod_container_status_restarts_total` climb, not by grepping logs.

### Mid-life re-verification

The warm-up gate (above) only proves a pod was policed **at container
start**. Every `MIDLIFE_REVERIFY_EVERY_N_CYCLES` cycles (12 by default, ~1
hour at `PROBE_INTERVAL_SECONDS=300s`), the probe loop re-runs that same
deny-check against `UNIFI_GATEWAY:443` on its own, outside the ordinary
per-cycle target list, specifically so a mid-life enforcement lapse (a
kube-router restart racing this pod, a full iptables resync gone wrong) is
distinguishable from an ordinary probe violation rather than just adding one
more line to that cycle's `violations` count. If that re-check finds the
target reachable, the container logs `MIDLIFE-ENFORCEMENT-LAPSE` and exits
non-zero rather than logging and continuing — restarting re-runs the warm-up
gate on the replacement pod, re-proving enforcement before the loop is
trusted again, at the cost of losing that pod's running history. The
alternative (log loudly, keep running) was rejected: continuing would mean
the probe keeps emitting `BLOCKED (ok)` results for every other target built
on a premise — "this pod is policed" — that this very check just disproved,
which is the same class of check-that-can't-fail bug the whole
CronJob→Deployment redesign exists to close.

## Known limitation: the per-pod NetworkPolicy enforcement window

**What it is:** k3s's bundled NetworkPolicy controller (a kube-router fork)
programs each pod's firewall chain (`KUBE-POD-FW-<hash>`) and its dispatch
rule in the `KUBE-ROUTER-FORWARD` chain *reactively*, in response to
watching the API server for new pods — not proactively, and not as part of
pod admission. Between a pod's creation and the moment that chain is
programmed, it has **no policy enforcement at all**: `KUBE-ROUTER-FORWARD`'s
per-pod dispatch is the only place traffic gets handed to a
`KUBE-POD-FW-*` chain, and a pod with no dispatch rule yet falls straight
through to the chain's default (`-P FORWARD ACCEPT`).

**Why it exists:** this is a property of the controller's design
(event-driven, not synchronous with pod creation), not a misconfiguration —
verified directly by re-testing against a pod that had been running long
enough for its rule to land: `nc -zv -w3 192.168.8.1 443` returned exit 1
(blocked) against a warmed-up `sandbox-docker` pod, while the identical
check against a pod that completed before its chain was programmed showed
every target reachable. That's also what
`deployment-netpol-falsifiability-probe.yaml`'s warm-up gate now checks for
directly, every time its container (re)starts, rather than assuming.

**What it affects and what it doesn't:**

- The sandbox VMs (dockerd, Talos) are long-lived — their `virt-launcher`
  pod is policed within seconds of creation and stays policed for the VM's
  entire lifetime. This window is irrelevant to them.
- Any short-lived pod created in `sandbox-docker` or `sandbox-talos` — most
  importantly, anything an agent with pod-create rights in `sandbox-talos`
  runs — can complete its entire lifecycle inside this window. A Job that
  opens an egress connection and exits within the first few seconds may
  never be policed at all. **This is a real gap in the isolation model, not
  a testing artifact:** the NetworkPolicies, though correct, are not a
  complete mediation guarantee against a short-lived process — only against
  anything that outlives the window.
- The mechanism (`KUBE-ROUTER-FORWARD` dispatch) is symmetric for ingress
  and egress, so the same gap exists for ingress in principle, though
  nothing outside the sandbox namespaces currently has a reason to dial a
  freshly-created pod inside this window in practice.

**How to verify a given pod is actually policed:** the dispatch rule lives
in the **legacy** iptables tables, invisible to `nft list ruleset` — k3s no
longer bundles an `iptables` binary at all (the host only has `nft`), so
checking requires a debug pod with `iptables-legacy` installed, run against
**the specific node the target pod landed on** (the rule is node-local, not
cluster-wide):

```bash
# Find which node the pod is on:
kubectl get pod -n sandbox-docker <pod-name> -o wide

# Debug pod on that node -- note "sysadmin" profile and chroot into the host:
kubectl debug node/<node-name> -it --profile=sysadmin --image=alpine -- chroot /host sh
apk add iptables-legacy
iptables-legacy -S KUBE-ROUTER-FORWARD | grep <pod-ip>
```

A dispatch rule referencing the pod's IP means it's policed; no match means
it isn't (yet, or ever, if the pod already exited). The pod's own
`KUBE-POD-FW-<hash>` chain (`iptables-legacy -S | grep KUBE-POD-FW`) ends
with `-j NFLOG --nflog-group 100` for anything it drops or rejects — dropped
traffic is already being logged at the node via NFLOG, a second,
independent signal that a denial actually happened, without relying on the
probe's own self-reported result.

**Standing mitigation:** the falsifiability probe's warm-up gate (above) is
the automated version of this same check, run against its own pod every
time it (re)starts. There is no equivalent gate today for pods an agent
creates directly in `sandbox-talos` — that would require something like a
validating admission policy or a delay before granting network access to
new pods, neither of which exists. Treat this window as a standing,
accepted property of the current isolation model, not a closed issue.

## Runbook: reach the sandbox VMs

**Why:** `virtctl ssh`/`virtctl port-forward` do not work against either sandbox VM,
deliberately — see the comment above `allow-coder-ingress` in
`sandbox-docker/network-policy.yaml` and above `allow-coder-and-mcp-ingress` in
`sandbox-talos/network-policy.yaml`. Both commands have `virt-api` (in the `kubevirt`
namespace) dial the VM's masquerade pod IP directly, which crosses into the sandbox
namespace — an ingress path that stays blocked on purpose, since nothing about running or
managing either VM depends on it, and admitting it would also admit `virt-api`/
`virt-operator`/`virt-controller`, not just the already-privileged `virt-handler`. This is
pre-explained here so it reads as expected behavior, not a bug to debug.

`kubectl port-forward` is the supported substitute — a completely different mechanism, not
subject to that NetworkPolicy: containerd's CRI implementation enters the *pod's own*
network namespace and dials `localhost`, so it never reaches the veth where policy is
enforced. Target the VM's `virt-launcher` pod directly by its standard KubeVirt label
(`kubevirt.io/domain=<vm-name>`) — substitute the actual VM name once the VM workload
manifests (a separate, not-yet-merged phase) exist:

```bash
# Talos API (talosctl, :50000) -- sandbox-talos namespace
kubectl port-forward -n sandbox-talos \
  "$(kubectl get pod -n sandbox-talos -l kubevirt.io/domain=<talos-vm-name> -o name)" \
  50000:50000
# then point talosctl at the forwarded port:
talosctl --talosconfig <path> config endpoint 127.0.0.1
talosctl --talosconfig <path> config node 127.0.0.1

# Sandbox Kubernetes API (:6443) -- sandbox-talos namespace
kubectl port-forward -n sandbox-talos \
  "$(kubectl get pod -n sandbox-talos -l kubevirt.io/domain=<talos-vm-name> -o name)" \
  6443:6443
# then point kubectl at it -- set `server: https://127.0.0.1:6443` in that sandbox
# cluster's own kubeconfig, not this cluster's.

# SSH to the Docker VM (:22) -- sandbox-docker namespace. cronjob-netpol-falsifiability-
# probe.yaml's DOCKER_SSH_SERVICE already anticipates this landing as a Service named
# docker-vm -- once Phase 4 creates it, `svc/docker-vm` replaces the pod lookup below.
kubectl port-forward -n sandbox-docker \
  "$(kubectl get pod -n sandbox-docker -l kubevirt.io/domain=<docker-vm-name> -o name)" \
  2222:22
ssh -p 2222 <user>@127.0.0.1
```

**Talos ships no SSH daemon at all** — it's API-only via `talosctl` on :50000 with mTLS —
so for that VM the SSH question doesn't arise regardless of NetworkPolicy; only the Docker
VM (a full Ubuntu install) has a real `sshd` to reach.
