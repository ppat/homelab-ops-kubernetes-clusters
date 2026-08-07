#!/bin/sh
#
# The netpol-falsifiability-probe loop for the sandbox-docker namespace. Run by
# ../deployment-netpol-falsifiability-probe.yaml, mounted at /etc/scripts from the
# ConfigMap ../kustomization.yaml generates from this directory. Every value read below
# (NODE_IPS, UNIFI_GATEWAY, LB_INGRESS_VIP_*, the TIMEOUT/WARMUP/INTERVAL knobs,
# HEARTBEAT_FILE) is an env var set on that Deployment, and each carries its own comment
# there explaining why that target is a deny assertion or a positive control -- this file
# says what is probed, the Deployment says why each address is what it is.
#
# ../network-policy.yaml is the control this exists to falsify; OPERATIONS.md covers how
# its output is read (Loki, no AlertManager wiring, by design).
set -u

# --- Warm-up: establish enforcement before trusting anything below ---
# This is the fix for the exact failure mode that fooled the original version of
# this probe: it *assumed* the policy was already active for this pod instead of
# establishing that it was. Poll a known-deny target until the connection is
# actually refused -- that transition is proof this pod's KUBE-POD-FW-* chain and
# its KUBE-ROUTER-FORWARD dispatch rule now exist. UNIFI_GATEWAY is used here (not
# one of the node-IP targets) because it was hand-verified live during the
# original investigation: reachable before the pod is policed, refused
# (icmp-port-unreachable) after. If it's never denied within
# WARMUP_TIMEOUT_SECONDS, this container exits loudly instead of silently running
# the suite against an unpoliced pod -- Kubernetes will restart it and this
# warm-up runs again from scratch on the new pod.
echo "WARMUP: waiting for this pod's egress chain to be programmed (deny-probing ${UNIFI_GATEWAY}:443)..."
warmup_elapsed=0
warmup_ok=0
while [ "$warmup_elapsed" -lt "$WARMUP_TIMEOUT_SECONDS" ]; do
  if ! nc -z -w 1 "$UNIFI_GATEWAY" 443 2>/dev/null; then
    echo "WARMUP-OK: denial observed after ${warmup_elapsed}s -- this pod is now policed, starting probe loop"
    warmup_ok=1
    break
  fi
  sleep "$WARMUP_POLL_INTERVAL_SECONDS"
  warmup_elapsed=$((warmup_elapsed + WARMUP_POLL_INTERVAL_SECONDS))
done

if [ "$warmup_ok" -ne 1 ]; then
  echo "WARMUP-TIMEOUT: ${UNIFI_GATEWAY}:443 was still reachable after ${WARMUP_TIMEOUT_SECONDS}s -- this pod's egress was never policed within the warm-up window. This is NOT evidence the NetworkPolicy is broken: it means enforcement could not be established for THIS pod before the deadline, so nothing below can be trusted yet. See the kube-router per-pod dispatch-rule race documented in network-policy.yaml / OPERATIONS.md. Exiting non-zero so this is loud (pod restart, CrashLoopBackOff) rather than a silent false pass."
  exit 1
fi

# --- Heartbeat: evidence for the liveness probe that the loop is still alive ---
# Touched here and again after every full cycle below. The liveness probe fails
# this container if HEARTBEAT_FILE's mtime is older than HEARTBEAT_MAX_AGE_SECONDS
# -- the one failure mode this specifically catches: the loop hangs mid-cycle on
# something with no bound of its own, e.g. the plain `nslookup api.github.com`
# call below has no explicit timeout and can wedge forever against a
# broken/unreachable DNS server. A liveness probe that just execs `true` would
# satisfy Kyverno's require-pod-probes policy too, without detecting anything --
# this is deliberately not that.
date +%s > "$HEARTBEAT_FILE"

# --- Main probe suite: runs on a loop once warm-up has succeeded ---
# Observation-first wording (REACHABLE / BLOCKED) rather than PASS/FAIL: a line
# like "FAIL: ... -- connected, expected denial" reads, at a skim, like the
# opposite of what happened -- that ambiguity is exactly what slowed down
# diagnosing the original false negative. (ok)/(violation) still carries the
# verdict, just stated after the observation instead of blended into it.
probe_deny() {
  desc="$1"; host="$2"; port="$3"
  probes=$((probes + 1))
  if nc -z -w "$TIMEOUT_SECONDS" "$host" "$port" 2>/dev/null; then
    echo "REACHABLE (violation): $desc ($host:$port)"
    violations=$((violations + 1))
  else
    echo "BLOCKED (ok): $desc ($host:$port)"
  fi
}

# Expect the connection to succeed. Exists so this probe can't pass against a
# network that's simply broken end-to-end rather than correctly scoped.
probe_allow() {
  desc="$1"; host="$2"; port="$3"
  probes=$((probes + 1))
  if nc -z -w "$TIMEOUT_SECONDS" "$host" "$port" 2>/dev/null; then
    echo "REACHABLE (ok): $desc ($host:$port)"
  else
    echo "BLOCKED (violation): $desc ($host:$port)"
    violations=$((violations + 1))
  fi
}

cycle=0
while true; do
  cycle=$((cycle + 1))
  probes=0
  violations=0

  # --- Mid-life re-verification (Fix 3) ---
  # The warm-up gate above only proves this pod was policed at container start --
  # a CLEAN verdict on cycle 50 otherwise rests on that one assumption from cycle
  # 0. Re-run the same check on a slower, distinguishable cadence
  # (MIDLIFE_REVERIFY_EVERY_N_CYCLES) so a mid-life lapse gets its own token
  # instead of blending into the ordinary per-cycle "UniFi gateway" deny probe
  # below as just one more line in a violations count.
  if [ $((cycle % MIDLIFE_REVERIFY_EVERY_N_CYCLES)) -eq 0 ]; then
    if nc -z -w "$TIMEOUT_SECONDS" "$UNIFI_GATEWAY" 443 2>/dev/null; then
      # Deliberately a distinct token from WARMUP-TIMEOUT: this is a materially
      # more alarming event -- the pod passed the warm-up gate at start and isn't
      # policed anymore, so every verdict from here on is untrustworthy, not just
      # this one target. Exit non-zero rather than log-and-continue: restarting
      # re-runs the warm-up gate on the new pod, re-proving enforcement before the
      # loop is trusted again, the same reasoning the warm-up gate itself uses.
      # The cost is losing this pod's running history (cycle count, any violation
      # trend) -- accepted, because continuing would mean this probe keeps
      # reporting BLOCKED (ok) results built on a premise that's already false,
      # which is exactly the "check that can't fail" class of bug this whole
      # redesign exists to close.
      echo "MIDLIFE-ENFORCEMENT-LAPSE: ${UNIFI_GATEWAY}:443 reachable at cycle ${cycle} -- this pod was policed at start (WARMUP-OK) but is not policed now. Exiting non-zero to force a restart and re-prove enforcement from a clean pod."
      exit 1
    fi
    echo "MIDLIFE-CHECK-OK: enforcement re-confirmed at cycle ${cycle} (next re-check in ${MIDLIFE_REVERIFY_EVERY_N_CYCLES} cycles)"
  fi

  for node in $NODE_IPS; do
    probe_deny "prod kube-apiserver" "$node" 6443
    # Longhorn manager (:9500) used to be probed here too, and always reported
    # "denied as expected" -- a false PASS. Longhorn's manager Service is
    # ClusterIP-only (confirmed against the chart: no hostNetwork/hostPort on the
    # longhorn-manager DaemonSet, no NodePort default), so node-IP:9500 is a dead
    # port with or without any NetworkPolicy -- a deny-check against a target
    # nothing is listening on passes forever and proves nothing. Dropped rather
    # than retargeted: kubelet (:10250, hostNetwork on every node) below already
    # exercises "does the deny mechanism work against every node IP", so no
    # replacement target is needed for that coverage.
    probe_deny "kubelet" "$node" 10250
    probe_deny "etcd" "$node" 2379
  done

  probe_deny "UniFi gateway" "$UNIFI_GATEWAY" 443
  probe_deny "in-cluster kubernetes Service" "$IN_CLUSTER_KUBERNETES_SERVICE" 443

  # LB_INGRESS_VIP_HOMELAB is a deny-assertion, not a positive control -- see that env
  # var's own comment on the Deployment, and ../network-policy.yaml, for the full
  # DNAT-vs-NetworkPolicy
  # mechanism (this is what caught the original version of this policy allowing a
  # range that could never actually match). This stack REJECTs rather than DROPs,
  # so BLOCKED alone can't distinguish a policy denial from a dead port -- proof
  # something is actually listening here comes from outside this probe: every one
  # of this cluster's ingress-fronted apps resolves to this exact VIP, so it is
  # definitely live. What's asserted is that DNAT-then-netpol denies pod-originated
  # traffic to it, not that nothing answers.
  probe_deny "homelab standard-pool ingress VIP (DNAT to pod IP, expected denied)" "$LB_INGRESS_VIP_HOMELAB" 443

  # Positive control for allow-egress-lb-ingress-vips in network-policy.yaml: nas's
  # standard-pool VIP has no Service on this cluster, so no DNAT intercepts it and
  # the ipBlock allow actually applies. TCP:443 accepting only proves L3
  # reachability, not that any particular hostname (Harbor, nas's API server) is
  # behind it -- this layer can't tell those apart, which is exactly the CAVEAT in
  # that policy file.
  probe_allow "nas standard-pool ingress VIP (LoadBalancer allow, fronts Harbor)" "$LB_INGRESS_VIP_NAS" 443

  probe_allow "GitHub API (internet egress)" "api.github.com" 443

  probes=$((probes + 1))
  if nslookup api.github.com >/dev/null 2>&1; then
    echo "REACHABLE (ok): DNS resolution via CoreDNS"
  else
    echo "BLOCKED (violation): DNS resolution via CoreDNS"
    violations=$((violations + 1))
  fi

  if [ "$violations" -eq 0 ]; then
    echo "SUMMARY namespace=$NAMESPACE cycle=$cycle probes=$probes violations=$violations verdict=CLEAN"
  else
    echo "SUMMARY namespace=$NAMESPACE cycle=$cycle probes=$probes violations=$violations verdict=VIOLATIONS-DETECTED"
  fi

  date +%s > "$HEARTBEAT_FILE"
  sleep "$PROBE_INTERVAL_SECONDS"
done
