# Generated runtime package

`src/`, `pyproject.toml` and `uv.lock` are generated copies of the repository root files.
Edit the root files, then run `make plugin` and commit the refreshed copy. The launcher runs
this copy from the installed plugin root; it keeps its environment in plugin state.
