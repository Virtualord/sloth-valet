# Benchmark results

Authentic `valet bench` output is committed here. JSON and Markdown files in this
directory are intentionally unignored. Generated results should be reviewed before
they are added to git; this repository must never contain invented model names or
made-up numbers.

## Committed run: 2026-10-20 19:43 UTC

- File: [`bench-20261020-194341-b06338bc.json`](bench-20261020-194341-b06338bc.json)
  and [`bench-20261020-194341-b06338bc.md`](bench-20261020-194341-b06338bc.md)
- Fixtures: all 50 entries in [`../labels.json`](../labels.json)
- Host: `http://127.0.0.1:11434`
- Environment: Linux, 8 CPU cores, 7.6 GiB RAM. Ollama 0.40.6 used CPU inference;
  the installed GTX 1050 (compute capability 6.1) was not supported by that build's
  CUDA kernels.
- Models, as reported by `ollama list`:
  - `qwen2.5:0.5b`, digest `a8b0c5157701`
  - `llama3.2:1b`, digest `baf6a787fdff`

| Model | Valid JSON | Category accuracy | Mean latency | P95 latency | Peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| `qwen2.5:0.5b` | 100.0% | 12.0% | 935.5 ms | 1416.2 ms | 660.6 MiB |
| `llama3.2:1b` | 100.0% | 80.0% | 1489.5 ms | 1829.1 ms | 788.5 MiB |

The run recorded no classifier failures. The first request for each model includes
Ollama model loading. Peak RSS is a best-effort sum of Linux `VmHWM` for the Ollama
server and its Ollama-spawned `llama-server` runner.

These numbers are a single CPU-only run on synthetic fixtures. They demonstrate that
model choice changes category accuracy substantially, not that either model is
universally suitable for personal files. Do not hand-edit this table or the committed
result files; rerun the benchmark on the machine being reported.
