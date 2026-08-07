# Beta channel — branch `beta`

This branch is the pre-release channel of the add-on. It is **not** meant to be merged into `main`
as-is: it carries a deliberate identity delta so beta and stable can be installed side by side.

## Install it

**Settings → Add-ons → Add-on Store → ⋮ → Repositories**, then add the repository URL with the
branch appended:

```
https://github.com/Playcolors-co/ha-enabot#beta
```

Supervisor accepts a `#<branch>` suffix on a repository URL (`RE_REPOSITORY` in its validator) and
clones that branch. The repository identity is the hash of the **whole** string, so this URL and the
plain one are two independent repositories: the beta add-on installs alongside the stable one, with
its own slug and its own configuration.

## The identity delta

Kept only on this branch — do not carry it into `main`:

| File | `beta` | `main` |
|---|---|---|
| `ebo/config.yaml` → `name` | `EBO for Home Assistant (unofficial, beta)` | `EBO for Home Assistant (unofficial)` |
| `ebo/config.yaml` → `version` | ahead of stable | the released version |
| `repository.yaml` → `name` | `… — beta channel` | `EBO for Home Assistant (unofficial)` |

## Promote

Open a PR from `beta` to `main`, resolve the conflict on those lines in favour of the `main`
values, and let the three required checks run. No rsync, no second repository: the history comes
across with the code.
