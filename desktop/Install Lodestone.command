#!/bin/bash
# Fresh macOS setup. Downloads are fixed by version and SHA-256 before execution.
set -euo pipefail
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != x86_64 ]; then
  echo 'This preview supports Intel macOS. No installation has been performed.' >&2
  exit 2
fi
payload_dir="$(cd "$(dirname "$0")" && pwd -P)"
install_root="$HOME/Library/Application Support/Lodestone Preview"
install_app="$HOME/Applications/Lodestone Preview.app"
download_cache=''
open_after=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) install_root="$2"; shift 2 ;;
    --app) install_app="$2"; shift 2 ;;
    --cache) download_cache="$2"; shift 2 ;;
    --no-open) open_after=0; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
umask 077
mkdir -p "$install_root/downloads" "$install_root/deps/python"
install_root="$(cd "$install_root" && pwd -P)"
if [ -z "$download_cache" ]; then download_cache="$install_root/downloads"; fi
mkdir -p "$download_cache"
python_hash='09fd4433b95d2ddd7424e0c66e848f01a812d133c293fa1d2a40f7b768d9ea01'
python_url='https://github.com/astral-sh/python-build-standalone/releases/download/20251120/cpython-3.12.12%2B20251120-x86_64-apple-darwin-install_only.tar.gz'
python_archive="$download_cache/$python_hash"
if [ ! -f "$python_archive" ]; then
  echo 'Downloading the private application Python runtime…'
  download_tmp="$(mktemp "$download_cache/python.XXXXXXXX")"
  trap 'rm -f "$download_tmp"' EXIT
  /usr/bin/curl --fail --location --retry 3 --proto '=https' --tlsv1.2 "$python_url" -o "$download_tmp"
  actual_hash="$(/usr/bin/shasum -a 256 "$download_tmp" | /usr/bin/awk '{print $1}')"
  if [ "$actual_hash" != "$python_hash" ]; then echo 'Python checksum mismatch.' >&2; exit 2; fi
  mv "$download_tmp" "$python_archive"
  trap - EXIT
fi
actual_hash="$(/usr/bin/shasum -a 256 "$python_archive" | /usr/bin/awk '{print $1}')"
if [ "$actual_hash" != "$python_hash" ]; then echo 'Cached Python checksum mismatch.' >&2; exit 2; fi
python_dir="$install_root/deps/python/3.12.12"
if [ ! -x "$python_dir/python/bin/python3.12" ]; then
  python_stage="$(mktemp -d "$install_root/deps/python/.staging.XXXXXXXX")"
  /usr/bin/tar -xzf "$python_archive" -C "$python_stage"
  if [ -e "$python_dir" ]; then echo 'An incomplete Python installation exists; retain it for inspection.' >&2; exit 2; fi
  mv "$python_stage" "$python_dir"
fi
export PYTHONDONTWRITEBYTECODE=1
"$python_dir/python/bin/python3.12" -B "$payload_dir/install.py" --root "$install_root" install \
  --payload "$payload_dir" --app "$install_app" --cache "$download_cache"
echo "Installed: $install_app"
if [ "$open_after" -eq 1 ]; then /usr/bin/open "$install_app"; fi
