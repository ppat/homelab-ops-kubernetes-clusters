#!/bin/bash
set -euo pipefail

TEMP_DIR=$(mktemp -d)
# shellcheck disable=SC2064
trap "rm -rf ${TEMP_DIR}" EXIT

detect_kustomizations() {
  local detected_ks_file="$1"
  echo "Detecting all kustomizations..."
  grep -rlPz 'apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization' "${KUSTOMIZATION_DIR}/" | \
    xargs -n1 yq '[.metadata.name, .spec.path, .spec.sourceRef.name]' -o csv | \
    sort > "${detected_ks_file}"
  # shellcheck disable=SC2002
  cat "${detected_ks_file}" | tr ',' '\t' | column -t | pr -t -o 4
  echo " "
}

# Resolves the local clone directory to use for a given source `spec.url`,
# cloning it if it hasn't been seen yet in this script invocation.
#
# ${MODULES_DIR} (populated by the calling workflow's "Checkout modules
# repository" step, which checks out ppat/homelab-ops-kubernetes-apps with
# full history) is reused as-is -- without a redundant clone -- for any
# source whose url matches ${MODULES_URL}, since that's the overwhelming
# majority of sources today (every module/apps/infra source) and the
# workflow needs that same clone anyway for copy_components(). Any other
# url (e.g. the standalone homelab-ops-policies repo) gets its own clone
# under a deterministic, hash-of-url directory name, so a repo referenced
# by several Kustomizations (e.g. multiple policy-* Kustomizations pointing
# at the same GitRepository) is only cloned once: the directory's mere
# existence is the cache (an in-memory cache, e.g. an associative array,
# would NOT work here -- this function is invoked as `x=$(resolve_source_clone_dir ...)`,
# a command substitution, which runs in a subshell and discards any
# variables it sets once it returns).
#
# These per-url clones use `--filter=blob:none --no-checkout` (a partial,
# not a shallow/--depth clone): different Kustomizations pinning a source
# to different, possibly-old tags need the full ref/commit history to be
# resolvable, which a depth-limited shallow clone can't guarantee -- a
# blobless partial clone gets that same history cheaply (file contents are
# fetched lazily on checkout) without downloading every blob upfront.
resolve_source_clone_dir() {
  local url="$1"

  if [[ -n "${MODULES_URL:-}" && "${url}" == "${MODULES_URL}" ]]; then
    echo "${MODULES_DIR}"
    return
  fi

  local dir="${TEMP_DIR}/src-$(echo -n "${url}" | sha256sum | cut -c1-16)"
  if [[ ! -d "${dir}" ]]; then
    echo "Cloning ${url} -> ${dir}..." >&2
    if ! git clone --quiet --filter=blob:none --no-checkout "${url}" "${dir}" 2> /dev/null; then
      echo "ERROR: failed to clone external source url '${url}'" >&2
      return 1
    fi
  fi
  echo "${dir}"
}

copy_external_source() {
  local ks_src="$1"
  local ks_src_tag="$2"
  local ks_src_url="$3"
  local ks_path="$4"
  local copied_ks_sources="$5"

  local clone_dir
  clone_dir=$(resolve_source_clone_dir "${ks_src_url}")

  pushd "${clone_dir}" > /dev/null 2>&1
  if ! git checkout "${ks_src_tag}" 2> /dev/null; then
    echo "ERROR: unable to checkout '${ks_src_tag}' for source '${ks_src}' (url: ${ks_src_url})" >&2
    popd > /dev/null 2>&1
    return 1
  fi

  local copy_path="${ks_path}"

  if [[ -e "${copy_path}" ]]; then
    echo "${ks_src}: ${ks_src_url}@${ks_src_tag} -> ${DESTINATION_DIR}/${copy_path}..."
    mkdir -p "${DESTINATION_DIR}/${copy_path}"
    rsync -r -q "${copy_path}/" "${DESTINATION_DIR}/${copy_path}/"
    echo "${ks_src}" >> "${copied_ks_sources}"
  fi

  # The target Kustomization's own kustomization.yaml may declare sibling
  # directories as resources via a relative "../" path (e.g.
  # pod-security-standard/restricted's kustomization.yaml lists
  # "../baseline" so that `kustomize build` can resolve it). Those siblings
  # live outside ks_path, so the copy above alone won't have picked them up.
  # Parse the declared resources list and copy just the "../"-prefixed
  # entries too, resolved the same way kustomize itself would resolve them
  # (relative to ks_path's own directory) -- this targets genuinely-declared
  # dependencies instead of blanket-copying the whole parent directory.
  if [[ -f "${copy_path}/kustomization.yaml" ]]; then
    local resource
    while IFS= read -r resource; do
      [[ -z "${resource}" ]] && continue

      local sibling_path
      if ! sibling_path=$(realpath -m --relative-to="." "${copy_path}/${resource}" 2>/dev/null); then
        echo "WARNING: could not resolve resource '${resource}' declared in ${copy_path}/kustomization.yaml (relative to '${ks_path}'); skipping" >&2
        continue
      fi
      if [[ "${sibling_path}" == ..* ]]; then
        echo "WARNING: resource '${resource}' declared in ${copy_path}/kustomization.yaml resolves outside the source repo ('${sibling_path}'); skipping" >&2
        continue
      fi

      if [[ -e "${sibling_path}" ]]; then
        echo "${ks_src}: ${ks_src_url}@${ks_src_tag} -> ${DESTINATION_DIR}/${sibling_path} (sibling dependency of ${ks_path})..."
        mkdir -p "${DESTINATION_DIR}/${sibling_path}"
        rsync -r -q "${sibling_path}/" "${DESTINATION_DIR}/${sibling_path}/"
      fi
    done < <(yq '.resources[]' "${copy_path}/kustomization.yaml" 2> /dev/null | grep '^\.\./')
  fi

  popd > /dev/null 2>&1
}

copy_components() {
  echo "Copying components..."
  pushd "${MODULES_DIR}" > /dev/null 2>&1
  git checkout main 2> /dev/null
  mkdir -p "${DESTINATION_DIR}/components"
  rsync -r -q "${MODULES_DIR}/components/" "${DESTINATION_DIR}/components/"
  echo " "
  popd > /dev/null 2>&1
}

prep_external_sources() {
  local detected_ks_file="$1"
  local copied_ks_sources="$2"

  echo "Preparing external sources..."
  while IFS= read -r ks; do
    ks_path=$(echo ${ks} | cut -d',' -f2)
    ks_src=$(echo ${ks} | cut -d',' -f3)
    ks_src_file="${SOURCES_DIR}/${ks_src}.yaml"
    if [[ -e "${ks_src_file}" ]]; then
      ks_src_tag=$(yq '.spec.ref.tag // .spec.ref.branch' "${ks_src_file}")
      ks_src_url=$(yq '.spec.url' "${ks_src_file}")
      copy_external_source "${ks_src}" "${ks_src_tag}" "${ks_src_url}" "${ks_path}" "${copied_ks_sources}"
    fi
  done < <(grep -v root "${detected_ks_file}") | pr -t -o 4
  echo " "
}

show_file_counts() {
  echo "infrastructure $(find ${DESTINATION_DIR}/infrastructure/ -type f -print | wc -l)"
  echo "components $(find ${DESTINATION_DIR}/components/ -type f -print | wc -l)"
  echo "apps $(find ${DESTINATION_DIR}/apps/ -type f -print | wc -l)"
}

main() {
  local detected_ks_file="${TEMP_DIR}/detected"
  local copied_ks_sources="${TEMP_DIR}/copied"

  detect_kustomizations "${detected_ks_file}"
  prep_external_sources "${detected_ks_file}" "${copied_ks_sources}"
  copy_components

  echo "Copied files..."
  show_file_counts | column -t
  echo " "

  echo "Capturing utilized external sources..."
  local utilized=$(grep -v root "${copied_ks_sources}" | paste -sd ',' -)
  echo "${OUTPUT_NAME}=${utilized}" >> $GITHUB_OUTPUT
  echo ${utilized} | pr -t -o 4
  echo " "
}

main
