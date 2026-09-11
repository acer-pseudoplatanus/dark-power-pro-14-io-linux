# Release Process

bqio follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) and
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Cut a release

1. **Fold `[Unreleased]`** in `CHANGELOG.md` into a dated version heading
   (e.g. `## [1.1.0] — YYYY-MM-DD`).
2. **Bump the version** in `pyproject.toml` (`version = "1.1.0"`).
3. **Verify the gates locally** (must all be green):

   ```bash
   make lint typecheck test   # ruff, mypy strict, pytest + coverage gate
   ```

4. **Commit and tag** with an *annotated* tag:

   ```bash
   git commit -am "release: vX.Y.Z"
   git tag -a vX.Y.Z -m "bqio vX.Y.Z"
   git push origin main vX.Y.Z
   ```

5. **CI publishes the artifacts.** The `release: created` trigger runs the
   `build` job (sdist + wheel, `twine check`) and uploads the `dist`
   artifact. Download it from the GitHub Actions run page.

## Publishing to PyPI (when ready)

The project is intentionally private today; publishing is a deliberate
decision, not an accident. When it happens:

```bash
python -m build
python -m twine upload dist/*
```

using a trusted publisher or a scoped API token — never a full-account
password.

## Rollback

Tags are immutable. If a release ships broken, cut the next patch release
with the fix rather than deleting the tag; note the retraction in the
CHANGELOG under the new version.
