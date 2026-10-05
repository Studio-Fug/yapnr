#!/bin/sh
# runtime-packages DIR...: the Debian packages holding the shared libraries that the ELF files
# under DIR link (by ldd), one per line, so a runtime image installs those and nothing of the
# build. Libraries under /opt (the install itself) are left out. Exits 1, naming them, when a
# library is not found.
set -eu

links=$(mktemp)
trap 'rm -f "${links}"' EXIT
find "$@" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) -print \
  | while read -r file; do ldd "${file}" 2>/dev/null || true; done > "${links}"
if grep -q "not found" "${links}"; then
  grep "not found" "${links}" | sort -u >&2
  exit 1
fi
awk '/=> \// { print $3 }' "${links}" | sort -u | while read -r lib; do
  real=$(readlink -f "${lib}")
  case "${real}" in /opt/*) continue ;; esac
  # Ubuntu 24.04 merged /lib into /usr/lib: dpkg knows a library under either name.
  for path in "${lib}" "${real}" "/usr${lib}" "/usr${real}"; do
    package=$(dpkg -S "${path}" 2>/dev/null | head -n 1 | cut -d: -f1)
    if [ -n "${package}" ]; then
      echo "${package}"
      break
    fi
  done
done | sort -u
