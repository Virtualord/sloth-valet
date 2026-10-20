# Benchmark fixtures

`labels.json` is the committed, deterministic description of 50 local fixtures.
It covers the default categories and a mix of text, PDF, DOCX, archive, image,
installer, media, and code files.

Generate the ignored fixture files with:

```sh
uv run python scripts/make_fixtures.py
uv run python scripts/make_fixtures.py --overwrite
```

The generator refuses to replace existing files unless `--overwrite` is passed,
never deletes unrelated files, and uses a fixed seed recorded in `labels.json`.

Run the comparison against models that are already pulled in Ollama:

```sh
uv run valet bench --models model-a,model-b
```

Model names are always supplied on the command line; the repository never names
or assumes a particular model. Add `--measure-memory` for a best-effort Linux
`VmHWM` reading from the Ollama server and its model-runner processes. Results are written to `bench/results/`
as a timestamped JSON file and a Markdown summary. Generated results are not
fabricated or committed automatically; run the benchmark locally and review the
files before adding authentic results to git.

The benchmark reads fixture files directly and calls the classifier only. It
does not create journal entries, move files, or invoke the report pipeline.
