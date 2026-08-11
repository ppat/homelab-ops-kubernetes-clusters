#!/usr/bin/env bash
# Gate for the Grafana Alloy (.alloy) config fragments this repo injects into the
# observability-core module's collector.
#
# Why a gate at all: a bad fragment is harmless on a *running* collector (the reload is
# rejected with a 400 and the last valid config keeps running) but fatal on a *fresh*
# one (the initial load fails and the process exits). So a broken fragment sits
# invisible until the DaemonSet next rolls, then takes out log collection node by node.
# Nothing else in either repo checks these files: the module's CI validates the module
# plus its own stand-in fragment, never this cluster's.
#
# Usage:
#   alloy-lint.sh fmt-check <file>...    # fail if formatting would change a file
#   alloy-lint.sh validate  <file>...    # validate each parent directory + name prefix
#   alloy-lint.sh check-embedded         # fragment file == the copy embedded in the
#                                        # Flux Kustomization that injects it
#
# All three modes are driven by the `alloy` CI job in .github/workflows/lint.yaml --
# there is no local pre-commit hook (this repo's primary dev environment has no Docker,
# and the shared lint-pre-commit reusable workflow only runs a fixed hardcoded hook-id
# list, so a local hook here would get zero CI coverage anyway). There is deliberately
# no `fmt-write` mode: it existed only to back that hook, and nothing else calls it.
#
# `validate` is directory-scoped and adds ci/alloy/module-anchors.alloy.stub, because
# alloy loads /etc/alloy as one merged component graph and these fragments reference
# module-owned components from the other repo. See that file for what the stub does and
# does not prove.
#
# fmt/validate run the pinned grafana/alloy image (ci/scripts/alloy-lint-version.yaml)
# so nothing needs the alloy binary installed -- only docker. check-embedded needs
# neither: just yq and diff.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
stub_file="${repo_root}/ci/alloy/module-anchors.alloy.stub"

alloy_version="$(sed -n 's/^version: "\(.*\)"$/\1/p' "${script_dir}/alloy-lint-version.yaml")"
if [[ -z "${alloy_version}" ]]; then
  echo "alloy-lint.sh: could not read pinned version from ${script_dir}/alloy-lint-version.yaml" >&2
  exit 1
fi
alloy_image="grafana/alloy:${alloy_version}"

# Matches the --stability.level the module's HelmRelease pins on the alloy container.
stability_level="generally-available"

# Every component a cluster declares must carry this prefix: all *.alloy files in
# /etc/alloy share one component namespace, and a name that collides with a module
# component fails the whole load, not just the offending file.
name_prefix="cluster_"

run_alloy() {
  local mount_dir="$1"
  shift
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "${mount_dir}:/workdir" \
    --workdir /workdir \
    "${alloy_image}" \
    "$@"
}

check_name_prefix() {
  local rc=0
  for f in "$@"; do
    while read -r name; do
      if [[ "${name}" != "${name_prefix}"* ]]; then
        echo "alloy-lint.sh: ${f}: component \"${name}\" must be named ${name_prefix}* (see the module README)" >&2
        rc=1
      fi
    done < <(sed -nE 's/^[a-z][a-z0-9_.]* "([^"]+)" \{$/\1/p' "${f}")
  done
  return "${rc}"
}

validate_dirs() {
  local rc=0
  mapfile -t dirs < <(for f in "$@"; do dirname -- "${f}"; done | sort -u)
  for d in "${dirs[@]}"; do
    local scratch
    scratch="$(mktemp -d)"
    cp "${d}"/*.alloy "${scratch}/"
    cp "${stub_file}" "${scratch}/zz-module-anchors.alloy"
    echo "alloy validate: ${d} (+ module anchor stub)"
    run_alloy "${scratch}" validate --stability.level="${stability_level}" . || rc=1
    rm -rf "${scratch}"
  done
  return "${rc}"
}

# A Flux Kustomization's spec.patches are inline-only: they cannot reference a file. The
# fragments therefore exist twice -- as a real .alloy file (what gets formatted and
# validated) and as a verbatim copy inside the patch (what Flux actually applies). This
# is the guard that keeps the copy honest.
check_embedded() {
  local rc=0 found=0
  local ks rel cluster n i kind name key src
  while IFS= read -r ks; do
    rel="${ks#"${repo_root}/"}"
    cluster="$(echo "${rel}" | cut -d'/' -f2)"
    n="$(yq '.spec.patches | length' "${ks}")"
    [[ "${n}" == "null" ]] && continue
    for ((i = 0; i < n; i++)); do
      kind="$(yq ".spec.patches[${i}].target.kind" "${ks}")"
      name="$(yq ".spec.patches[${i}].target.name" "${ks}")"
      [[ "${kind}" == "ConfigMap" && "${name}" == "alloy-config" ]] || continue
      local tmp
      tmp="$(mktemp -d)"
      yq ".spec.patches[${i}].patch" "${ks}" > "${tmp}/patch.yaml"
      while IFS= read -r key; do
        [[ "${key}" == *.alloy ]] || continue
        found=$((found + 1))
        # Both sides go through the same $(...) round trip, which strips trailing
        # newlines, so the comparison is exact on content and blind only to how many
        # newlines the file ends with (end-of-file-fixer already pins that to one).
        printf '%s\n' "$(yq ".data.\"${key}\"" "${tmp}/patch.yaml")" > "${tmp}/embedded"
        src="$(find "${repo_root}/clusters/${cluster}/services" -type f -name "${key}" -print -quit)"
        if [[ -z "${src}" ]]; then
          echo "alloy-lint.sh: ${rel} embeds '${key}' but no such file exists under clusters/${cluster}/services" >&2
          rc=1
          continue
        fi
        printf '%s\n' "$(cat "${src}")" > "${tmp}/src"
        if ! diff -u "${tmp}/src" "${tmp}/embedded" > "${tmp}/diff"; then
          echo "alloy-lint.sh: ${rel} embeds a copy of ${src#"${repo_root}/"} that has drifted:" >&2
          sed -e "s|${tmp}/src|${src#"${repo_root}/"}|" -e "s|${tmp}/embedded|<copy embedded in ${rel}>|" "${tmp}/diff" >&2
          rc=1
        else
          echo "ok: ${rel} embeds ${src#"${repo_root}/"} verbatim"
        fi
      done < <(yq '.data | keys | .[]' "${tmp}/patch.yaml")
      rm -rf "${tmp}"
    done
  done < <(find "${repo_root}/clusters" -path '*/kustomizations/*.yaml' -type f | sort)
  if [[ "${found}" -eq 0 ]]; then
    echo "alloy-lint.sh: found no embedded .alloy fragment to check" >&2
    return 1
  fi
  return "${rc}"
}

mode="${1:?usage: alloy-lint.sh <fmt-check|validate|check-embedded> [file...]}"
shift

if [[ "${mode}" == "check-embedded" ]]; then
  check_embedded
  exit
fi

# The CI job skips these modes when no *.alloy file matches; this guard only makes a
# manual invocation with no files a no-op.
if [[ $# -eq 0 ]]; then
  echo "alloy-lint.sh: no files given, nothing to do"
  exit 0
fi

case "${mode}" in
fmt-check)
  for f in "$@"; do
    run_alloy "$(pwd)" fmt --test "${f}"
  done
  ;;
validate)
  # Both run even if the first fails, so one report covers everything.
  rc=0
  check_name_prefix "$@" || rc=1
  validate_dirs "$@" || rc=1
  exit "${rc}"
  ;;
*)
  echo "alloy-lint.sh: unknown mode '${mode}' (expected fmt-check, validate or check-embedded)" >&2
  exit 1
  ;;
esac
