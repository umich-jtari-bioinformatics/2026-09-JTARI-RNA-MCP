# Setup: the project environment

Everything this project needs on your Mac lives in one conda environment,
`jtari-mcp`. Nothing is installed system-wide.

```bash
conda activate jtari-mcp      # do this in every new terminal before working
```

It was created with:

```bash
conda create -n jtari-mcp -c conda-forge python=3.13 uv gh
```

| Tool | Version | Location |
|---|---|---|
| Python | 3.13.16 | `~/miniforge3/envs/jtari-mcp/bin/python` |
| uv | 0.13.1 | `~/miniforge3/envs/jtari-mcp/bin/uv` |
| gh | 2.102.0 | `~/miniforge3/envs/jtari-mcp/bin/gh` |

---

## What `uv` is for

**uv installs and pins the Python packages the server needs** (`mcp`, `duckdb`,
`pyarrow`, …). It does the job of `pip` plus `pip freeze`, but faster and with an
exact lock file.

| File | What it is | Committed? |
|---|---|---|
| `pyproject.toml` | the list of packages we depend on, e.g. `mcp>=2.3,<3` | yes |
| `uv.lock` | the exact version of every package, including dependencies of dependencies | yes |
| `.venv/` | the installed packages, created by uv inside the repo | no (gitignored) |

In nf-core terms: `pyproject.toml` is like the `environment.yml` a module asks
for, and `uv.lock` is like pinning the container by digest. Anyone who runs `uv sync`
gets exactly the same packages, on your Mac, on a lab member's laptop and on EC2.

There are two layers, and they don't conflict:

```
conda env  jtari-mcp  →  holds the tools (python, uv, gh)
   └─ uv  →  .venv/ in the repo  →  holds the server's packages, pinned by uv.lock
```

Commands you'll use:

```bash
uv sync                          # install exactly what uv.lock says into .venv/
uv add duckdb                    # add a dependency (updates pyproject.toml and uv.lock)
uv run python -m jtari_mcp.server   # run something inside .venv/ without activating it
uv run pytest                    # run the tests
```

Claude Desktop launches the server with `uv run`. Desktop doesn't know about
`conda activate`, so its config uses the full path
`~/miniforge3/envs/jtari-mcp/bin/uv` (written out in full, e.g. `/Users/<you>/miniforge3/envs/jtari-mcp/bin/uv`).

---

## What `gh` is for

**gh is GitHub's command-line tool.** Plain `git` moves code: commit, push, pull.
`gh` handles everything else on github.com from the terminal:

| Task | Command |
|---|---|
| Log in once (opens your browser) | `gh auth login` |
| Check you're logged in and can reach the org | `gh auth status` |
| Open a pull request for a phase branch | `gh pr create` |
| Read a PR and its diff | `gh pr view 3`, `gh pr diff 3` |
| Merge a PR after you approve it | `gh pr merge 3` |

This project uses one branch per phase, and you approve each one as a pull
request, so you read the diff before anything reaches `main`. `gh` is what
creates those pull requests.

Login is the one step only you can do:

```bash
conda activate jtari-mcp
gh auth login       # choose GitHub.com → HTTPS → "Login with a web browser"
gh auth status      # should list your account and the umich-jtari-bioinformatics org
```

---

## Not installed (yet)

- **AWS CLI:** only needed if Claude launches the EC2 instance from this Mac (Day 2).
  Otherwise you use the AWS web console with step-by-step instructions.
- **Docker:** only needed to test the container locally (Day 2). Not required to
  deploy, because the image can be built on the EC2 instance itself.
