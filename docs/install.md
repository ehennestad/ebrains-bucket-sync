# Installing ebrains-bucket-sync

`ebrains-bucket-sync` is a command-line program written in Python. The simplest way to install it is with pipx, which gives the program an environment of its own and puts the `ebrains-bucket-sync` command where your terminal finds it.

## With pipx

```bash
pipx install ebrains-bucket-sync
```

Check that it works:

```bash
ebrains-bucket-sync --version
```

### If you do not have pipx

Install it with pip:

```bash
python3 -m pip install --user pipx
```

Then let it add its command folder to your `PATH`:

```bash
python3 -m pipx ensurepath
```

Open a new terminal, and run `pipx install ebrains-bucket-sync`. On macOS with Homebrew, `brew install pipx` works too.

## With uv

If you use [uv](https://docs.astral.sh/uv/), it does the same as pipx:

```bash
uv tool install ebrains-bucket-sync
```

## For use from your own Python code

To call `sync_to_bucket` and the rest of the Python API from your own code, install the package into that project's virtual environment:

```bash
python -m pip install ebrains-bucket-sync
```

## Updating

```bash
pipx upgrade ebrains-bucket-sync
```

or, if you installed it with uv:

```bash
uv tool upgrade ebrains-bucket-sync
```

## Troubleshooting

**`command not found: ebrains-bucket-sync` after installing.** The folder the command was installed into is not on your `PATH`. Run `python3 -m pipx ensurepath` (or `uv tool update-shell` if you used uv), then open a new terminal. This also happens after a plain `pip install` outside a virtual environment, which can put the command in a folder such as `~/Library/Python/3.12/bin`.

**`error: externally-managed-environment` from pip.** Some Python installations, such as Homebrew's Python on macOS and the system Python on Debian and Ubuntu, do not let pip install packages into them. Install with pipx instead, or into a virtual environment.

**pip replaces your version of `ebrains-drive`.** `ebrains-bucket-sync` needs `ebrains-drive` 0.7.0 exactly. Installed with pip into an environment that has another version, it replaces that version for everything else in the environment. pipx and uv avoid this, since the program gets an environment of its own.
