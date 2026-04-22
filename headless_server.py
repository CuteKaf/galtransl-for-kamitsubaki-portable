#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from itertools import groupby
from datetime import datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

import pysrt
import yaml

from GalTransl.ConfigHelper import CProjectConfig
from GalTransl.Runner import run_galtransl
from GalTransl.SubtitleQuality import (
    SubtitleQualityIssue,
    format_quality_issue,
    inspect_srt_quality,
)
from prompt2srt import make_srt
from srt2prompt import make_prompt


ROOT = Path(__file__).resolve().parent
PROJECT_DIR = ROOT / "project"
CONFIG_PATH = PROJECT_DIR / "headless_config.yaml"
API_KEY_PATH = PROJECT_DIR / "api_key.txt"
PROJECT_CONFIG_PATH = PROJECT_DIR / "config.yaml"
TIKTOKEN_CACHE_DIR = PROJECT_DIR / "tiktoken-cache"

os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(TIKTOKEN_CACHE_DIR))

MEDIA_EXTS = {
    ".wav",
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".srt",
}

TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


class RepairOutcome:
    def __init__(self, handled: bool, accepted_issue: SubtitleQualityIssue | None = None) -> None:
        self.handled = handled
        self.accepted_issue = accepted_issue


def is_local_translation(cfg: dict) -> bool:
    provider = str(cfg.get("translate", {}).get("provider", "api")).strip().lower()
    return provider in {"local", "sakura_local", "sakura"}


def has_local_fallback(cfg: dict) -> bool:
    local_cfg = cfg.get("local_llm", {})
    return bool(local_cfg.get("server_bin")) and bool(local_cfg.get("model"))


def should_use_gpt4_backend(cfg: dict) -> bool:
    translate = cfg.get("translate", {})
    engine = str(translate.get("engine", "")).strip().lower()
    if engine in {"gpt4", "gpt4-turbo"}:
        return True
    model_name = str(translate.get("model", "")).strip().lower()
    if not model_name or "3.5" in model_name:
        return False
    return any(
        token in model_name
        for token in ("gpt-4", "gpt4", "gpt-5", "gpt5", "claude", "gemini", "glm-4", "o1", "o3", "o4")
    )


def local_endpoint(cfg: dict) -> str:
    translate = cfg.get("translate", {})
    local_cfg = cfg.get("local_llm", {})
    host = str(local_cfg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    port = int(local_cfg.get("port", 8989))
    if not is_local_translation(cfg):
        if endpoint := str(translate.get("endpoint", "")).strip():
            return endpoint.rstrip("/")
    return f"http://{host}:{port}"


def load_headless_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def ensure_dirs(cfg: dict) -> dict:
    paths = cfg.setdefault("paths", {})
    defaults = {
        "inbox": "project/inbox",
        "processing": "project/processing",
        "outbox": "project/outbox",
        "done": "project/done",
        "failed": "project/failed",
    }
    resolved = {}
    for key, default in defaults.items():
        value = Path(paths.get(key, default))
        if not value.is_absolute():
            value = ROOT / value
        value.mkdir(parents=True, exist_ok=True)
        resolved[key] = value
    paths.update({k: str(v) for k, v in resolved.items()})
    return resolved


def read_api_key() -> str:
    if not API_KEY_PATH.exists():
        raise RuntimeError(f"missing API key file: {API_KEY_PATH}")
    return API_KEY_PATH.read_text(encoding="utf-8").strip()


def sync_project_config(cfg: dict, api_key: str | None = None) -> None:
    project_cfg = yaml.safe_load(PROJECT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    source_lang = cfg.get("asr", {}).get("language", "ja")
    translate = cfg.get("translate", {})
    proxy_addr = translate.get("proxy", "").strip()
    is_local = is_local_translation(cfg)

    project_cfg.setdefault("common", {})
    project_cfg["common"]["language"] = f"{source_lang}2zh-cn"

    backend = project_cfg.setdefault("backendSpecific", {})
    if is_local:
        sakura = backend.setdefault("Sakura", {})
        sakura["endpoint"] = local_endpoint(cfg)
    else:
        if not api_key:
            raise RuntimeError("API translation selected but no API key provided")
        endpoint = translate.get("endpoint", "https://api.deepseek.com").rstrip("/")
        model_name = translate.get("model", "deepseek-chat")
        for section_name in ("GPT35", "GPT4"):
            section = backend.setdefault(section_name, {})
            section["tokens"] = [{"token": api_key, "endpoint": ""}]
            section["defaultEndpoint"] = endpoint
            section["rewriteModelName"] = model_name

    proxy = project_cfg.setdefault("proxy", {})
    proxy["enableProxy"] = bool(proxy_addr) and not is_local
    proxy["proxies"] = [{"address": proxy_addr or "http://127.0.0.1:7890"}]

    PROJECT_CONFIG_PATH.write_text(
        yaml.safe_dump(project_cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def reset_translation_workspace() -> None:
    for folder in ["gt_input", "gt_output", "transl_cache"]:
        path = PROJECT_DIR / folder
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)


def purge_outbox_for_stem(outbox: Path, stem: str) -> list[Path]:
    removed: list[Path] = []
    candidates = [outbox / stem] + [
        path
        for path in outbox.iterdir()
        if path.is_dir() and path.name.startswith(f"{stem}_")
    ]
    for path in candidates:
        if path.exists() and path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
    return removed


def reset_state(paths: dict) -> None:
    inbox = paths["inbox"]
    processing = paths["processing"]
    outbox = paths["outbox"]

    restored_stems: set[str] = set()
    processing_files = sorted(p for p in processing.iterdir() if p.is_file())
    media_source_stems = {
        p.stem
        for p in processing_files
        if p.suffix.lower() in MEDIA_EXTS
        and p.suffix.lower() != ".srt"
        and not p.name.endswith(".16k.wav")
    }

    for path in processing_files:
        if path.name.endswith(".16k.wav"):
            path.unlink(missing_ok=True)
            print(f"[RESET] removed derived audio {path.name}", flush=True)
            continue

        if path.suffix.lower() == ".srt" and path.stem in media_source_stems:
            path.unlink(missing_ok=True)
            print(f"[RESET] removed derived subtitle {path.name}", flush=True)
            continue

        if path.suffix.lower() in MEDIA_EXTS:
            target = inbox / path.name
            if target.exists():
                target.unlink()
            shutil.move(str(path), str(target))
            restored_stems.add(path.stem)
            print(f"[RESET] restored source to inbox {path.name}", flush=True)
            continue

        path.unlink(missing_ok=True)
        print(f"[RESET] removed stray file {path.name}", flush=True)

    for folder in ["gt_input", "gt_output", "transl_cache"]:
        folder_path = PROJECT_DIR / folder
        if folder_path.exists():
            shutil.rmtree(folder_path)
        folder_path.mkdir(parents=True, exist_ok=True)
        print(f"[RESET] cleared {folder_path.relative_to(ROOT)}", flush=True)

    for stem in sorted(restored_stems):
        removed = purge_outbox_for_stem(outbox, stem)
        for path in removed:
            print(f"[RESET] removed partial output {path.relative_to(ROOT)}", flush=True)

    log_path = PROJECT_DIR / "llama_server.log"
    if log_path.exists():
        log_path.unlink()
        print(f"[RESET] removed {log_path.relative_to(ROOT)}", flush=True)


def run_cmd(args: list[str], extra_ld_paths: list[Path] | None = None) -> None:
    env = os.environ.copy()
    if extra_ld_paths:
        ld_paths = [str(path) for path in extra_ld_paths if path]
        if ld_paths:
            existing_ld = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = ":".join(ld_paths + ([existing_ld] if existing_ld else []))
    proc = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    if proc.stdout:
        print(proc.stdout, end="", flush=True)


def run_cmd_capture(args: list[str], extra_ld_paths: list[Path] | None = None) -> str:
    env = os.environ.copy()
    if extra_ld_paths:
        ld_paths = [str(path) for path in extra_ld_paths if path]
        if ld_paths:
            existing_ld = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = ":".join(ld_paths + ([existing_ld] if existing_ld else []))
    proc = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    return proc.stdout


def post_chat_completion(endpoint: str, model_name: str, timeout: int = 8) -> dict | None:
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    req = urlrequest.Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return json.loads(body)
    except (urlerror.URLError, urlerror.HTTPError, json.JSONDecodeError, TimeoutError):
        return None


class LocalLlamaServer:
    def __init__(self, cfg: dict):
        local_cfg = cfg.get("local_llm", {})
        self.server_bin = ROOT / str(local_cfg.get("server_bin", "llama/llama-server"))
        self.model_path = ROOT / str(
            local_cfg.get("model", "llama/Sakura-Galtransl-14B-v3.8-Q5_K_S.gguf")
        )
        self.host = str(local_cfg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        self.port = int(local_cfg.get("port", 8989))
        self.threads = int(local_cfg.get("threads", min(16, os.cpu_count() or 16)))
        self.ctx_size = int(local_cfg.get("ctx_size", 8192))
        self.gpu_layers = int(local_cfg.get("gpu_layers", 0))
        self.startup_timeout = int(local_cfg.get("startup_timeout", 180))
        self.log_path = ROOT / str(local_cfg.get("log_file", "project/llama_server.log"))
        self.extra_args = [str(arg) for arg in local_cfg.get("extra_args", [])]
        self.proc: subprocess.Popen[str] | None = None
        self.log_handle = None

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def model_name(self) -> str:
        return self.model_path.name

    def is_ready(self) -> bool:
        body = post_chat_completion(self.endpoint, self.model_name)
        return bool(isinstance(body, dict) and body.get("choices"))

    def tail_log(self, lines: int = 40) -> str:
        if not self.log_path.exists():
            return ""
        text = self.log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(text[-lines:])

    def ensure_started(self) -> None:
        if self.is_ready():
            print(f"[LLM] using existing server at {self.endpoint}", flush=True)
            return
        if not self.server_bin.exists():
            raise RuntimeError(f"missing llama-server binary: {self.server_bin}")
        if not self.model_path.exists():
            raise RuntimeError(f"missing local model: {self.model_path}")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("a", encoding="utf-8")
        cmd = [
            str(self.server_bin),
            "-m",
            str(self.model_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "-t",
            str(self.threads),
            "-c",
            str(self.ctx_size),
        ]
        if self.gpu_layers > 0:
            cmd.extend(["-ngl", str(self.gpu_layers)])
        cmd.extend(self.extra_args)

        print(f"[LLM] starting local server: {' '.join(cmd)}", flush=True)
        env = os.environ.copy()
        existing_ld = env.get("LD_LIBRARY_PATH", "")
        server_dir = self.server_bin.parent
        env["LD_LIBRARY_PATH"] = f"{server_dir}:{ROOT / 'llama'}:{existing_ld}".rstrip(":")
        self.proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        started_at = time.time()
        while time.time() - started_at < self.startup_timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "local llama-server exited early\n" + self.tail_log()
                )
            if self.is_ready():
                print(f"[LLM] local server ready at {self.endpoint}", flush=True)
                return
            time.sleep(2)

        raise RuntimeError("local llama-server startup timed out\n" + self.tail_log())

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            print("[LLM] stopping local server", flush=True)
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
        self.proc = None


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def truncate_text(text: str, limit: int = 72) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def seconds_to_ffmpeg_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole_seconds = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        whole_seconds += 1
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{millis:03d}"


def get_media_duration_seconds(input_path: Path) -> float:
    proc = subprocess.run(
        [
            str(ROOT / "ffmpeg" / "ffmpeg"),
            "-i",
            str(input_path),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stdout)
    if not match:
        return 0.0
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def shift_subtitle_times(subs: pysrt.SubRipFile, offset_seconds: float) -> pysrt.SubRipFile:
    offset_ms = int(round(offset_seconds * 1000))
    for sub in subs:
        sub.start.ordinal += offset_ms
        sub.end.ordinal += offset_ms
    return subs


def save_subtitles(subs: pysrt.SubRipFile, target: Path) -> None:
    for idx, sub in enumerate(subs, start=1):
        sub.index = idx
    subs.save(str(target), encoding="utf-8")


def merge_repaired_subtitles(
    original_srt_path: Path,
    replacement_subs: pysrt.SubRipFile,
    replace_start: float,
    replace_end: float,
) -> None:
    original_subs = pysrt.open(str(original_srt_path), encoding="utf-8")
    merged = pysrt.SubRipFile()
    for sub in original_subs:
        sub_start = sub.start.ordinal / 1000
        sub_end = sub.end.ordinal / 1000
        if sub_end <= replace_start or sub_start >= replace_end:
            merged.append(sub)
    for sub in replacement_subs:
        merged.append(sub)
    merged.sort(key=lambda item: item.start.ordinal)
    save_subtitles(merged, original_srt_path)


def cap_consecutive_repeats(
    subs: pysrt.SubRipFile, max_repeat: int
) -> pysrt.SubRipFile:
    if max_repeat <= 0:
        return pysrt.SubRipFile()

    capped = pysrt.SubRipFile()
    run_text = ""
    run_count = 0
    last_kept = None

    for sub in subs:
        normalized = " ".join(sub.text.replace("\r\n", "\n").replace("\r", "\n").split())
        if normalized and normalized == run_text:
            run_count += 1
        else:
            run_text = normalized
            run_count = 1 if normalized else 0
            last_kept = None

        if normalized and run_count > max_repeat:
            if last_kept is not None and sub.end.ordinal > last_kept.end.ordinal:
                last_kept.end.ordinal = sub.end.ordinal
            continue

        capped.append(sub)
        last_kept = sub

    return capped


def cap_repeated_characters(text: str, max_repeat: int) -> str:
    if max_repeat <= 0:
        return text
    pieces: list[str] = []
    for char, group in groupby(text):
        count = sum(1 for _ in group)
        pieces.append(char * min(count, max_repeat))
    return "".join(pieces)


def preserve_quality_issue_in_place(
    srt_path: Path,
    issue: SubtitleQualityIssue,
    asr: dict,
) -> SubtitleQualityIssue | None:
    subs = pysrt.open(str(srt_path), encoding="utf-8")
    preserve_repeat_limit = int(asr.get("preserve_max_consecutive_repeats", 5))
    preserve_char_limit = int(asr.get("preserve_max_char_repeats", 20))

    if issue.kind == "consecutive_repeat":
        subs = cap_consecutive_repeats(subs, preserve_repeat_limit)
        save_subtitles(subs, srt_path)
    elif issue.kind == "char_repeat":
        changed = False
        for sub in subs:
            if sub.index != issue.start_index:
                continue
            capped_text = cap_repeated_characters(sub.text, preserve_char_limit)
            if capped_text != sub.text:
                sub.text = capped_text
                changed = True
        if not changed:
            return issue
        save_subtitles(subs, srt_path)
    else:
        return issue

    return inspect_srt_quality(srt_path)


def run_whisper_cli(
    wav_path: Path,
    output_prefix: Path,
    asr: dict,
    asr_ld_paths: list[Path],
    use_vad: bool,
) -> str:
    cmd = [
        str(ROOT / str(asr.get("cli_bin", "whisper/whisper-src/build-cuda/bin/whisper-cli"))),
        "-m",
        str(ROOT / asr.get("model", "whisper/ggml-large-v3.bin")),
        "-osrt",
        "-t",
        str(asr.get("threads", 16)),
        "-l",
        asr.get("language", "ja"),
        str(wav_path),
        "-of",
        str(output_prefix),
    ]
    if use_vad:
        cmd.extend(
            [
                "--vad",
                "--vad-model",
                str(ROOT / asr.get("vad_model", "whisper/ggml-silero-v5.1.2.bin")),
            ]
        )
    output = run_cmd_capture(cmd, extra_ld_paths=asr_ld_paths)
    if output:
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
    return output


def should_use_chunked_repair(
    issue: SubtitleQualityIssue,
    total_duration: float,
    asr: dict,
    allow_chunked_repair: bool,
) -> bool:
    if not allow_chunked_repair or issue.kind != "consecutive_repeat":
        return False
    min_repeat_count = int(asr.get("tail_repair_min_repeat_count", 50))
    min_duration = float(asr.get("tail_repair_min_duration_seconds", 900))
    return issue.count >= min_repeat_count or issue.duration_seconds >= min_duration


def repair_workspace_for(path: Path) -> Path:
    parent = path.parent
    while parent.name == "_repair":
        parent = parent.parent
    return parent / "_repair"


def make_repair_temp_paths(srt_path: Path, tag: str) -> tuple[Path, Path, Path]:
    repair_dir = repair_workspace_for(srt_path)
    repair_dir.mkdir(parents=True, exist_ok=True)
    short_id = uuid.uuid4().hex[:12]
    prefix = repair_dir / f"{tag}_{short_id}"
    return prefix.with_suffix(".wav"), prefix, prefix.with_suffix(".srt")


def repair_degenerated_srt_segment(
    wav_path: Path,
    srt_path: Path,
    issue: SubtitleQualityIssue,
    asr: dict,
    asr_ld_paths: list[Path],
    total_duration: float,
) -> RepairOutcome:
    if issue.kind not in {"consecutive_repeat", "char_repeat"}:
        return RepairOutcome(False)

    padding_seconds = float(asr.get("repair_padding_seconds", 8))
    clip_start = max(0.0, issue.start_time_seconds - padding_seconds)
    clip_end = min(total_duration, issue.end_time_seconds + padding_seconds)
    if clip_end <= clip_start:
        return RepairOutcome(False)

    clip_wav, clip_prefix, clip_srt = make_repair_temp_paths(srt_path, "clip")

    issue_label = "repeated subtitle segment" if issue.kind == "consecutive_repeat" else "subtitle quality issue"
    print(
        f"[ASR] detected {issue_label}, retrying clipped region "
        f"{seconds_to_ffmpeg_time(clip_start)} -> {seconds_to_ffmpeg_time(clip_end)} "
        f"(原始异常: {format_quality_issue(issue)})",
        flush=True,
    )

    try:
        run_cmd(
            [
                str(ROOT / "ffmpeg" / "ffmpeg"),
                "-y",
                "-ss",
                seconds_to_ffmpeg_time(clip_start),
                "-to",
                seconds_to_ffmpeg_time(clip_end),
                "-i",
                str(wav_path),
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(clip_wav),
            ]
        )
        run_whisper_cli(
            clip_wav,
            clip_prefix,
            asr,
            asr_ld_paths,
            use_vad=False,
        )
        repaired_issue, accepted_repaired_issue = repair_srt_quality_issues(
            clip_wav,
            clip_srt,
            asr,
            asr_ld_paths,
            clip_end - clip_start,
            allow_chunked_repair=True,
            context_label="ASR clipped retry",
        )
        if repaired_issue:
            print(
                "[ASR] clipped retry still failed quality check: "
                f"{format_quality_issue(repaired_issue)}",
                flush=True,
            )
            return RepairOutcome(False)

        repaired_subs = pysrt.open(str(clip_srt), encoding="utf-8")
        shift_subtitle_times(repaired_subs, clip_start)
        merge_repaired_subtitles(srt_path, repaired_subs, clip_start, clip_end)
        print(
            "[ASR] clipped retry succeeded and merged back into original subtitle",
            flush=True,
        )
        if accepted_repaired_issue is not None:
            merged_issue = inspect_srt_quality(srt_path)
            return RepairOutcome(True, accepted_issue=merged_issue)
        return RepairOutcome(True)
    finally:
        clip_wav.unlink(missing_ok=True)
        clip_srt.unlink(missing_ok=True)


def repair_srt_quality_issues(
    wav_path: Path,
    srt_path: Path,
    asr: dict,
    asr_ld_paths: list[Path],
    total_duration: float,
    *,
    allow_chunked_repair: bool,
    context_label: str = "ASR",
) -> tuple[SubtitleQualityIssue | None, SubtitleQualityIssue | None]:
    max_repair_attempts = int(asr.get("max_repair_attempts", 5))
    issue = inspect_srt_quality(srt_path)
    repair_attempt = 0
    while issue and issue.kind in {"consecutive_repeat", "char_repeat"} and repair_attempt < max_repair_attempts:
        repair_attempt += 1
        print(
            f"[{context_label}] repair attempt {repair_attempt}/{max_repair_attempts}: "
            f"{format_quality_issue(issue)}",
            flush=True,
        )
        if should_use_chunked_repair(issue, total_duration, asr, allow_chunked_repair):
            repaired = repair_large_repeated_region(
                wav_path,
                srt_path,
                issue,
                asr,
                asr_ld_paths,
                total_duration,
            )
        else:
            repaired = repair_degenerated_srt_segment(
                wav_path,
                srt_path,
                issue,
                asr,
                asr_ld_paths,
                total_duration,
            )
        if not repaired.handled:
            break
        if repaired.accepted_issue is not None:
            return None, repaired.accepted_issue
        issue = inspect_srt_quality(srt_path)

    if issue and repair_attempt >= max_repair_attempts:
        preserved_issue = preserve_quality_issue_in_place(srt_path, issue, asr)
        print(
            f"[{context_label}] reached max repair attempts, preserving current subtitle "
            f"content and continuing: {format_quality_issue(issue)}",
            flush=True,
        )
        return None, preserved_issue
    return issue, None


def repair_large_repeated_region(
    wav_path: Path,
    srt_path: Path,
    issue: SubtitleQualityIssue,
    asr: dict,
    asr_ld_paths: list[Path],
    total_duration: float,
) -> RepairOutcome:
    if issue.kind != "consecutive_repeat":
        return RepairOutcome(False)

    padding_seconds = float(asr.get("repair_padding_seconds", 8))
    chunk_seconds = float(asr.get("tail_repair_chunk_seconds", 300))
    preserve_repeat_limit = int(asr.get("preserve_max_consecutive_repeats", 5))
    region_start = max(0.0, issue.start_time_seconds - padding_seconds)
    region_end = min(total_duration, issue.end_time_seconds + padding_seconds)
    if region_end <= region_start:
        return RepairOutcome(False)

    total_chunks = max(1, int((region_end - region_start + chunk_seconds - 1) // chunk_seconds))
    replacement_subs = pysrt.SubRipFile()
    original_subs = pysrt.open(str(srt_path), encoding="utf-8")
    preserved_chunk_count = 0

    print(
        "[ASR] detected a large repeated region, retrying in chunks "
        f"{seconds_to_ffmpeg_time(region_start)} -> {seconds_to_ffmpeg_time(region_end)} "
        f"(共 {total_chunks} 段, 原始异常: {format_quality_issue(issue)})",
        flush=True,
    )

    for chunk_index in range(total_chunks):
        core_start = region_start + chunk_index * chunk_seconds
        core_end = min(region_end, core_start + chunk_seconds)
        extract_start = max(region_start, core_start - padding_seconds)
        extract_end = min(region_end, core_end + padding_seconds)
        chunk_tag = f"tail{chunk_index + 1:02d}"
        chunk_wav, chunk_prefix, chunk_srt = make_repair_temp_paths(srt_path, chunk_tag)
        chunk_duration = extract_end - extract_start

        print(
            "[ASR] tail chunk "
            f"{chunk_index + 1}/{total_chunks}: "
            f"{seconds_to_ffmpeg_time(core_start)} -> {seconds_to_ffmpeg_time(core_end)}",
            flush=True,
        )

        try:
            run_cmd(
                [
                    str(ROOT / "ffmpeg" / "ffmpeg"),
                    "-y",
                    "-ss",
                    seconds_to_ffmpeg_time(extract_start),
                    "-to",
                    seconds_to_ffmpeg_time(extract_end),
                    "-i",
                    str(wav_path),
                    "-acodec",
                    "pcm_s16le",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(chunk_wav),
                ]
            )
            run_whisper_cli(
                chunk_wav,
                chunk_prefix,
                asr,
                asr_ld_paths,
                use_vad=False,
            )
            chunk_issue = repair_srt_quality_issues(
                chunk_wav,
                chunk_srt,
                asr,
                asr_ld_paths,
                chunk_duration,
                allow_chunked_repair=False,
                context_label=f"ASR chunk {chunk_index + 1}/{total_chunks}",
            )
            final_chunk_issue, accepted_chunk_issue = chunk_issue
            if final_chunk_issue or accepted_chunk_issue:
                preserved_chunk_count += 1
                issue_to_report = final_chunk_issue or accepted_chunk_issue
                print(
                    "[ASR] tail chunk still failed quality check after retries, "
                    "preserving original segment: "
                    f"{format_quality_issue(issue_to_report)}",
                    flush=True,
                )
                for sub in original_subs:
                    sub_start = sub.start.ordinal / 1000
                    sub_end = sub.end.ordinal / 1000
                    if sub_end <= core_start or sub_start >= core_end:
                        continue
                    replacement_subs.append(sub)
                replacement_subs = cap_consecutive_repeats(
                    replacement_subs, preserve_repeat_limit
                )
                continue

            chunk_subs = pysrt.open(str(chunk_srt), encoding="utf-8")
            shift_subtitle_times(chunk_subs, extract_start)
            for sub in chunk_subs:
                sub_start = sub.start.ordinal / 1000
                sub_end = sub.end.ordinal / 1000
                if sub_end <= core_start or sub_start >= core_end:
                    continue
                replacement_subs.append(sub)
        finally:
            chunk_wav.unlink(missing_ok=True)
            chunk_srt.unlink(missing_ok=True)

    replacement_subs.sort(key=lambda item: item.start.ordinal)
    merge_repaired_subtitles(srt_path, replacement_subs, region_start, region_end)
    accepted_issue = None
    if preserved_chunk_count:
        accepted_issue = inspect_srt_quality(srt_path)
        print(
            "[ASR] chunked tail retry completed with preserved original content "
            f"for {preserved_chunk_count} chunk(s); continuing with current subtitle",
            flush=True,
        )
    else:
        print("[ASR] chunked tail retry succeeded and merged back into original subtitle", flush=True)
    return RepairOutcome(True, accepted_issue=accepted_issue)


def read_srt_progress(srt_path: Path) -> tuple[float, str]:
    if not srt_path.exists():
        return 0.0, ""

    text = srt_path.read_text(encoding="utf-8", errors="ignore")
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    if not blocks:
        return 0.0, ""

    latest_end = 0.0
    latest_text = ""
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        timestamp_line = next((line for line in lines if "-->" in line), "")
        match = TIMESTAMP_RE.search(timestamp_line)
        if not match:
            continue
        latest_end = (
            int(match.group(5)) * 3600
            + int(match.group(6)) * 60
            + int(match.group(7))
            + int(match.group(8)) / 1000
        )
        text_lines = [line for line in lines if "-->" not in line and not line.isdigit()]
        if text_lines:
            latest_text = " ".join(text_lines)
    return latest_end, latest_text


def count_srt_entries(srt_path: Path) -> int:
    if not srt_path.exists():
        return 0
    text = srt_path.read_text(encoding="utf-8", errors="ignore")
    return text.count("-->")


def validate_srt_quality(srt_path: Path) -> None:
    issue = inspect_srt_quality(srt_path)
    if issue is None:
        return
    raise RuntimeError(f"SRT quality check failed: {format_quality_issue(issue)}")


def monitor_asr_progress(srt_path: Path, total_duration: float, stop_event: threading.Event) -> None:
    last_key = (-1.0, "")
    started_at = time.time()
    last_heartbeat_bucket = -1
    while not stop_event.is_set():
        latest_end, latest_text = read_srt_progress(srt_path)
        current_key = (latest_end, latest_text)
        if latest_end > 0 and current_key != last_key:
            progress = (latest_end / total_duration * 100) if total_duration > 0 else 0.0
            print(
                f"[ASR] {min(progress, 100.0):5.1f}% "
                f"({format_seconds(latest_end)}/{format_seconds(total_duration)}) "
                f"{truncate_text(latest_text)}",
                flush=True,
            )
            last_key = current_key
        elif latest_end <= 0:
            elapsed = int(time.time() - started_at)
            heartbeat_bucket = elapsed // 15
            if heartbeat_bucket != last_heartbeat_bucket:
                print(
                    f"[ASR] running... elapsed {format_seconds(elapsed)} "
                    f"(audio {format_seconds(total_duration)})",
                    flush=True,
                )
                last_heartbeat_bucket = heartbeat_bucket
        time.sleep(2)


def read_translation_progress(cache_path: Path) -> tuple[int, str, str]:
    if not cache_path.exists():
        return 0, "", ""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return 0, "", ""

    if not isinstance(data, list) or not data:
        return 0, "", ""

    last = data[-1]
    jp_text = str(last.get("post_jp") or last.get("pre_jp") or "")
    zh_text = str(last.get("post_zh_preview") or last.get("pre_zh") or "")
    return len(data), jp_text, zh_text


def monitor_translation_progress(
    cache_path: Path, total_segments: int, stop_event: threading.Event
) -> None:
    last_count = -1
    last_pair = ("", "")
    while not stop_event.is_set():
        translated_count, jp_text, zh_text = read_translation_progress(cache_path)
        current_pair = (jp_text, zh_text)
        if translated_count > 0 and (
            translated_count != last_count or current_pair != last_pair
        ):
            progress = (translated_count / total_segments * 100) if total_segments > 0 else 0.0
            print(
                f"[TRANSLATE] {translated_count}/{total_segments} {min(progress, 100.0):5.1f}% "
                f"JP: {truncate_text(jp_text)} | ZH: {truncate_text(zh_text)}",
                flush=True,
            )
            last_count = translated_count
            last_pair = current_pair
        time.sleep(2)


def output_dir_for_base(base_name: str, outbox: Path) -> Path:
    target = outbox / base_name
    target.mkdir(parents=True, exist_ok=True)

    # Backfill from older timestamped output dirs so future runs converge
    # into a single stable folder per source file.
    legacy_dirs = sorted(
        [
            path
            for path in outbox.iterdir()
            if path.is_dir() and path.name.startswith(f"{base_name}_")
        ],
        key=lambda path: path.name,
    )
    for legacy_dir in legacy_dirs:
        for legacy_file in legacy_dir.iterdir():
            if legacy_file.is_file():
                shutil.copy2(legacy_file, target / legacy_file.name)
    return target


def transcribe_to_srt(
    input_path: Path, cfg: dict, processing_dir: Path
) -> tuple[Path, SubtitleQualityIssue | None]:
    asr = cfg.get("asr", {})
    stem = input_path.stem
    reuse_existing_16k = input_path.suffix.lower() == ".wav" and input_path.name.endswith(".16k.wav")
    if reuse_existing_16k:
        wav_path = input_path
    else:
        wav_path = processing_dir / f"{stem}.16k.wav"
        run_cmd(
            [
                str(ROOT / "ffmpeg" / "ffmpeg"),
                "-y",
                "-i",
                str(input_path),
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(wav_path),
            ]
        )
    out_prefix = processing_dir / stem
    srt_path = processing_dir / f"{stem}.srt"
    asr_bin = ROOT / str(asr.get("cli_bin", "whisper/whisper-cli"))
    asr_ld_paths = [
        asr_bin.parent,
        asr_bin.parent.parent / "src",
        asr_bin.parent.parent / "ggml" / "src",
        asr_bin.parent.parent / "ggml" / "src" / "ggml-cuda",
    ]
    total_duration = get_media_duration_seconds(wav_path)
    stop_event = threading.Event()
    monitor = threading.Thread(
        target=monitor_asr_progress,
        args=(srt_path, total_duration, stop_event),
        daemon=True,
    )
    monitor.start()
    try:
        run_whisper_cli(
            wav_path,
            out_prefix,
            asr,
            asr_ld_paths,
            use_vad=True,
        )
    finally:
        stop_event.set()
        monitor.join(timeout=3)

    issue = None
    accepted_issue = None
    if asr.get("repair_repeated_segments", True):
        issue, accepted_issue = repair_srt_quality_issues(
            wav_path,
            srt_path,
            asr,
            asr_ld_paths,
            total_duration,
            allow_chunked_repair=True,
            context_label="ASR",
        )

    if not reuse_existing_16k:
        wav_path.unlink(missing_ok=True)
    latest_end, latest_text = read_srt_progress(srt_path)
    if latest_text:
        print(
            f"[ASR] done ({format_seconds(latest_end)}/{format_seconds(total_duration)}) "
            f"{truncate_text(latest_text)}",
            flush=True,
        )
    return srt_path, accepted_issue


def translate_srt(
    input_srt: Path, cfg: dict, output_dir: Path, server_manager: LocalLlamaServer | None = None
) -> Path:
    reset_translation_workspace()
    json_name = f"{input_srt.stem}.json"
    gt_input_json = PROJECT_DIR / "gt_input" / json_name
    gt_output_json = PROJECT_DIR / "gt_output" / json_name
    cache_json = PROJECT_DIR / "transl_cache" / json_name
    make_prompt(str(input_srt), str(gt_input_json))
    total_segments = count_srt_entries(input_srt)

    def run_translation_once(engine: str, use_local_backend: bool) -> None:
        if use_local_backend:
            if server_manager is None:
                raise RuntimeError("local translation selected but no server manager provided")
            server_manager.ensure_started()
            local_cfg = dict(cfg)
            local_translate = dict(cfg.get("translate", {}))
            local_translate["provider"] = "sakura_local"
            local_translate["endpoint"] = local_endpoint(cfg)
            local_cfg["translate"] = local_translate
            sync_project_config(local_cfg)
        else:
            api_key = read_api_key()
            sync_project_config(cfg, api_key)

        project_cfg = CProjectConfig(str(PROJECT_DIR), "config.yaml")
        stop_event = threading.Event()
        monitor = threading.Thread(
            target=monitor_translation_progress,
            args=(cache_json, total_segments, stop_event),
            daemon=True,
        )
        monitor.start()
        try:
            asyncio.run(run_galtransl(project_cfg, engine))
        finally:
            stop_event.set()
            monitor.join(timeout=3)

    primary_is_local = is_local_translation(cfg)
    primary_engine = cfg.get("translate", {}).get(
        "engine",
        "sakura-010" if primary_is_local else ("gpt4" if should_use_gpt4_backend(cfg) else "gpt35-1106"),
    )

    try:
        run_translation_once(primary_engine, primary_is_local)
    except Exception as exc:
        if primary_is_local or not has_local_fallback(cfg):
            raise
        print(f"[WARN] API翻译失败，准备回退本地Sakura: {exc}", flush=True)
        reset_translation_workspace()
        make_prompt(str(input_srt), str(gt_input_json))
        cache_json.unlink(missing_ok=True)
        gt_output_json.unlink(missing_ok=True)
        run_translation_once("sakura-010", True)

    zh_srt = output_dir / f"{input_srt.stem}.zh.srt"
    make_srt(str(gt_output_json), str(zh_srt))
    translated_count, jp_text, zh_text = read_translation_progress(cache_json)
    if translated_count:
        print(
            f"[TRANSLATE] done {translated_count}/{total_segments} "
            f"JP: {truncate_text(jp_text)} | ZH: {truncate_text(zh_text)}",
            flush=True,
        )
    return zh_srt


def process_file(
    path: Path, cfg: dict, paths: dict, server_manager: LocalLlamaServer | None = None
) -> None:
    processing_dir = paths["processing"]
    done_dir = paths["done"]
    failed_dir = paths["failed"]
    outbox = paths["outbox"]

    base_name = path.stem
    output_dir = output_dir_for_base(base_name, outbox)
    processing_path = processing_dir / path.name
    shutil.move(str(path), str(processing_path))

    try:
        accepted_asr_issue = None
        if processing_path.suffix.lower() == ".srt":
            original_srt = processing_path
        else:
            original_srt, accepted_asr_issue = transcribe_to_srt(processing_path, cfg, processing_dir)

        if accepted_asr_issue is not None:
            print(
                "[WARN] continuing with preserved ASR content after retry exhaustion: "
                f"{format_quality_issue(accepted_asr_issue)}",
                flush=True,
            )
        else:
            validate_srt_quality(original_srt)

        jp_srt_out = output_dir / f"{processing_path.stem}.srt"
        shutil.copy2(original_srt, jp_srt_out)
        if cfg.get("translate", {}).get("enabled", True):
            translate_srt(original_srt, cfg, output_dir, server_manager=server_manager)

        shutil.move(str(processing_path), str(done_dir / processing_path.name))
        if original_srt.parent == processing_dir:
            original_srt.unlink(missing_ok=True)
        print(f"[OK] {processing_path.name} -> {output_dir}")
    except Exception as exc:
        print(f"[ERROR] {processing_path.name}: {exc}")
        failed_target = failed_dir / processing_path.name
        if processing_path.exists():
            shutil.move(str(processing_path), str(failed_target))
        raise


def watch_loop(cfg: dict) -> None:
    paths = ensure_dirs(cfg)
    poll_interval = int(cfg.get("watch", {}).get("poll_interval", 5))
    stable_seconds = int(cfg.get("watch", {}).get("stable_seconds", 30))
    server_manager = LocalLlamaServer(cfg) if (is_local_translation(cfg) or has_local_fallback(cfg)) else None

    print(f"[WATCH] inbox={paths['inbox']}")
    try:
        while True:
            candidates = sorted(
                p for p in paths["inbox"].iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS
            )
            for path in candidates:
                age = time.time() - path.stat().st_mtime
                if age < stable_seconds:
                    continue
                try:
                    process_file(path, cfg, paths, server_manager=server_manager)
                except Exception:
                    continue
            time.sleep(poll_interval)
    finally:
        if server_manager:
            server_manager.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    once = sub.add_parser("once")
    once.add_argument("file")

    sub.add_parser("reset")
    sub.add_parser("watch")
    args = parser.parse_args()

    cfg = load_headless_config()
    paths = ensure_dirs(cfg)
    if args.cmd == "reset":
        reset_state(paths)
        return 0
    if args.cmd == "once":
        server_manager = LocalLlamaServer(cfg) if (is_local_translation(cfg) or has_local_fallback(cfg)) else None
        try:
            process_file(Path(args.file).resolve(), cfg, paths, server_manager=server_manager)
        finally:
            if server_manager:
                server_manager.stop()
        return 0

    watch_loop(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
