#!/bin/bash
# Optional, administrator-run, Ubuntu 24.04: let bubblewrap start the fence that
# Lodestone's optional command providers need. The normal installer never runs this.
#
# Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor
# (kernel.apparmor_restrict_unprivileged_userns=1). This leaves that global
# setting alone and loads upstream AppArmor's own bwrap-userns-restrict policy,
# byte-for-byte, pinned below (AppArmor apparmor-4.0 branch, commit 4569381e,
# profiles/apparmor/profiles/extras/bwrap-userns-restrict, sha256 a964037f...):
# /usr/bin/bwrap may use user namespaces and capabilities inside them, and
# every program bwrap starts runs stacked under unpriv_bwrap, which denies
# capabilities, so bwrap cannot be used to bypass the restriction in general.
# Ubuntu 25.04 enables this same profile by default; 24.04 ships no copy.
#
#   sudo "./Enable Linux Command Fence.sh"           install and load
#   sudo "./Enable Linux Command Fence.sh" --remove  unload, then delete
#
# It manages only /etc/apparmor.d/lodestone-bwrap-userns-restrict whose bytes
# are exactly the header line plus the pinned policy. A symlink, a different
# file, or an independently loaded bwrap profile is reported and left
# untouched (nonzero exit). Without this step Lodestone still works; optional
# command providers refuse.
#
# Test hooks (never needed in normal use): LODESTONE_APPARMOR_DIR,
# LODESTONE_APPARMOR_PARSER, LODESTONE_APPARMOR_PROFILES, LODESTONE_BWRAP,
# LODESTONE_FENCE_TEST_NOT_ROOT=0 to skip the root check in isolated tests.
set -euo pipefail

directory=${LODESTONE_APPARMOR_DIR:-/etc/apparmor.d}
parser=${LODESTONE_APPARMOR_PARSER:-apparmor_parser}
loaded_list=${LODESTONE_APPARMOR_PROFILES:-/sys/kernel/security/apparmor/profiles}
bwrap=${LODESTONE_BWRAP:-/usr/bin/bwrap}
name=lodestone-bwrap-userns-restrict
target=$directory/$name
pinned=a964037f6cf0df1099f14226b037eaedde6237c86e715188e93eb460b30be859
header="# Installed by Lodestone (\"Enable Linux Command Fence.sh\"); remove with --remove. Upstream policy follows unchanged."

upstream() {
  cat <<'APPARMOR_UPSTREAM_EOF'
# This profile allows almost everything and only exists to allow
# bwrap to work on a system with user namespace restrictions
# being enforced.
# bwrap is allowed access to user namespaces and capabilities
# within the user namespace, but its children do not have
# capabilities, blocking bwrap from being able to be used to
# arbitrarily by-pass the user namespace restrictions.
#
# Note: the bwrap child is stacked against the bwrap profile due to
# bwraps use of no-new-privs

# disabled by default as it can break some use cases on a system that
# doesn't have or has disable user namespace restrictions for unconfined
# use aa-enforce to enable it

abi <abi/4.0>,

include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(attach_disconnected,mediate_deleted) {
  allow capability,
  # not allow all, to allow for pix stack
  # sadly we have to allow  m every where to allow children to work under
  # stacking.
  allow file rwlkm /{**,},
  allow network,
  allow unix,
  allow ptrace,
  allow signal,
  allow mqueue,
  allow io_uring,
  allow userns,
  allow mount,
  allow umount,
  allow pivot_root,
  allow dbus,
  allow px /** -> bwrap//&unpriv_bwrap,

  # the local include should not be used without understanding the userns
  # restriction.
  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/bwrap-userns-restrict>
}

profile unpriv_bwrap flags=(attach_disconnected,mediate_deleted) {
  # not allow all, to allow for pix stack
  allow file rwlkm /{**,},
  allow network,
  allow unix,
  allow ptrace,
  allow signal,
  allow mqueue,
  allow io_uring,
  allow userns,
  allow mount,
  allow umount,
  allow pivot_root,
  allow dbus,

  allow pix /** -> &unpriv_bwrap,

  audit deny capability,

  # the local include should not be used without understanding the userns
  # restriction.
  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/unpriv_bwrap>
}
APPARMOR_UPSTREAM_EOF
}

profile() { printf '%s\n' "$header"; upstream; }

usage() { echo "Usage: sudo \"$0\" [--remove]" >&2; exit 2; }
fail() { echo "$1" >&2; exit "${2:-1}"; }
sha256() { if command -v sha256sum >/dev/null; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi; }

case "$#:${1:-}" in
  0:) action=apply ;;
  1:--remove) action=remove ;;
  *) usage ;;
esac
[[ "$(upstream | sha256)" == "$pinned" ]] || fail "Embedded AppArmor policy does not match its pinned upstream hash" 7
if [[ "${LODESTONE_FENCE_TEST_NOT_ROOT:-1}" != 0 && $EUID -ne 0 ]]; then
  fail "Run with sudo; this changes the host's AppArmor policy for $bwrap only." 2
fi
command -v "$parser" >/dev/null || fail "apparmor_parser is not installed" 3

# Returns 0 for exactly our file, 1 if absent; refuses anything else untouched.
ours() {
  if [[ -L "$target" ]]; then
    fail "$target is a symbolic link; refusing to manage it" 4
  elif [[ ! -e "$target" ]]; then
    return 1
  elif [[ ! -f "$target" ]]; then
    fail "$target is not a regular file; refusing to manage it" 4
  elif ! cmp -s "$target" <(profile); then
    fail "$target exists with content Lodestone did not write; leaving it unchanged" 4
  fi
  return 0
}

# Read the loaded-profile list once: 0 = a bwrap profile is loaded,
# 1 = none is loaded, 2 = unknown (the list could not be read).
loaded_state() {
  local listing
  listing=$(cat -- "$loaded_list" 2>/dev/null) || return 2
  grep -Eq '^(bwrap|unpriv_bwrap) ' <<<"$listing" && return 0
  return 1
}

if [[ $action == remove ]]; then
  if ! ours; then
    echo "$target is not installed; nothing to remove"
    exit 0
  fi
  if loaded_state; then state=0; else state=$?; fi
  # Loaded or unknown: the unload must succeed before the file is deleted.
  if [[ $state != 1 ]]; then
    "$parser" -R "$target" || fail "Unloading the bwrap profiles failed; $target is kept" 5
  fi
  rm -- "$target"
  echo "Unloaded and removed $target"
  exit 0
fi

[[ -x "$bwrap" ]] || fail "Install bubblewrap first: sudo apt-get install bubblewrap" 3
if ours; then
  created=no
else
  for other in "$directory"/bwrap "$directory"/bwrap-userns-restrict "$directory"/usr.bin.bwrap; do
    if [[ -e "$other" || -L "$other" ]]; then
      fail "An independently installed bubblewrap profile exists ($other); Lodestone leaves it unchanged" 6
    fi
  done
  if loaded_state; then state=0; else state=$?; fi
  [[ $state != 2 ]] || fail "Cannot read the loaded AppArmor profiles ($loaded_list); changing nothing" 6
  [[ $state != 0 ]] || fail "A bubblewrap AppArmor profile is already loaded independently; Lodestone leaves it unchanged" 6
  staged=$(mktemp "$directory/.$name.XXXXXX")
  trap 'rm -f -- "$staged"' EXIT
  profile > "$staged"
  chmod 0644 "$staged"
  # A hard link publishes atomically and never replaces an existing name.
  ln -- "$staged" "$target" || fail "$target appeared concurrently; leaving it unchanged" 4
  created=yes
fi
if [[ $created == yes ]]; then
  # Add-only: never replaces a profile someone else loaded meanwhile.
  if ! "$parser" -a "$target"; then
    rm -- "$target"
    fail "Loading the bwrap profiles failed; the new Lodestone profile file was removed" 5
  fi
elif ! "$parser" -r "$target"; then
  fail "Reloading the existing Lodestone profile failed; $target is kept as it was" 5
fi
echo "Loaded $target; kernel.apparmor_restrict_unprivileged_userns is unchanged:"
sysctl kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || true
