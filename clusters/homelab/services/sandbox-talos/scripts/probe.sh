#!/bin/sh
set -u

# --- Warm-up: establish enforcement before trusting anything below ---
# See sandbox-docker's copy of this Deployment for the full rationale.
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
# See sandbox-docker's copy of this Deployment for the full rationale (Fix 2).
date +%s > "$HEARTBEAT_FILE"

# --- Main probe suite: runs on a loop once warm-up has succeeded ---
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
  # See sandbox-docker's copy of this Deployment for the full rationale: why this
  # is a separate, rarer check rather than reusing the ordinary per-cycle "UniFi
  # gateway" deny probe below, and why a lapse restarts the pod instead of
  # logging and continuing.
  if [ $((cycle % MIDLIFE_REVERIFY_EVERY_N_CYCLES)) -eq 0 ]; then
    if nc -z -w "$TIMEOUT_SECONDS" "$UNIFI_GATEWAY" 443 2>/dev/null; then
      echo "MIDLIFE-ENFORCEMENT-LAPSE: ${UNIFI_GATEWAY}:443 reachable at cycle ${cycle} -- this pod was policed at start (WARMUP-OK) but is not policed now. Exiting non-zero to force a restart and re-prove enforcement from a clean pod."
      exit 1
    fi
    echo "MIDLIFE-CHECK-OK: enforcement re-confirmed at cycle ${cycle} (next re-check in ${MIDLIFE_REVERIFY_EVERY_N_CYCLES} cycles)"
  fi

  for node in $NODE_IPS; do
    probe_deny "prod kube-apiserver" "$node" 6443
    # Longhorn manager (:9500) dropped -- see sandbox-docker's copy of this
    # Deployment for why: its Service is ClusterIP-only, so node-IP:9500 is a
    # dead port regardless of policy and always reported a false "denied as
    # expected". kubelet below already covers "does the deny mechanism work
    # against every node IP".
    probe_deny "kubelet" "$node" 10250
    probe_deny "etcd" "$node" 2379
  done

  probe_deny "UniFi gateway" "$UNIFI_GATEWAY" 443
  probe_deny "in-cluster kubernetes Service" "$IN_CLUSTER_KUBERNETES_SERVICE" 443

  # Cross-sandbox denial: the load-bearing check for why these are two
  # namespaces rather than one. See network-policy.yaml.
  probes=$((probes + 1))
  if nslookup "$DOCKER_SSH_SERVICE" >/dev/null 2>&1; then
    if nc -z -w "$TIMEOUT_SECONDS" "$DOCKER_SSH_SERVICE" "$DOCKER_SSH_PORT" 2>/dev/null; then
      echo "REACHABLE (violation): Docker namespace SSH Service ($DOCKER_SSH_SERVICE:$DOCKER_SSH_PORT)"
      violations=$((violations + 1))
    else
      echo "BLOCKED (ok): Docker namespace SSH Service ($DOCKER_SSH_SERVICE:$DOCKER_SSH_PORT)"
    fi
  else
    echo "SKIP: Docker namespace SSH Service ($DOCKER_SSH_SERVICE:$DOCKER_SSH_PORT) -- not deployed yet"
  fi

  # Same-namespace positive control: the Talos VM's own Service, both ports it
  # serves. See the file-level comment above for why this exists and why it's
  # SKIP (not a violation) when the Service doesn't resolve yet. Two ports, so
  # (unlike the single-target DOCKER_SSH_SERVICE check above) the SKIP branch
  # bumps `probes` by 2 itself rather than sharing one outer increment --
  # probe_allow already does its own increment for each port on the resolvable
  # path, so this keeps the SKIP and checked paths counting the same total either
  # way.
  if nslookup "$TALOS_VM_SERVICE" >/dev/null 2>&1; then
    probe_allow "Talos VM Talos API (same-namespace ingress)" "$TALOS_VM_SERVICE" "$TALOS_VM_TALOS_PORT"
    probe_allow "Talos VM Kubernetes API (same-namespace ingress)" "$TALOS_VM_SERVICE" "$TALOS_VM_K8S_PORT"
  else
    probes=$((probes + 2))
    echo "SKIP: Talos VM Service ($TALOS_VM_SERVICE) -- not deployed yet (both ports)"
  fi

  # LB_INGRESS_VIP_HOMELAB is a deny-assertion (DNAT to a Traefik pod IP before
  # NetworkPolicy evaluates it), LB_INGRESS_VIP_NAS is the positive control -- see
  # sandbox-docker's copy of this Deployment for the full rationale (REJECT vs
  # DROP, why proof-of-liveness for the homelab target comes from outside this
  # probe, and why the NAS check only proves L3 reachability, not which hostname
  # is behind the VIP).
  probe_deny "homelab standard-pool ingress VIP (DNAT to pod IP, expected denied)" "$LB_INGRESS_VIP_HOMELAB" 443
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
