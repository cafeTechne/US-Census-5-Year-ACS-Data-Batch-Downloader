# Using the toolkit with local open-weights models

The MCP server is standard MCP over stdio with plain JSON results, so any MCP-capable client can drive it, including clients backed by a local model. Small models (7-14B) work best with the **core** tool profile.

## Tool profiles

| Profile | Tools | Use for |
| :--- | :--- | :--- |
| `full` (default) | every tool, full descriptions | frontier models, Claude Code |
| `core` | 14 tools: `get_recipe`, `search_tables`, `describe_variable`, `plan_extraction`, `download_table`, `derive_estimate`, `calculate_significance`, `trend_test`, `minimum_detectable_difference`, `build_analysis_frame`, `fit_model`, `make_figure`, `make_map`, `run_manifest` | local models |
| `core_spatial` | `core` plus `spatial_autocorrelation` (15 tools), same short descriptions | local models doing spatial work; the evaluation's spatial task |
| `review` | concordance and rule review tools | reviewer agents |

Select a profile with a flag or an environment variable (the flag wins):

```bash
python mcp_server.py --profile core
CENSUS_MCP_PROFILE=core python mcp_server.py
```

In the core profile every tool has a short description with one example call, flat parameters (the only exception is the optional `derive` list of `build_analysis_frame`), and every result starts with `summary` and `next_step`; failures add `error_code` and a plain-language `fix`. Profiles are defined in `census/mcp_profiles.py`.

If a model is unsure what to do, it should call `get_recipe` (no arguments lists the recipes in `docs/recipes/`).

## 1. Run a local model server

**Ollama** (OpenAI-compatible endpoint at `http://localhost:11434/v1`):

```bash
ollama pull qwen3:8b        # supports tool calling
ollama serve                # if not already running as a service
```

**LM Studio**: download a tool-calling model (for example Qwen3 8B or Llama 3.1 8B Instruct), then start the local server from the Developer tab (default `http://localhost:1234/v1`).

llama.cpp (`llama-server --jinja`) and vLLM (`--enable-auto-tool-choice`) also expose OpenAI-compatible endpoints with tool calls.

Choose a model whose chat template supports tool calls. Without a GPU, an 8B model at 4-bit quantization runs on CPU, but expect tens of seconds to minutes per turn.

## 2. Connect an MCP client

Any MCP client that can use a local model works. The server entry is the same everywhere:

```json
{
  "mcpServers": {
    "census": {
      "command": "python",
      "args": ["/path/to/US-Census-5-Year-ACS-Data-Batch-Downloader/mcp_server.py", "--profile", "core"],
      "env": {"CENSUS_API_KEY": "your-key"}
    }
  }
}
```

- **LM Studio** (0.3.17+): add the block above to `mcp.json` (Program tab, Install, Edit mcp.json).
- **Open WebUI, Continue, Cline, and similar clients**: add the server under their MCP settings and point the client at the Ollama or LM Studio endpoint.
- **Your own script**: `scripts/local_model_eval.py` is a minimal working example: it starts the server over stdio with the `mcp` Python package and relays tool calls between the server and any OpenAI-compatible endpoint.

The Census API key is only needed for downloads; derivations, significance tests, trend tests, planning, and recipes work offline.

## 3. Check a model before relying on it

```bash
python scripts/local_model_eval.py --offline-check                # expected answers only, no model
python scripts/local_model_eval.py --model qwen3:8b               # Ollama default endpoint
python scripts/local_model_eval.py --base-url http://localhost:1234/v1 --model <name>
```

Several models can be compared in one report: `--model qwen3:8b llama3.1:8b`.

The harness runs scripted tasks with known answers (a rate with its MOE, a two-county significance test, a trend across non-overlapping releases, a refused overlapping trend, a minimum detectable difference, a recipe lookup, a reliability-aware tract map with `make_map`, and Moran's I with `spatial_autocorrelation` on the `core_spatial` profile), scores tool choice, arguments, and the final answer, and writes `docs/audits/local_model_eval.md`. The map and spatial tasks need no network: the server gets `CENSUS_GEO_DIR=tests/fixtures/geo` (the DC tract cartographic file) and a synthetic DC tract frame is written to `.cache/local_model_eval/`. For Qwen3 models it appends `/no_think` to skip the reasoning phase (pass `--think` to allow it). It is not part of CI.

## Tips for small models

- **Context length.** The `core` tool list alone is close to 4,096 tokens, Ollama's default context. Start the server with a larger context, for example `OLLAMA_CONTEXT_LENGTH=8192 ollama serve` (or set `num_ctx` in the model's options), or the model may lose the tool definitions mid-conversation.
- **CPU-only machines.** If Ollama offloads part of the model to a weak or integrated GPU, generation can be limited by that GPU. Compare against CPU-only by passing `"options": {"num_gpu": 0}`.
- **Recipes first.** In evaluation (docs/audits/local_model_eval.md) the one failed task was a multi-step question the model answered from tool descriptions without calling `get_recipe`. The core instructions now tell models to call `get_recipe` first.

- Keep the system prompt short and tell the model to use tools for every number.
- Use temperature 0.
- Check the final answer against the tool result: the tool output is authoritative, and every estimate should be reported with its 90% MOE.
