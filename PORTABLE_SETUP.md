# Portable Setup

This branch is the portable source-only version of the project.

It keeps the workflow code and config, but does not store:

- local API keys
- model weights
- generated subtitles and caches
- bundled runtime binaries such as `whisper`, `llama`, and `ffmpeg/ffmpeg`

## What To Prepare On Another Computer

1. Clone this repository.
2. Install Python dependencies from `requirements.txt`.
3. Provide runtime tools and models:
   - `whisper/whisper-src/build-cuda/bin/whisper-cli`
   - `whisper/ggml-large-v3.bin`
   - `whisper/ggml-silero-v5.1.2.bin`
   - `llama/llama-src/build-cuda/bin/llama-server`
   - `llama/Sakura-Galtransl-14B-v3.8-Q5_K_S.gguf`
   - `ffmpeg/ffmpeg`
4. Copy `project/api_key.example.txt` to `project/api_key.txt` and fill in your API key.
5. Review `project/headless_config.yaml` and adjust paths, endpoint, model, and local LLM settings if needed.

## Long-Running Mode

Start:

```bash
bash run_server.sh
```

Watch logs:

```bash
tail -f project/watch.log
```

Stop:

```bash
bash stop_server.sh
```

## What This Version Includes

- ASR quality inspection for repeated lines and repeated characters
- clipped-region ASR repair
- chunked tail repair for long repeated regions
- preserve-and-continue fallback after repeated repair failures
- GPT primary translation with automatic local Sakura fallback after repeated `503` errors
