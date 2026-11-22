# Preserve and recover your own files

**Platform: macOS/Linux.** Recovery relies on secure directory descriptors and
no-follow filesystem operations; native Windows is not supported in this release.

The local archive tools underpin Lodestone's Saṃsāra continuity workflow. They
create a new archive, keep the originals, verify payload bytes, and recover into
a new directory. Cloud upload and independently verified off-device custody are
separate operations; this command does not perform them.

Start with a small directory whose files you are authorised to preserve:

```sh
python3 lodestone.py yggdrasil archive plan --root documents=./my-documents
python3 lodestone.py yggdrasil archive pack --root documents=./my-documents --destination ./my-archive --epoch first-copy --authority-ref local-user-request
python3 lodestone.py yggdrasil archive verify --archive ./my-archive
python3 lodestone.py yggdrasil archive restore --archive ./my-archive --destination ./my-restored-files
```

Both destination paths must be new. The pack result includes the descriptor's
SHA-256; retain it separately and pass `--expected-sha256 HASH` to later verify
or restore commands when you need an independently pinned descriptor.

`.git` directories are excluded. This is file-content recovery, not automatic
Git history restoration, account migration or a disk image. Ownership, ACLs,
extended attributes, hardlink identity and sparse layout are outside the archive
contract. Preserve anything you need beyond that contract separately. Never
remove an original merely because an archive was created: verify a recovery and
your own independent copy first.
