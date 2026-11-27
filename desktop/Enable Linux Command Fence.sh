#!/bin/bash
# Optional, administrator-run, Ubuntu 24.04: let bubblewrap start the fence that
# Lodestone's optional command providers need. The normal installer never runs this.
#
# Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor
# (kernel.apparmor_restrict_unprivileged_userns=1). This script leaves that
# global setting alone and grants user namespaces to /usr/bin/bwrap only:
#   1. Ubuntu's own profile, if this AppArmor release ships one
#      (/usr/share/apparmor/extra-profiles/bwrap-userns-restrict), else
#   2. a minimal profile, /etc/apparmor.d/lodestone-bwrap, scoped to /usr/bin/bwrap.
# Undo: sudo "$0" --remove
# Without this step Lodestone still works; optional command providers refuse.
set -euo pipefail

profile_name=lodestone-bwrap
target=/etc/apparmor.d/$profile_name
distro=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo; this changes the host's AppArmor policy for /usr/bin/bwrap only." >&2
  exit 2
fi
if [[ "${1:-}" == "--remove" ]]; then
  for name in "$profile_name" bwrap-userns-restrict; do
    if [[ -f /etc/apparmor.d/$name ]] && grep -q "Installed by Lodestone" "/etc/apparmor.d/$name"; then
      apparmor_parser -R "/etc/apparmor.d/$name" || true
      rm -f "/etc/apparmor.d/$name"
      echo "Removed /etc/apparmor.d/$name"
    fi
  done
  exit 0
fi
command -v apparmor_parser >/dev/null || { echo "apparmor_parser is not installed" >&2; exit 3; }
[[ -x /usr/bin/bwrap ]] || { echo "Install bubblewrap first: sudo apt-get install bubblewrap" >&2; exit 3; }

if [[ -f "$distro" ]]; then
  installed=/etc/apparmor.d/bwrap-userns-restrict
  if [[ ! -e "$installed" ]]; then
    { echo "# Installed by Lodestone from $distro"; cat "$distro"; } > "$installed"
  fi
  source_kind=distribution
else
  installed=$target
  cat > "$installed" <<'PROFILE'
# Installed by Lodestone: user namespaces for /usr/bin/bwrap only.
abi <abi/4.0>,
include <tunables/global>

profile lodestone-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,

  include if exists <local/lodestone-bwrap>
}
PROFILE
  source_kind=minimal
fi
apparmor_parser -r "$installed"
echo "Loaded $source_kind AppArmor profile $installed; kernel.apparmor_restrict_unprivileged_userns is unchanged:"
sysctl kernel.apparmor_restrict_unprivileged_userns || true
