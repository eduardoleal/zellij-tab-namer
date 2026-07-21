"""Pure naming policy for Zellij pane state."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shlex
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple


Compressor = Callable[[str, int], Optional[str]]

_DEFAULT_TAB_RE = re.compile(r"^(?:Tab #\d+|\d+)$")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NamerConfig:
    max_chars: int = 32
    fallback_separator: str = " - "


@dataclass(frozen=True)
class RenameDecision:
    tab_id: int
    current_name: str
    candidate: str
    reason: str
    skipped: bool = False
    skip_reason: Optional[str] = None


def plan_renames(
    panes: Iterable[Mapping[str, Any]],
    state: Optional[Mapping[str, Any]] = None,
    config: Optional[NamerConfig] = None,
    compressor: Optional[Compressor] = None,
    force: bool = False,
) -> Tuple[List[RenameDecision], Dict[str, Dict[str, str]]]:
    """Return rename decisions and the generated-name state after applying them."""

    config = config or NamerConfig()
    generated = dict((state or {}).get("generated", {}))
    next_state = {"generated": generated}
    grouped = _group_panes_by_tab(panes)
    decisions: List[RenameDecision] = []

    for tab_id in sorted(grouped):
        tab_panes = grouped[tab_id]
        chosen = choose_pane(tab_panes)
        if chosen is None:
            continue

        current_name = _string(chosen.get("tab_name"))
        candidate, reason = label_for_pane(chosen, config, compressor=compressor)
        if not candidate:
            decisions.append(
                RenameDecision(
                    tab_id=tab_id,
                    current_name=current_name,
                    candidate="",
                    reason="none",
                    skipped=True,
                    skip_reason="no_candidate",
                )
            )
            continue

        tab_key = str(tab_id)
        last_generated = generated.get(tab_key)
        manual_override = (
            not force
            and not is_default_tab_name(current_name)
            and current_name != last_generated
            and current_name != candidate
        )

        if manual_override:
            decisions.append(
                RenameDecision(
                    tab_id=tab_id,
                    current_name=current_name,
                    candidate=candidate,
                    reason=reason,
                    skipped=True,
                    skip_reason="manual_override",
                )
            )
            continue

        if current_name == candidate:
            generated[tab_key] = candidate
            decisions.append(
                RenameDecision(
                    tab_id=tab_id,
                    current_name=current_name,
                    candidate=candidate,
                    reason=reason,
                    skipped=True,
                    skip_reason="already_named",
                )
            )
            continue

        generated[tab_key] = candidate
        decisions.append(
            RenameDecision(
                tab_id=tab_id,
                current_name=current_name,
                candidate=candidate,
                reason=reason,
            )
        )

    return decisions, next_state


def choose_pane(panes: Iterable[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    valid = [pane for pane in panes if _is_eligible_pane(pane)]
    if not valid:
        return None
    return max(valid, key=_pane_score)


def label_for_pane(
    pane: Mapping[str, Any],
    config: NamerConfig,
    compressor: Optional[Compressor] = None,
) -> Tuple[str, str]:
    title = clean_title(_string(pane.get("title")))
    command = command_name(_string(pane.get("pane_command")))

    if is_useful_title(title, command):
        return _compress_or_shorten(title, config.max_chars, compressor), "title"

    fallback = config.fallback_separator.join(
        part for part in (project_name(_string(pane.get("pane_cwd"))), command) if part
    )
    if fallback:
        return shorten_label(fallback, config.max_chars), "fallback"
    return "", "none"


def clean_title(title: str) -> str:
    collapsed = _SPACE_RE.sub(" ", title).strip()
    while collapsed and not collapsed[0].isalnum() and collapsed[0] not in {"~", "/", "."}:
        collapsed = collapsed[1:].lstrip()
    return collapsed


def shorten_label(label: str, max_chars: int) -> str:
    normalized = _SPACE_RE.sub(" ", label).strip()
    if max_chars < 1:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return normalized[:max_chars]

    limit = max_chars - 3
    prefix = normalized[:limit].rstrip()
    word_cut = prefix.rfind(" ")
    if word_cut >= max(8, limit // 2):
        prefix = prefix[:word_cut].rstrip()
    if not prefix:
        prefix = normalized[:limit]
    return f"{prefix}..."


def is_default_tab_name(name: str) -> bool:
    stripped = name.strip()
    return not stripped or bool(_DEFAULT_TAB_RE.match(stripped))


def is_useful_title(title: str, command: str = "") -> bool:
    if not title:
        return False

    lower_title = title.lower()
    lower_command = command.lower()
    generic_titles = {
        "bash",
        "zsh",
        "fish",
        "sh",
        "vim",
        "nvim",
        "hx",
        "helix",
        "claude code",
        "codex",
    }
    if lower_title in generic_titles:
        return False
    if lower_command and lower_title == lower_command:
        return False
    if _looks_like_path(title):
        return False
    return True


def command_name(command: str) -> str:
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    return os.path.basename(parts[0]) or parts[0]


def project_name(cwd: str) -> str:
    if not cwd:
        return ""
    stripped = cwd.rstrip("/")
    if not stripped:
        return ""
    return os.path.basename(stripped)


def _group_panes_by_tab(panes: Iterable[Mapping[str, Any]]) -> Dict[int, List[Mapping[str, Any]]]:
    grouped: Dict[int, List[Mapping[str, Any]]] = {}
    for pane in panes:
        tab_id = pane.get("tab_id")
        if tab_id is None:
            continue
        try:
            tab_id_int = int(tab_id)
        except (TypeError, ValueError):
            continue
        grouped.setdefault(tab_id_int, []).append(pane)
    return grouped


def _is_eligible_pane(pane: Mapping[str, Any]) -> bool:
    return not bool(pane.get("is_plugin")) and not bool(pane.get("exited"))


def _pane_score(pane: Mapping[str, Any]) -> int:
    title = clean_title(_string(pane.get("title")))
    command = command_name(_string(pane.get("pane_command")))
    if is_useful_title(title, command):
        score = 100
    elif title and _looks_like_path(title):
        score = 20
    else:
        score = 10

    if bool(pane.get("is_focused")):
        score += 3
    if not bool(pane.get("is_floating")):
        score += 2

    return score


def _compress_or_shorten(
    label: str, max_chars: int, compressor: Optional[Compressor]
) -> str:
    if len(label) <= max_chars:
        return label
    if compressor is not None:
        compressed = compressor(label, max_chars)
        if compressed:
            cleaned = clean_title(compressed)
            if cleaned and len(cleaned) <= max_chars:
                return cleaned
    return shorten_label(label, max_chars)


def _looks_like_path(title: str) -> bool:
    return (
        title.startswith("~/")
        or title.startswith("/")
        or "/." in title
        or ("/" in title and " " not in title)
    )


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value)
