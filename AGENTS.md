# Commit Guideline

DO NOT commit by yourself. Commit changes only on my request.

# Repository Guidelines

## Committing

commit messages should start with `feature:`, `docs:`, `refactor:`, `fix:` or `chore:`.

## Documentation

Documents are under `docs`; when updating the documents, always update their Chinese version `*.zh.md` if necessary.
Always give recommendations on updating docs after committing any non-doc changes.

## Design Concerns

- Frontend platform-specific implementations must not leak outside `frontend/src/platforms`; code in other frontend folders must depend on platform abstractions only.
- Public frontend APIs may use Web-first types, but platform-specific implementations must remain confined to `frontend/src/platforms`.
- In backend code, when the type is explicit, prefer direct attribute access and avoid `getattr`.
- Add docstrings to all backend and frontend types, functions, structs, classes, and other APIs exposed outside their defining module.
- Backend docstrings must follow the NumPy docstring style.
- Frontend docstrings must use TypeDoc-compatible JSDoc/TSDoc block comments (`/** ... */`).
- Avoid using try catch too often; use only when it is urgently necessary.
