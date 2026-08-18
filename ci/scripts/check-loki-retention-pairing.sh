#!/usr/bin/env bash
# Fails if the homelab cluster's two manually-maintained copies of Loki's global
# retention period drift apart.
#
# kustomizations/infra-observability-core.yaml's `loki_retention_size` postBuild
# variable is what actually configures the compactor (rendered into Loki's
# limits_config.retention_period Helm value). kustomizations/config-services.yaml's
# `global_loki_retention` postBuild variable feeds the loki-query-correctness CronJob's
# GLOBAL_RETENTION env var (services/loki-query-correctness/loki-query-correctness.py),
# which uses it to compute when a baselined query window stops being safely immutable
# under retention.
#
# Flux has no mechanism for one Kustomization's postBuild.substitute to reference
# another's, so these are two independent literals that must be kept equal by hand. A
# stale `global_loki_retention` fails in the unsafe direction: it makes the retention
# guard compute a horizon that can outlive the data actually still on disk, silently
# reintroducing the false "migration lost data" alarm class the guard exists to prevent
# (see the incident this guard was built for, referenced in loki-query-correctness.py's
# module docstring). This is a trivial string-equality check, not a semantic one - it
# only catches this one pair going out of sync, not e.g. a third file gaining its own
# copy of the same value.
set -euo pipefail

config_services_file="clusters/homelab/kustomizations/config-services.yaml"
observability_core_file="clusters/homelab/kustomizations/infra-observability-core.yaml"

extract() {
  local key="$1" file="$2" value
  # Anchored on column-agnostic leading whitespace so this doesn't depend on the
  # surrounding indentation level, but anchored to start-of-line so it can't match the
  # key name inside a comment (every mention of these key names in prose in either file
  # is inside a `#`-prefixed comment line, never at the start of the stripped line).
  value="$(grep -E "^[[:space:]]*${key}:" "${file}" | sed -E "s/^[[:space:]]*${key}:[[:space:]]*//")"
  if [[ -z "${value}" ]]; then
    echo "::error file=${file}::could not find a '${key}:' line - has this Kustomization been restructured? Update this script's grep pattern to match." >&2
    exit 1
  fi
  if [[ "$(wc -l <<<"${value}")" -gt 1 ]]; then
    echo "::error file=${file}::found more than one '${key}:' line - this script expects exactly one" >&2
    exit 1
  fi
  printf '%s' "${value}"
}

global_loki_retention="$(extract global_loki_retention "${config_services_file}")"
loki_retention_size="$(extract loki_retention_size "${observability_core_file}")"

if [[ "${global_loki_retention}" != "${loki_retention_size}" ]]; then
  echo "::error file=${config_services_file}::global_loki_retention (${global_loki_retention}) does not match loki_retention_size (${loki_retention_size}) in ${observability_core_file} - loki-query-correctness's retention guard (GLOBAL_RETENTION) would compute a wrong horizon against the compactor's actual retention_period. Flux cannot reference one Kustomization's postBuild variable from another, so these two literals must be updated together by hand." >&2
  exit 1
fi

echo "OK: global_loki_retention (${config_services_file}) == loki_retention_size (${observability_core_file}) == ${global_loki_retention}"
