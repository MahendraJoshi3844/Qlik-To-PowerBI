Start a new feature (one feature per branch, never on main).

1. `git checkout main && git pull`
2. `git checkout -b feature/$ARGUMENTS`
3. Add or update the acceptance criteria in `docs/specs/SPEC-qlik-to-powerbi-migration.md` first.
4. Add the tasks to `docs/TASKS.md`.
5. `git push -u origin feature/$ARGUMENTS`
