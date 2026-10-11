# Bundled evaluation modules

This directory includes the MUSE `CadQueryValidator` and DrawCAD four-view
renderer. No separate download or PyPI `validator` package is required.
Keep the complete DrawCAD directory: its `renderer`, `svg`, and `utils`
modules are required by `cad_to_svg.py`.

These files were copied from the development workspace's configured OrcaChat
modules on 2026-10-10 without changing the geometry or drawing algorithms
(trailing whitespace in the drawing entry point was removed).
The validator matches the local anonymous-review artifact byte for byte.
DrawCAD includes local development changes, so its upstream Git HEAD alone
is not an exact source identifier. `SOURCE_SHA256.json` records the precise
bundled Python files. The MUSE commit containing this directory identifies
this distribution; it is not a claim that historical paper runs all used
these exact bytes. Historical experiment environment provenance has not
been fully reconstructed.

Use `python scripts/check_installation.py` from the repository checkout.
See [installation and compatibility](../docs/installation.md).
