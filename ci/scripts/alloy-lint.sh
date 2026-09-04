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
#   alloy-lint.sh fmt-check [file...]    # fail if formatting would change a fragment
#   alloy-lint.sh validate  [file...]    # validate each fragment group
#
# Every cluster-owned Alloy fragment lives ONLY inside a Flux Kustomization's
# spec.patches: patches are inline-only, so there is no file kustomize's own build
# could ever point at. fmt-check and validate therefore EXTRACT every `*.alloy`-named
# data key from every ConfigMap-targeting patch under clusters/*/kustomizations/*.yaml
# and lint the extracted bytes directly -- the patch is the only copy, and what CI
# checks is exactly what Flux ships. [file...] arguments are still accepted and linted
# the same way, for any fragment that does live on disk.
#
# `validate` groups every `*.alloy` key from the SAME spec.patches entry into one
# scratch directory before validating, mirroring how they'd actually be merged into one
# generated ConfigMap and thus into one /etc/alloy load at runtime -- plus
# ci/alloy/module-anchors.alloy.stub, because alloy loads /etc/alloy as one merged
# component graph and these fragments reference module-owned components from the other
# repo. See that file for what the stub does and does not prove.
#
# fmt/validate run the pinned grafana/alloy image (ci/scripts/alloy-lint-version.yaml)
# so nothing needs the alloy binary installed -- only docker.

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

# Extracts every `*.alloy` data key from every ConfigMap-targeting spec.patches entry
# across clusters/*/kustomizations/*.yaml into "${1}", one subdirectory per patch entry
# (named <kustomization-basename>-patch<index>) so that keys added together in one
# patch -- and therefore merged into the same ConfigMap and the same /etc/alloy load --
# land together on disk too. Echoes the number of keys extracted.
extract_embedded_fragments() {
  local out_root="$1" found=0
  local ks rel n i kind name key group_dir
  while IFS= read -r ks; do
    rel="${ks#"${repo_root}/"}"
    n="$(yq '.spec.patches | length' "${ks}")"
    [[ "${n}" == "null" ]] && continue
    for ((i = 0; i < n; i++)); do
      kind="$(yq ".spec.patches[${i}].target.kind" "${ks}")"
      name="$(yq ".spec.patches[${i}].target.name" "${ks}")"
      [[ "${kind}" == "ConfigMap" && "${name}" == "alloy-config" ]] || continue
      local tmp
      tmp="$(mktemp -d)"
      yq ".spec.patches[${i}].patch" "${ks}" > "${tmp}/patch.yaml"
      group_dir=""
      while IFS= read -r key; do
        [[ "${key}" == *.alloy ]] || continue
        if [[ -z "${group_dir}" ]]; then
          group_dir="${out_root}/$(basename "${ks}" .yaml)-patch${i}"
          mkdir -p "${group_dir}"
        fi
        # $(...) strips the one trailing newline end-of-file-fixer would otherwise
        # leave; immaterial to fmt/validate, kept for tidy extracted files.
        printf '%s\n' "$(yq ".data.\"${key}\"" "${tmp}/patch.yaml")" > "${group_dir}/${key}"
        found=$((found + 1))
        echo "alloy-lint.sh: extracted ${key} from ${rel} (spec.patches[${i}])" >&2
      done < <(yq '.data | keys | .[]' "${tmp}/patch.yaml")
      rm -rf "${tmp}"
    done
  done < <(find "${repo_root}/clusters" -path '*/kustomizations/*.yaml' -type f | sort)
  echo "${found}"
}

validate_group() {
  local d="$1" rc=0
  local scratch
  scratch="$(mktemp -d)"
  cp "${d}"/*.alloy "${scratch}/"
  cp "${stub_file}" "${scratch}/zz-module-anchors.alloy"
  echo "alloy validate: ${d} (+ module anchor stub)"
  run_alloy "${scratch}" validate --stability.level="${stability_level}" . || rc=1
  rm -rf "${scratch}"
  return "${rc}"
}

mode="${1:?usage: alloy-lint.sh <fmt-check|validate> [file...]}"
shift

case "${mode}" in
fmt-check | validate) ;;
*)
  echo "alloy-lint.sh: unknown mode '${mode}' (expected fmt-check or validate)" >&2
  exit 1
  ;;
esac

extract_dir="$(mktemp -d)"
trap 'rm -rf "${extract_dir}"' EXIT
extract_embedded_fragments "${extract_dir}" > /dev/null
mapfile -t extracted_files < <(find "${extract_dir}" -name '*.alloy' -type f | sort)

files=("$@" "${extracted_files[@]}")

# Nothing to do only when both the caller passed no files AND nothing was embedded --
# the latter is worth noticing (the extraction mechanism broken, or every fragment gone
# from every Kustomization) but not worth failing the job over here: a silent no-op
# matches how the CI job already tolerates an empty file list when
# `git ls-files -- '*.alloy'` matches nothing.
if [[ "${#files[@]}" -eq 0 ]]; then
  echo "alloy-lint.sh: no fragments given and none embedded, nothing to do"
  exit 0
fi

case "${mode}" in
fmt-check)
  for f in "${files[@]}"; do
    run_alloy "$(dirname "${f}")" fmt --test "$(basename "${f}")"
  done
  ;;
validate)
  rc=0
  check_name_prefix "${files[@]}" || rc=1
  # Explicit [file...] args are grouped per their own parent directory (the on-disk
  # behavior this mode always had); extracted fragments are already grouped per patch
  # entry by extract_embedded_fragments.
  mapfile -t explicit_dirs < <(for f in "$@"; do dirname -- "${f}"; done | sort -u)
  mapfile -t extracted_dirs < <(find "${extract_dir}" -mindepth 1 -maxdepth 1 -type d | sort)
  for d in "${explicit_dirs[@]}" "${extracted_dirs[@]}"; do
    [[ -z "${d}" ]] && continue
    validate_group "${d}" || rc=1
  done
  exit "${rc}"
  ;;
esac
