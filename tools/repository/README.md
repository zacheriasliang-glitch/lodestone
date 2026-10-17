# Repository export and local recovery

This toolkit separates a Git history backup from an explicitly selected source
archive. It needs Git and operates locally. The source checkout is not changed.
Only committed files you name are selected; no laboratory manifest is inherited.

```sh
python3 -B lodestone.py repository manifest --repository /path/to/project --file README.md --file src/app.py --output /path/to/review-manifest.json
# Read the manifest and selected source before exporting.
python3 -B lodestone.py repository export --repository /path/to/project --manifest /path/to/review-manifest.json --output /path/to/new-export
python3 -B lodestone.py repository verify /path/to/new-export
```

Both output destinations must be new. The export directory must be outside the
source checkout. `manifest` pins the chosen commit and blob identities. The
export checks those pins, creates a deterministic `clean-harness.tar.gz`, writes
checksums and a source map, and verifies a `private-repository.bundle`.
`verify` restores that bundle in a fresh temporary repository, runs strict fsck,
checks the restored HEAD and every file in the clean archive. It executes no
program from the recovered project.

The bundle includes committed history reachable from local refs and HEAD,
including secrets ever committed there. **Keep the bundle and private-state.json
private.** The selected source archive is only a publication candidate: explicit
review and a small marker check are not a general privacy guarantee. No upload or
publication occurs. Uncommitted, ignored and untracked files, external LFS objects,
and submodule repositories are outside this backup contract. Use the
[file archive toolkit](../../docs/archives.md) for ordinary filesystem preservation.
