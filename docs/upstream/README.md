# Upstream mirror — do not edit

Verbatim snapshot of `docs/*.md` from `upstream/main` (`mortiphi/oc_mortic`).

These files exist so drift is mechanically visible. Nothing here is authoritative
for this repo, and nothing here should ever be hand-edited.

## Refreshing

```bash
git fetch upstream
for f in $(git ls-tree --name-only upstream/main docs/ | grep '\.md$'); do
  git show "upstream/main:$f" > "docs/upstream/$(basename "$f")"
done
```

Refresh deliberately, not on a schedule — the point is to choose when to look.

## Seeing drift

```bash
for f in docs/*.md; do
  b=$(basename "$f")
  diff -q "$f" "docs/upstream/$b" >/dev/null 2>&1 || echo "DRIFT: $b"
done
```

That tells you *what* diverged. `docs/dev/upstream-drift.md` records *why*.
After a refresh, reconcile the two: any new mechanical drift either gets an
entry there or gets reverted.

## Not mirrored

`docs/MORTIPHI_YC_APPLICATION_*.docx` — company material, not engineering docs.
Left untouched at `docs/` root.
