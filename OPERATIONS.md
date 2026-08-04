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

# 3. Tag the node in Longhorn. Longhorn node tags live on the nodes.longhorn.io
#    CR and are NOT managed in git -- this is a second piece of out-of-band
#    node state that must stay in step with the label above.
#    sc-longhorn-local-non-replicated-ephemeral carries nodeSelector:
#    "virtualization", so Longhorn will only place a sandbox VM's disk replica
#    on a node carrying this tag. Note --type=merge REPLACES the whole tag
#    list; read spec.tags first if the node already has others.
kubectl -n longhorn-system patch nodes.longhorn.io beelink-ser8-1 \
  --type=merge -p '{"spec":{"tags":["virtualization"]}}'

# Repeat all three steps for minisforum-nab9-1.
```

No `kubectl cordon`, no `systemctl stop/start k3s` — this path makes no
change that a running k3s process, or anything scheduled on the node, would
ever observe.

**The label and the Longhorn tag are a pair, and the pairing is asymmetric.**
Tag only nodes that also carry the label — never the reverse. A node that is
labelled but untagged is harmless (the VM may run there; Longhorn just won't
put a disk there). A node that is tagged but *not* labelled lets Longhorn
place a VM's disk on a node the VM's own `nodeSelector` forbids it to run on,
which pins the resulting PV's `nodeAffinity` to an unusable node and leaves
the VM permanently unschedulable. See the reasoning block in
`clusters/homelab/storage/infra/sc/sc-longhorn-local-non-replicated-ephemeral.yaml`.

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
5. Tag the node in Longhorn. Unlike the k3s node label, this has no
   registration-time equivalent — Longhorn creates the `nodes.longhorn.io`
   CR itself with an empty `spec.tags`, so this step is manual on *both*
   paths, and it must happen before any sandbox VM PVC is created against
   `sc-longhorn-local-non-replicated-ephemeral`:

   ```bash
   kubectl -n longhorn-system patch nodes.longhorn.io <node> \
     --type=merge -p '{"spec":{"tags":["virtualization"]}}'
   ```

   Only do this on a node that also carries the
   `homelab-ops.internal/virtualization` label — see the asymmetry note at
   the end of Path A for what breaks otherwise.
6. `kubectl uncordon <node>` if step 1 applied.

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
— if one is ever removed, remove the other too, and remove the Longhorn node
tag first. Dropping the label while the tag remains is the one ordering that
recreates the unschedulable-VM deadlock described in Path A.

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

Moved to the experiments repo, right next to the VM it builds for and the VM it actually runs
on — it was VM-specific operational procedure (which artifact to build, which tool converts
it, why qcow2, where the build tooling lives), not node-level or cluster-wide, so it doesn't
belong in this file (see this file's own opening paragraph).

- **The runbook and its reasoning:**
  [`experiments/apps/sandbox-talos/OPERATIONS.md`](https://github.com/ppat/homelab-ops-kubernetes-experiments/blob/main/experiments/apps/sandbox-talos/OPERATIONS.md)
- **The runnable task itself** (`mise run build-containerdisk`), not a script in a fence:
  [`experiments/apps/sandbox-talos/mise.toml`](https://github.com/ppat/homelab-ops-kubernetes-experiments/blob/main/experiments/apps/sandbox-talos/mise.toml)
- **Where it actually runs, and why**, plus the mise base/per-use-case split and the per-build
  setup sequence:
  [`experiments/apps/docker-host/OPERATIONS.md`](https://github.com/ppat/homelab-ops-kubernetes-experiments/blob/main/experiments/apps/docker-host/OPERATIONS.md)

## Runbook: verify the sandbox NetworkPolicy falsifiability probes before/after rollout

**Why:** `clusters/homelab/services/sandbox-docker/` and
`clusters/homelab/services/sandbox-talos/` each ship a
`netpol-falsifiability-probe` Deployment (see each namespace's
`deployment-netpol-falsifiability-probe.yaml`) that asserts the namespace's
NetworkPolicies are actually being enforced, not just present — it probes
targets that should now be unreachable (the prod kube-apiserver, kubelet,
etcd, the UniFi gateway, the in-cluster `kubernetes` Service, and, from
`sandbox-talos`, the `sandbox-docker` namespace's SSH Service) alongside
targets that must stay reachable (`api.github.com`, DNS resolution via
CoreDNS, and, from `sandbox-talos`, its own Talos VM's Service on both
ports it serves — the same-namespace ingress path that went silently
missing until it denied `bootstrap-job.yaml`'s `talosctl bootstrap` call;
see `sandbox-talos/network-policy.yaml`'s `allow-same-namespace-ingress-
to-vm`), on a recurring loop (`PROBE_INTERVAL_SECONDS`, 300s by default).

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
  importantly, the bootstrap Job or either CronJob that runs in
  `sandbox-talos` — can complete its entire lifecycle inside this window. A
  Job that opens an egress connection and exits within the first few seconds
  may never be policed at all. **This is a real gap in the isolation model,
  not a testing artifact:** the NetworkPolicies, though correct, are not a
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
time it (re)starts. There is no equivalent gate today for the other
short-lived pods created directly in `sandbox-talos` (the bootstrap Job, the
CronJobs) — that would require something like a validating admission policy
or a delay before granting network access to new pods, neither of which
exists. Treat this window as a standing, accepted property of the current
isolation model, not a closed issue.

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
