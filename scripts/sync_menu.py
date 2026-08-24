#!/usr/bin/env python3
"""Interactive menu wrapper around sync_images.py.

Guided arrow-key menu for source/destination folders, copy mode, thread
count, and skip folders. Settings persist as JSON so repeated runs need
nothing more than Enter. Zero third-party dependencies; reuses all copy
logic from sync_images.py.

Usage:
    python sync_menu.py [--config PATH]

Keys:
    Arrow Up/Down  move selection (digits jump directly)
    Enter          confirm / enter folder
    Left/Right     parent folder / enter folder (browser)
    Esc            back / quit from main menu
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import sync_images as si

try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import termios
    import tty
except ImportError:
    termios = None

DEFAULT_CONFIG = Path.home() / ".metatrace_sync_menu.json"
MODES = ("all", "final", "manual")
MODE_INFO = {
    "all": "alle erlaubten Bilder unterhalb der Quelle",
    "final": "alle Bilder unter Ordnern mit 'final' im Namen",
    "manual": "in 'manual'-Ordnern alle Bilder, sonst nur *manual*-Bilddateien",
}

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
RED = "\x1b[31m"
RESET = "\x1b[0m"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
FRAME_BLOCK = "░"


@dataclass
class Settings:
    src: str = ""
    dst: str = ""
    mode: str = "final"
    threads: int = 8
    skip_dirs: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        settings = cls(**{k: v for k, v in data.items() if k in known})
        try:
            settings.threads = clamp_threads(int(settings.threads))
        except (TypeError, ValueError):
            settings.threads = 8
        if settings.mode not in MODES:
            settings.mode = "final"
        if not isinstance(settings.skip_dirs, list) or not all(
            isinstance(entry, str) for entry in settings.skip_dirs
        ):
            settings.skip_dirs = []
        return settings

    def save(self, path: Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def clamp_threads(value: int) -> int:
    """Clamp robocopy worker threads to the range the /MT flag accepts."""
    return max(1, min(128, int(value)))


def clean_skip_entry(text: str) -> str | None:
    """Return a source-relative posix skip path, or None when invalid."""
    value = text.strip().replace("\\", "/").strip("/")
    if not value or value == ".":
        return None
    return Path(value).as_posix()


def shorten(text: str, width: int = 58) -> str:
    """Collapse long paths to width with a middle ellipsis."""
    text = text or ""
    if len(text) <= width:
        return text
    keep = width - 1
    left = keep // 2
    return text[:left] + "…" + text[len(text) - (keep - left):]


def clear_screen() -> None:
    """Clear prior menu output before rendering the next menu state."""
    sys.stdout.write("\x1b[H\x1b[2J\x1b[H")
    sys.stdout.flush()


@contextmanager
def alternate_screen():
    """Keep interactive redraws out of the terminal's normal scrollback."""
    sys.stdout.write("\x1b[?1049h")
    clear_screen()
    try:
        yield
    finally:
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()


def is_accessible_directory(path: str) -> bool:
    """Check local, drive-letter, and UNC folders with Windows long-path support."""
    return os.path.isdir(si.longpath(path))


def _read_key() -> str:
    """Read one keypress; return 'up', 'down', 'enter', 'esc' or the character."""
    if msvcrt is not None:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        if ch == "\r":
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    ch = sys.stdin.read(1)
    if ch == "":
        raise EOFError
    if ch == "\x1b":
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not ready:
            return "esc"
        if sys.stdin.read(1) != "[":
            return "esc"
        return {"A": "up", "B": "down"}.get(sys.stdin.read(1), "esc")
    if ch in ("\n", "\r"):
        return "enter"
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch == "\x04":
        raise EOFError
    return ch


@contextmanager
def raw_terminal():
    if termios is None or not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def choose(
    title: str,
    options: list[str],
    preamble: list[str] | None = None,
    framed: bool = False,
) -> int | None:
    """Render an arrow-key selectable list; return index, or None on Esc."""
    selected = 0
    hint = f"{DIM}↑/↓ auswählen · Enter bestätigen · Esc zurück{RESET}"
    with raw_terminal():
        sys.stdout.write("\x1b[?25l")
        try:
            while True:
                rows = [*(preamble or []), f"{BOLD}{title}{RESET}", hint]
                for index, option in enumerate(options):
                    if index == selected:
                        rows.append(f"{CYAN}{BOLD}>{RESET} {BOLD}{option}{RESET}")
                    else:
                        rows.append(f"  {option}")
                if framed:
                    rows = framed_rows(rows)
                clear_screen()
                for row in rows:
                    sys.stdout.write("\r\x1b[2K" + row + "\n")
                sys.stdout.flush()

                key = _read_key()
                if key == "up":
                    selected = (selected - 1) % len(options)
                elif key == "down":
                    selected = (selected + 1) % len(options)
                elif key.isdigit():
                    target = int(key) - 1
                    if 0 <= target < len(options):
                        selected = target
                elif key == "enter":
                    return selected
                elif key == "esc":
                    return None
        finally:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()


MANUAL_INPUT = object()
VIEW_SIZE = 14


def list_subdirectories(path: Path) -> list[str]:
    """Return visible subdirectory names, sorted case-insensitively."""
    try:
        entries = list(os.scandir(path))
    except OSError:
        return []
    names = [
        entry.name
        for entry in entries
        if entry.is_dir()
        and entry.name not in si.SKIP_DIRS
        and not entry.name.startswith(".")
    ]
    return sorted(names, key=str.casefold)


def browse_folder(title: str, start: str) -> str | object | None:
    """Cursor-driven directory browser.

    Returns the selected path, MANUAL_INPUT for typed entry, or None on Esc.
    """
    current = Path(start) if start and Path(start).exists() else Path.home()
    entries = list_subdirectories(current)
    sel = 0
    with raw_terminal():
        sys.stdout.write("\x1b[?25l")
        try:
            while True:
                listing = [".."] + [name + "/" for name in entries]
                sel %= 2 + len(listing)
                if len(listing) > VIEW_SIZE:
                    lo = min(max(0, sel - 2 - VIEW_SIZE // 2), len(listing) - VIEW_SIZE)
                else:
                    lo = 0
                view = listing[lo:lo + VIEW_SIZE]
                while len(view) < VIEW_SIZE:
                    view.append("")
                truncated = f"{DIM}… {len(listing)} Einträge{RESET}" if len(listing) > VIEW_SIZE else ""
                rows = [
                    f"{BOLD}{title}{RESET}",
                    f"{DIM}Pfad: {RESET}{shorten(str(current), 64)}",
                ]
                if sel == 0:
                    rows.append(f"{CYAN}{BOLD}>{RESET} {BOLD}[ Diesen Ordner wählen ]{RESET}")
                else:
                    rows.append("  [ Diesen Ordner wählen ]")
                if sel == 1:
                    rows.append(f"{CYAN}{BOLD}>{RESET} {BOLD}[ Pfad manuell eingeben … ]{RESET}")
                else:
                    rows.append(f"  {DIM}[ Pfad manuell eingeben … ]{RESET}")
                for offset, item in enumerate(view):
                    index = 2 + lo + offset
                    if not item:
                        rows.append("")
                    elif index == sel:
                        rows.append(f"{CYAN}{BOLD}>{RESET} {BOLD}{item}{RESET}")
                    else:
                        rows.append(f"  {item}")
                rows.append(truncated)
                rows = framed_rows(rows)
                clear_screen()
                for row in rows:
                    sys.stdout.write("\r\x1b[2K" + row + "\n")
                sys.stdout.flush()

                key = _read_key()
                if key == "up":
                    sel -= 1
                elif key == "down":
                    sel += 1
                elif key == "enter":
                    if sel == 0:
                        return str(current)
                    if sel == 1:
                        return MANUAL_INPUT
                    target = listing[sel - 2]
                    current = current.parent if target == ".." else current / target[:-1]
                    entries = list_subdirectories(current)
                    sel = 2
                elif key == "esc":
                    return None
                elif key == "left":
                    current = current.parent
                    entries = list_subdirectories(current)
                    sel = 2
                elif key == "right" and sel >= 2 and listing[sel - 2] != "..":
                    current = current / listing[sel - 2][:-1]
                    entries = list_subdirectories(current)
                    sel = 2
        finally:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()


def pick_folder(title: str, current: str) -> str:
    """Browse first; fall back to typed entry via the manual option."""
    picked = browse_folder(title, current)
    if picked is None:
        return current
    if picked is MANUAL_INPUT:
        return ask_path(f"{title} – Pfad", str(browse_folder_start(current)), True)
    return picked


def browse_folder_start(current: str) -> Path:
    return Path(current) if current and Path(current).exists() else Path.home()


def ask_path(prompt: str, current: str, must_exist: bool) -> str:
    """Prompt for a folder; empty input keeps current, bad paths re-ask."""
    shown = shorten(current) if current else "leer"
    while True:
        raw = input(f"{prompt} [{shown}]: ").strip()
        if not raw:
            return current
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if must_exist and not is_accessible_directory(expanded):
            print(f"{RED}Nicht erreichbar: {expanded}{RESET}")
            continue
        return expanded


def pause() -> None:
    try:
        input(f"\n{DIM}Enter zum Fortsetzen …{RESET}")
    except EOFError:
        pass


def summary_rows(settings: Settings) -> list[str]:
    """Return current settings as rows for rendering above the main menu."""
    skips = ", ".join(settings.skip_dirs) if settings.skip_dirs else "–"
    return [
        f"{BOLD}── MetaTrace Sync ──────────────────────────────{RESET}",
        f"  {DIM}Quelle      {RESET}{shorten(settings.src) or '–'}",
        f"  {DIM}Ziel        {RESET}{shorten(settings.dst) or '–'}",
        f"  {DIM}Modus       {RESET}{settings.mode}",
        f"  {DIM}Threads     {RESET}{settings.threads}",
        f"  {DIM}Skip-Ordner {RESET}{skips}",
        "",
    ]


def framed_rows(rows: list[str], width: int | None = None) -> list[str]:
    """Wrap menu rows in a solid terminal-width frame, clipping long text."""
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    width = min(width or 72, terminal_width)
    width = max(20, width)
    content_width = width - 4

    def framed_row(row: str) -> str:
        visible = ANSI_ESCAPE.sub("", row)
        if len(visible) > content_width:
            row = shorten(visible, content_width)
            visible = row
        visible_length = len(visible)
        padding = " " * max(0, content_width - visible_length)
        return f"{CYAN}{FRAME_BLOCK}{RESET} {row}{padding} {CYAN}{FRAME_BLOCK}{RESET}"

    return [
        f"{CYAN}{FRAME_BLOCK * width}{RESET}",
        *(framed_row(row) for row in rows),
        f"{CYAN}{FRAME_BLOCK * width}{RESET}",
    ]


def edit_skip_dirs(settings: Settings) -> None:
    while True:
        clear_screen()
        options = ["+ Ordner hinzufügen"]
        options += [f"− {entry}" for entry in settings.skip_dirs]
        options.append("Fertig")
        choice = choose("Skip-Ordner (quellrelative Pfade)", options, framed=True)
        if choice is None or choice == len(options) - 1:
            return
        if choice == 0:
            raw = input("Zu überspringender Ordner (z. B. archive/old): ").strip()
            cleaned = clean_skip_entry(raw)
            if cleaned is None:
                print(
                    f"{RED}Ungültig oder '.': Unterordner unterhalb der Quelle angeben.{RESET}"
                )
                continue
            existing = {entry.casefold() for entry in settings.skip_dirs}
            if cleaned.casefold() in existing:
                print(f"{DIM}Steht bereits auf der Liste.{RESET}")
                continue
            settings.skip_dirs.append(cleaned)
        else:
            del settings.skip_dirs[choice - 1]


def edit_settings(settings: Settings, config_path: Path) -> None:
    while True:
        clear_screen()
        options = [
            f"Quellordner   {shorten(settings.src) or '–'}",
            f"Zielordner    {shorten(settings.dst) or '–'}",
            f"Modus         {settings.mode}",
            f"Threads       {settings.threads}",
            f"Skip-Ordner   {len(settings.skip_dirs)}",
            "Zurück",
        ]
        choice = choose("Einstellungen", options, framed=True)
        if choice is None or choice == 5:
            return
        if choice == 0:
            settings.src = pick_folder("Quelle (Netzwerkshare)", settings.src)
        elif choice == 1:
            settings.dst = pick_folder("Ziel (lokaler Ordner)", settings.dst)
        elif choice == 2:
            picked = choose(
                "Modus", [f"{m} – {MODE_INFO[m]}" for m in MODES], framed=True
            )
            if picked is not None:
                settings.mode = MODES[picked]
        elif choice == 3:
            raw = input(f"Threads 1-128 [{settings.threads}]: ").strip()
            if raw.isdigit():
                settings.threads = clamp_threads(int(raw))
        elif choice == 4:
            edit_skip_dirs(settings)
        settings.save(config_path)


def run_flow(settings: Settings, dry_run: bool) -> None:
    src, dst = Path(settings.src), Path(settings.dst)
    if not settings.src or not src.is_dir():
        print(f"{RED}Fehler: Quelle nicht erreichbar: {settings.src or '(leer)'}{RESET}")
        pause()
        return
    if dst.exists() and not dst.is_dir():
        print(f"{RED}Fehler: Ziel existiert und ist kein Verzeichnis: {dst}{RESET}")
        pause()
        return
    if not dry_run:
        dst.mkdir(parents=True, exist_ok=True)
    try:
        skip_dirs = si.normalize_skip_dirs(settings.skip_dirs)
    except ValueError as exc:
        print(f"{RED}Fehler: {exc}{RESET}")
        pause()
        return

    label = "TESTLAUF — es wird nichts kopiert" if dry_run else "KOPIEREN"
    print(f"\n{BOLD}{label}{RESET}\n")
    stats = si.Stats()
    started = time.time()
    try:
        si.copy_matching_folders(
            src,
            dst,
            settings.mode,
            clamp_threads(settings.threads),
            dry_run,
            stats,
            skip_dirs,
        )
    except KeyboardInterrupt:
        print("\nAbgebrochen — Zwischenstand:")
    duration = time.time() - started

    print("-" * 60)
    print(f"Modus   : {settings.mode}" + (" (dry-run)" if dry_run else ""))
    print(f"Ordner  : {stats.folders}")
    print(f"Fehler  : {stats.failed}")
    for error in stats.errors[:20]:
        print(f"  ERROR {error}")
    if len(stats.errors) > 20:
        print(f"  ... und {len(stats.errors) - 20} weitere")
    print(f"Dauer   : {duration:.1f}s")
    pause()


def _enable_windows_ansi() -> None:
    """Enable ANSI escape sequence handling in Windows consoles."""
    os.system("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        metavar="PATH",
        help=f"settings file (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args(argv)

    if not si.IS_WINDOWS:
        parser.error("this robocopy-based tool requires Windows")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error("an interactive terminal is required")

    if msvcrt is not None:
        _enable_windows_ansi()

    settings = Settings.load(args.config)

    with alternate_screen():
        while True:
            choice = choose(
                "Hauptmenü",
                [
                    "Testlauf starten (Dry-Run)",
                    "Kopieren starten",
                    "Einstellungen bearbeiten",
                    "Beenden",
                ],
                preamble=summary_rows(settings),
                framed=True,
            )
            if choice in (None, 3):
                return 0
            if choice == 0:
                run_flow(settings, dry_run=True)
            elif choice == 1:
                run_flow(settings, dry_run=False)
            elif choice == 2:
                edit_settings(settings, args.config)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nAbgebrochen.")
        sys.exit(130)
