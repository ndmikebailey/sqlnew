# Archived lattice-warp experiment

This earlier experiment places byte-valued data in a 3D grid, applies reversible
bends, measures complete compressed files and recovers every original cell.
It is separate from native SQLNEW region storage.

The retained [measurements](saved-results/results.json) and
[method/decoding example](saved-results/report.md) record the original run.
Generated HTML, PNG/GIF and `.lat` files are preserved locally under ignored
`build/archive/lattice-warp-output`; regenerate them from this directory with:

```console
python -m pip install -r requirements.txt
python experiment.py --open
```

The generated `output/lattice.html` works offline. Drag to rotate, hover to read
a cell and choose **Animate best warp**. Matplotlib/Pillow produce the chart and
GIF. Other fixtures can be explored with `--seed 123 --output output-seed123` or
`--input data.npy --size 32 --output output-custom` (uint8 axes z, y, x).

The default search evaluates 81 shift maps on three 32x32x32 datasets: curved
repeated bands, approximately 10% random replacement and an independent random
control. Every candidate is decoded exactly. The favorable constructed fixture
and finite search do not establish a globally optimal compressor.

The original numerical run, saved-file recovery, animation and static chart were
verified. Interactive browser QA was not performed. Results are historical;
path organization does not establish new performance or browser validation.
