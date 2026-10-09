# Installation and compatibility

## Get the complete checkout

Clone this repository (or `git pull` an existing clone), then install it in
a fresh Python 3.11 environment with
`python -m pip install -e . -c constraints-smoke.txt`. Run the commands below
from the checkout root. Editable installation from a checkout is the supported
installation layout; standalone wheel distribution is not covered here.

`external/validator/validator.py` and `external/DrawCAD/cad_to_svg.py` are now
included. Do not install an unrelated PyPI package named `validator`.
The default paths work without environment variables. Remove stale
`VALIDATOR_ROOT` or `DRAWCAD_ROOT` overrides if they point to missing directories.
For custom copies, set these variables to the directories containing
`validator.py` and `cad_to_svg.py`, respectively. The reverse CLI also accepts
`--validator-root` and `--drawcad-root`.

## System dependency

The four-view SVG is converted to PNG with `rsvg-convert`:

- Ubuntu/Debian: `sudo apt-get install librsvg2-bin`
- macOS/Homebrew: `brew install librsvg`

Check `rsvg-convert --version` before running the pipeline.
VTK 3D rendering additionally needs a working graphics backend.

## Offline verification (no dataset or API key needed)

```sh
python scripts/check_installation.py
# Also verify VTK rendering in a graphics-capable session:
python scripts/check_installation.py --render-3d
```

The check executes a box through the real sandbox and OCCT evaluator, checks
that invalid Python is rejected, and generates a four-view SVG and PNG.
It returns a nonzero exit status on failure. `out/installation-check/report.json`
records the environment and results. With `--render-3d`, it also exports
STEP/STL and a VTK PNG. This is an installation test, not a full benchmark run.

## Locally verified environments

On macOS 26.3, the box execution, OCCT checks, invalid-code rejection and
four-view SVG/PNG passed with Python 3.11.13, CadQuery 2.7.0,
cadquery-ocp 7.8.1.1.post1, NumPy 1.26.4 and VTK 9.3.1.
`constraints-smoke.txt` pins these core packages, not every transitive dependency.
Python 3.13.5 / CadQuery 2.7.0 also passed, including the optional VTK
STEP/STL/PNG check outside the macOS process sandbox.

## Python, CadQuery, and Windows

The package metadata allows Python >=3.10 and CadQuery >=2.4; these lower
bounds are not a guarantee that every combination is supported. Use the
smoke test to validate the actual environment. `ModuleNotFoundError: validator`
means the module is absent or the configured path is wrong; downgrading
Python does not fix that error.

Native Windows 11 / Python 3.12 / CadQuery 2.8.0 has not been validated by
this release. On Windows, WSL2 with Ubuntu provides the Linux installation
route above. Native Windows additionally needs `rsvg-convert` available on
PATH. We do not claim Windows is unsupported or that Python 3.10 is required.
The CI workflow checks geometry and drawings on Ubuntu with Python 3.11 and
3.12 and CadQuery 2.7.0; consult its run results before treating those as verified.

## Which version reproduces the paper?

Use a specific MUSE commit including `external/`, record `git rev-parse HEAD`
and `python -m pip freeze`, and retain the installation report with your results.
The bundled source hashes are in `external/SOURCE_SHA256.json`.
This repair makes the dependencies available; it does not establish an exact
historical paper environment or promise identical scores with newer OCCT,
CadQuery, or LLM judge versions. See `external/README.md` for source provenance.
