# Offline wheelhouse

`Server~/offline/wheels` contains the locked Python dependencies used by the MCP
server. On first start, the Unity package installs from this directory with
`uv sync --offline --find-links` before attempting any network access.

The checked-in wheelhouse targets **Windows x64, CPython 3.11–3.14**. Other
platforms or Python versions automatically fall back to the online installer.
The wheelhouse deliberately does not contain a `.venv`: virtual environments
are machine-specific, while wheels are portable within the target platform.

Regenerate the wheelhouse after changing `pyproject.toml` or `uv.lock`:

```bash
cd Server~
python scripts/build_offline_wheelhouse.py --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

Commit the updated `offline/wheels` directory together with the lockfile.
