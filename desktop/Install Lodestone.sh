#!/bin/bash
# Per-user Ubuntu 24.04 install. Every executable download is pinned before use.
set -euo pipefail
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
  echo 'This download requires Ubuntu 24.04 x86_64.' >&2; exit 2
fi
. /etc/os-release
if [ "${ID:-}" != ubuntu ] || [ "${VERSION_ID:-}" != 24.04 ]; then
  echo 'This download requires Ubuntu 24.04 x86_64.' >&2; exit 2
fi
payload_dir="$(cd "$(dirname "$0")" && pwd -P)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
case "$data_home" in /*) ;; *) data_home="$HOME/.local/share" ;; esac
install_root="$data_home/lodestone-preview"
install_app="$data_home/applications/lodestone-preview.desktop"
download_cache=''
open_after=1
system_deps=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) install_root="$2"; shift 2 ;;
    --app) install_app="$2"; shift 2 ;;
    --cache) download_cache="$2"; shift 2 ;;
    --no-open) open_after=0; shift ;;
    --no-system-deps) system_deps=0; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
# GTK's SVG loader and accessibility bus are recommendations, so minimal
# systems can have GTK itself installed without these desktop runtime pieces.
packages=(curl ca-certificates libglib2.0-bin libgtk-3-0t64 librsvg2-common at-spi2-core libwebkit2gtk-4.1-0 libnotify4 libportaudio2 libgl1 libegl1 libsm6 libxtst6 libsdl2-2.0-0 bubblewrap)
missing=()
for package in "${packages[@]}"; do
  if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then missing+=("$package"); fi
done
if [ "${#missing[@]}" -ne 0 ]; then
  if [ "$system_deps" -eq 0 ]; then
    echo "Missing Ubuntu packages: ${missing[*]}" >&2; exit 2
  fi
  echo 'Installing the Ubuntu GUI and isolation prerequisites using apt.'
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
fi
# Reject existing alias ancestors before creating or downloading anything.
check_managed_path() {
  local checked="$1"
  case "$checked" in /*) ;; *) checked="$PWD/$checked" ;; esac
  while [ "$checked" != / ] && [ "$checked" != . ]; do
    if [ -L "$checked" ]; then echo "Managed path contains a symbolic link: $checked" >&2; exit 2; fi
    checked="$(dirname "$checked")"
  done
}
check_managed_path "$install_root"
if [ -n "$download_cache" ]; then check_managed_path "$download_cache"; fi
umask 077
mkdir -p "$install_root/downloads" "$install_root/deps/bootstrap"
install_root="$(cd "$install_root" && pwd -P)"
if [ -z "$download_cache" ]; then download_cache="$install_root/downloads"; fi
mkdir -p "$download_cache"
python_hash='c17aadf3265258bf3b4ee5182bdca24f3e4b34ffaa0a73b5efe2d84ddb9fcccc'
python_url='https://github.com/astral-sh/python-build-standalone/releases/download/20251120/cpython-3.12.12%2B20251120-x86_64-unknown-linux-gnu-install_only.tar.gz'
python_archive="$download_cache/$python_hash"
if [ ! -f "$python_archive" ]; then
  download_tmp="$(mktemp "$download_cache/python.XXXXXXXX")"
  trap 'rm -f "$download_tmp"' EXIT
  curl --fail --location --retry 3 --proto '=https' --proto-redir '=https' --tlsv1.2 "$python_url" -o "$download_tmp"
  actual_hash="$(sha256sum "$download_tmp" | awk '{print $1}')"
  if [ "$actual_hash" != "$python_hash" ]; then echo 'Python checksum mismatch.' >&2; exit 2; fi
  mv "$download_tmp" "$python_archive"
  trap - EXIT
fi
actual_hash="$(sha256sum "$python_archive" | awk '{print $1}')"
if [ "$actual_hash" != "$python_hash" ]; then echo 'Cached Python checksum mismatch.' >&2; exit 2; fi
# Use a fresh verified bootstrap every invocation; never execute an unchecked old tree.
python_stage="$(mktemp -d "$install_root/deps/bootstrap/python.XXXXXXXX")"
trap 'rm -rf "$python_stage"' EXIT
tar -xzf "$python_archive" -C "$python_stage"
export PYTHONDONTWRITEBYTECODE=1
"$python_stage/python/bin/python3.12" -I -B "$payload_dir/install.py" --root "$install_root" install \
  --payload "$payload_dir" --app "$install_app" --cache "$download_cache"
echo "Installed: $install_app"
if [ "$open_after" -eq 1 ]; then gio launch "$install_app"; fi
