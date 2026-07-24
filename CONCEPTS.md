# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Native plugin delivery

### Native plugin
The headless WebAssembly implementation that performs automatic tab naming inside Zellij, distinct from the separately runnable Python watcher.

### Artifact pipeline
The end-to-end process that checks and compiles the Native plugin, packages verifiable output, and promotes the same bytes from a Workflow artifact to a Release asset.

### Workflow artifact
The build-scoped Native plugin binary and digest pair produced by a successful CI run for verification or download before release.

### Release asset
The immutable, versioned publication of a verified Workflow artifact, created only when the release tag agrees with the package version.

## Session persistence

### Session lock
An explicit per-session mark that makes the session detach rather than quit when its terminal closes. Locking requires the user to provide a deliberate session name; a name alone does not imply a lock.

### Unlocked session
A disposable session that quits when its terminal closes. It may retain a descriptive name after being unlocked.
