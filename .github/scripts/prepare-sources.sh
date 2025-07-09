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
  cat "${detected_ks_file}" | tr ',' '\t' | column -t | pr -t -o 4
  echo " "
}

copy_external_source() {
  local ks_src="$1"
  local ks_src_tag="$2"
  local ks_path="$3"
  local copied_ks_sources="$4"

  pushd "${MODULES_DIR}" > /dev/null 2>&1
  git checkout ${ks_src_tag} 2> /dev/null
  if [[ -e "${ks_path}" ]]; then
    echo "${ks_src}: modules@${ks_src_tag} -> ${DESTINATION_DIR}/${ks_path}..."
    mkdir -p "${DESTINATION_DIR}/${ks_path}"
    rsync -r -q "${ks_path}/" "${DESTINATION_DIR}/${ks_path}/"
    echo "${ks_src}" >> "${copied_ks_sources}"
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
    ks_name=$(echo ${ks} | cut -d',' -f1)
    ks_path=$(echo ${ks} | cut -d',' -f2)
    ks_src=$(echo ${ks} | cut -d',' -f3)
    ks_src_file="${SOURCES_DIR}/${ks_src}.yaml"
    if [[ -e "${ks_src_file}" ]]; then
      ks_src_tag=$(yq '.spec.ref.tag // .spec.ref.branch' "${ks_src_file}")
      copy_external_source "${ks_src}" "${ks_src_tag}" "${ks_path}" "${copied_ks_sources}"
    fi
  done < <(cat "${detected_ks_file}" | grep -v root) | pr -t -o 4
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
  local utilized=$(cat "${copied_ks_sources}" | grep -v root | paste -sd ',' -)
  echo "${OUTPUT_NAME}=${utilized}" >> $GITHUB_OUTPUT
  echo ${utilized} | pr -t -o 4
  echo " "
}

main
