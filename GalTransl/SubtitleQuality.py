from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pysrt


DEFAULT_CONSECUTIVE_REPEAT_LIMIT = 3
DEFAULT_CHAR_REPEAT_LIMIT = 20


@dataclass(frozen=True)
class SubtitleQualityIssue:
    kind: str
    message: str
    text: str = ""
    start_index: int = 0
    end_index: int = 0
    count: int = 0
    duration_seconds: float = 0.0
    start_time_seconds: float = 0.0
    end_time_seconds: float = 0.0
    char: str = ""
    char_count: int = 0


def normalize_subtitle_text(text: str) -> str:
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").split())


def _most_common_nontrivial_char(text: str) -> tuple[str, int]:
    counter = Counter(ch for ch in text if not ch.isspace())
    for char, count in counter.most_common():
        if char not in {".", "，"}:
            return char, count
    return "", 0


def _subrip_time_to_seconds(subrip_time) -> float:
    return (
        subrip_time.hours * 3600
        + subrip_time.minutes * 60
        + subrip_time.seconds
        + subrip_time.milliseconds / 1000
    )


def find_consecutive_repeat_issue(
    texts: Sequence[str], min_repeat: int = DEFAULT_CONSECUTIVE_REPEAT_LIMIT
) -> SubtitleQualityIssue | None:
    normalized = [normalize_subtitle_text(text) for text in texts]
    if not normalized:
        return None

    run_text = normalized[0]
    run_start = 0
    run_count = 1 if run_text else 0

    for index in range(1, len(normalized)):
        current = normalized[index]
        if current and current == run_text:
            run_count += 1
        else:
            if run_text and run_count >= min_repeat:
                return SubtitleQualityIssue(
                    kind="consecutive_repeat",
                    message=f"第{run_start + 1}句开始连续{run_count}句内容完全相同",
                    text=run_text,
                    start_index=run_start + 1,
                    end_index=run_start + run_count,
                    count=run_count,
                )
            run_text = current
            run_start = index
            run_count = 1 if current else 0

    if run_text and run_count >= min_repeat:
        return SubtitleQualityIssue(
            kind="consecutive_repeat",
            message=f"第{run_start + 1}句开始连续{run_count}句内容完全相同",
            text=run_text,
            start_index=run_start + 1,
            end_index=run_start + run_count,
            count=run_count,
        )
    return None


def inspect_srt_quality(
    srt_path: str | Path,
    min_repeat: int = DEFAULT_CONSECUTIVE_REPEAT_LIMIT,
    char_repeat_limit: int = DEFAULT_CHAR_REPEAT_LIMIT,
) -> SubtitleQualityIssue | None:
    subs = pysrt.open(str(srt_path), encoding="utf-8")

    repeat_text = ""
    repeat_start = 0
    repeat_count = 0
    repeat_start_time = 0.0

    for list_index, sub in enumerate(subs):
        normalized = normalize_subtitle_text(sub.text)
        if not normalized:
            repeat_text = ""
            repeat_start = 0
            repeat_count = 0
            continue

        most_char, char_count = _most_common_nontrivial_char(normalized)
        if most_char and char_count > char_repeat_limit:
            return SubtitleQualityIssue(
                kind="char_repeat",
                message=f"第{sub.index}句中字符'{most_char}'重复{char_count}次",
                text=normalized,
                start_index=sub.index,
                end_index=sub.index,
                count=1,
                duration_seconds=_subrip_time_to_seconds(sub.end)
                - _subrip_time_to_seconds(sub.start),
                start_time_seconds=_subrip_time_to_seconds(sub.start),
                end_time_seconds=_subrip_time_to_seconds(sub.end),
                char=most_char,
                char_count=char_count,
            )

        if normalized == repeat_text:
            repeat_count += 1
        else:
            if repeat_text and repeat_count >= min_repeat:
                end_sub = subs[repeat_start + repeat_count - 1]
                return SubtitleQualityIssue(
                    kind="consecutive_repeat",
                    message=f"第{subs[repeat_start].index}句开始连续{repeat_count}句字幕完全相同",
                    text=repeat_text,
                    start_index=subs[repeat_start].index,
                    end_index=end_sub.index,
                    count=repeat_count,
                    duration_seconds=_subrip_time_to_seconds(end_sub.end)
                    - repeat_start_time,
                    start_time_seconds=repeat_start_time,
                    end_time_seconds=_subrip_time_to_seconds(end_sub.end),
                )
            repeat_text = normalized
            repeat_start = list_index
            repeat_count = 1
            repeat_start_time = _subrip_time_to_seconds(sub.start)

    if repeat_text and repeat_count >= min_repeat:
        end_sub = subs[repeat_start + repeat_count - 1]
        return SubtitleQualityIssue(
            kind="consecutive_repeat",
            message=f"第{subs[repeat_start].index}句开始连续{repeat_count}句字幕完全相同",
            text=repeat_text,
            start_index=subs[repeat_start].index,
            end_index=end_sub.index,
            count=repeat_count,
            duration_seconds=_subrip_time_to_seconds(end_sub.end) - repeat_start_time,
            start_time_seconds=repeat_start_time,
            end_time_seconds=_subrip_time_to_seconds(end_sub.end),
        )
    return None


def format_quality_issue(issue: SubtitleQualityIssue) -> str:
    parts: list[str] = [issue.message]
    if issue.text:
        parts.append(f"内容: {issue.text}")
    if issue.duration_seconds > 0:
        parts.append(f"持续约{issue.duration_seconds:.2f}秒")
    return " | ".join(parts)
