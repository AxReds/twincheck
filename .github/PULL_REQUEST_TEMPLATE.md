## What does this pull request change?

<!-- Short description. Link the issue if there is one: Fixes #123 -->

## Type

- [ ] New language
- [ ] Translation fix / improvement
- [ ] Bug fix
- [ ] Feature
- [ ] Documentation

## Checklist

- [ ] `python3 app/tools/check_locales.py` passes
- [ ] `python3 -B -m unittest discover -s app/tests` passes
- [ ] No visible text hard-coded in the code (keys added to `en.json`)
- [ ] Placeholders (`{count}`, `{path}`…) unchanged; plural forms match the language
- [ ] New environment variables documented in `app/backend/README.md` and `docker/README.md`
- [ ] Language table in `README.md` updated (new language or status change)
- [ ] No photos, databases, `.env`, keys, passwords or tokens committed

## For translations

- Language code and name:
- Status (`partial` / `needs-review` / `complete`):
- Are you a native speaker? Did you review the whole file?
