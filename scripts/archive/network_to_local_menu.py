#!/usr/bin/env python3
"""Interactive menu wrapper around network_to_local_copy.py.

Arrow-key menu for source/destination folders, excluded folder depth,
excluded folder names, and image extensions. Settings persist as JSON.
Terminal primitives (alternate screen buffer, key handling, framed rows)
are reused from sync_menu.py.

Usage:
    python network_to_local_menu.py [--config PATH]

Keys:
    Arrow Up/Down  move selection (digits jump directly)
    Enter          confirm / enter folder
    Esc            back / quit from main menu
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for module_dir in (_HERE, _HERE.parent):
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

import network_to_local_copy as nc
import sync_menu as sm

DEFAULT_CONFIG = Path.home() / ".metatrace_network_copy.json"
MAX_EXCLUDE_LEVELS = 16
DEFAULT_PRESET_NAME = "Standard"
FRAME_WIDTH = 122
SUMMARY_LABEL_WIDTH = 17
OPTION_LABEL_WIDTH = 15

# Captured before apply_settings() can overwrite the copy module's globals.
DEFAULT_SRC = nc.SHARE_ROOT
DEFAULT_EXCLUDE_LEVELS = nc.EXCLUDED_DIR_LEVELS
DEFAULT_EXCLUDE_PATHS = sorted(nc.EXCLUDED_SCAN_PATHS)
DEFAULT_EXCLUDE_DIRS = sorted(nc.EXCLUDED_DIR_NAMES)
DEFAULT_EXTENSIONS = sorted(nc.EXTENSIONS)
DEFAULT_MAX_FILE_SIZE_MB = nc.MAX_FILE_SIZE_MB


@dataclass
class Settings:
    src: str = DEFAULT_SRC
    dst: str = ""
    exclude_levels: int = DEFAULT_EXCLUDE_LEVELS
    exclude_paths: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_PATHS)
    )
    exclude_dirs: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS)
    )
    extensions: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXTENSIONS)
    )
    max_file_size_mb: int = DEFAULT_MAX_FILE_SIZE_MB

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
        settings.normalize()
        return settings

    def normalize(self) -> None:
        try:
            self.exclude_levels = clamp_levels(int(self.exclude_levels))
        except (TypeError, ValueError):
            self.exclude_levels = DEFAULT_EXCLUDE_LEVELS
        self.exclude_paths = _clean_list(self.exclude_paths, clean_exclude_path)
        self.exclude_dirs = _clean_list(self.exclude_dirs, clean_dir_name)
        self.extensions = _clean_list(self.extensions, clean_extension)
        try:
            self.max_file_size_mb = max(0, int(self.max_file_size_mb))
        except (TypeError, ValueError):
            self.max_file_size_mb = DEFAULT_MAX_FILE_SIZE_MB


@dataclass
class PresetConfig:
    active_preset: str = DEFAULT_PRESET_NAME
    presets: dict[str, Settings] = field(
        default_factory=lambda: {DEFAULT_PRESET_NAME: Settings()}
    )

    @classmethod
    def load(cls, path: Path) -> "PresetConfig":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()

        setting_fields = {f.name for f in fields(Settings)}
        if setting_fields & set(data):
            settings = Settings(**{k: v for k, v in data.items() if k in setting_fields})
            settings.normalize()
            return cls(presets={DEFAULT_PRESET_NAME: settings})

        raw_presets = data.get("presets")
        presets = {}
        if isinstance(raw_presets, dict):
            for name, values in raw_presets.items():
                if not isinstance(name, str) or not isinstance(values, dict):
                    continue
                settings = Settings(
                    **{k: v for k, v in values.items() if k in setting_fields}
                )
                settings.normalize()
                presets[name] = settings
        if not presets:
            presets = {DEFAULT_PRESET_NAME: Settings()}

        active_preset = data.get("active_preset")
        if active_preset not in presets:
            active_preset = next(iter(sorted(presets)))
        return cls(active_preset=active_preset, presets=presets)

    def save(self, path: Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_preset": self.active_preset,
            "presets": {
                name: asdict(settings)
                for name, settings in sorted(self.presets.items())
            },
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def current_settings(self) -> Settings:
        return self.presets[self.active_preset]

    def current_name(self) -> str:
        return self.active_preset

    def ensure_valid(self) -> None:
        if not self.presets:
            self.presets[DEFAULT_PRESET_NAME] = Settings()
        if self.active_preset not in self.presets:
            self.active_preset = next(iter(sorted(self.presets)))


def _clean_list(values, cleaner) -> list[str]:
    """Return sorted, de-duplicated entries, dropping anything invalid."""
    if not isinstance(values, list):
        return []
    cleaned = {cleaner(v) for v in values if isinstance(v, str)}
    cleaned.discard(None)
    return sorted(cleaned)


def clamp_levels(value: int) -> int:
    return max(1, min(MAX_EXCLUDE_LEVELS, int(value)))


def clean_dir_name(text: str) -> str | None:
    """Return a bare, case-folded folder name, or None when invalid."""
    value = text.strip().strip("/\\").casefold()
    if not value or value == "." or "/" in value or "\\" in value:
        return None
    return value


def clean_exclude_path(text: str) -> str | None:
    """Return a source-relative path, or None when invalid."""
    return sm.clean_skip_entry(text)


def clean_extension(text: str) -> str | None:
    """Return a lowercase extension with leading dot, or None when invalid."""
    value = text.strip().lower().lstrip("*")
    if not value:
        return None
    if not value.startswith("."):
        value = "." + value
    if len(value) < 2 or any(c in value[1:] for c in "./\\ "):
        return None
    return value


def apply_settings(settings: Settings) -> None:
    """Push menu settings into the copy module's configuration globals."""
    nc.EXCLUDED_DIR_LEVELS = settings.exclude_levels
    nc.EXCLUDED_SCAN_PATHS = set(settings.exclude_paths)
    nc.EXCLUDED_DIR_NAMES = set(settings.exclude_dirs)
    nc.EXTENSIONS = set(settings.extensions)
    nc.MAX_FILE_SIZE_MB = settings.max_file_size_mb


def clean_preset_name(text: str) -> str | None:
    """Return a non-empty preset name, or None when invalid."""
    value = text.strip()
    return value or None


def format_summary_row(label: str, value: str) -> str:
    """Return one aligned summary row for the menu preamble."""
    return f"  {sm.DIM}{label:<{SUMMARY_LABEL_WIDTH}}{sm.RESET} {value}"


def format_option_row(label: str, value: str) -> str:
    """Return one aligned option row for settings menus."""
    return f"{label:<{OPTION_LABEL_WIDTH}} {value}"


def summary_rows(config: PresetConfig) -> list[str]:
    settings = config.current_settings()
    paths = ", ".join(settings.exclude_paths) if settings.exclude_paths else "–"
    dirs = ", ".join(settings.exclude_dirs) if settings.exclude_dirs else "–"
    exts = " ".join(settings.extensions) if settings.extensions else "–"
    max_mb = f"{settings.max_file_size_mb} MB" if settings.max_file_size_mb else "ohne Limit"
    return [
        f"{sm.BOLD}── MetaTrace Netzwerk-Kopie ────────────────────{sm.RESET}",
        format_summary_row("Preset", config.current_name()),
        format_summary_row("Quelle", sm.shorten(settings.src, 96) or "–"),
        format_summary_row("Ziel", sm.shorten(settings.dst, 96) or "–"),
        format_summary_row("Exclude-Ebenen", str(settings.exclude_levels)),
        format_summary_row("Exclude-Pfade", sm.shorten(paths, 96)),
        format_summary_row("Exclude-Ordner", sm.shorten(dirs, 96)),
        format_summary_row("Extensions", sm.shorten(exts, 96)),
        format_summary_row("Max-Dateigröße", max_mb),
        "",
    ]


def edit_list(title: str, entries: list[str], prompt: str, cleaner) -> None:
    """Add or remove entries of a string list in place."""
    while True:
        sm.clear_screen()
        options = ["+ Eintrag hinzufügen"]
        options += [f"− {entry}" for entry in entries]
        options.append("Fertig")
        choice = sm.choose(title, options, framed=True, frame_width=FRAME_WIDTH)
        if choice is None or choice == len(options) - 1:
            return
        if choice == 0:
            cleaned = cleaner(input(f"{prompt}: ").strip())
            if cleaned is None:
                print(f"{sm.RED}Ungültige Eingabe.{sm.RESET}")
                sm.pause()
                continue
            if cleaned in entries:
                print(f"{sm.DIM}Steht bereits auf der Liste.{sm.RESET}")
                sm.pause()
                continue
            entries.append(cleaned)
            entries.sort()
        else:
            del entries[choice - 1]


def edit_settings(settings: Settings, config: PresetConfig, config_path: Path) -> None:
    while True:
        sm.clear_screen()
        options = [
            format_option_row("Quellordner", sm.shorten(settings.src, 90) or "–"),
            format_option_row("Zielordner", sm.shorten(settings.dst, 90) or "–"),
            format_option_row("Exclude-Ebenen", str(settings.exclude_levels)),
            format_option_row("Exclude-Pfade", str(len(settings.exclude_paths))),
            format_option_row("Exclude-Ordner", str(len(settings.exclude_dirs))),
            format_option_row("Extensions", str(len(settings.extensions))),
            format_option_row(
                "Max-Dateigröße",
                str(settings.max_file_size_mb) if settings.max_file_size_mb else "ohne Limit",
            ),
            "Zurück",
        ]
        choice = sm.choose("Einstellungen", options, framed=True, frame_width=FRAME_WIDTH)
        if choice is None or choice == 7:
            return
        if choice == 0:
            settings.src = sm.pick_folder(
                "Quelle (Netzwerkshare)", settings.src, frame_width=FRAME_WIDTH
            )
        elif choice == 1:
            settings.dst = sm.pick_folder(
                "Ziel (lokaler Ordner)",
                settings.dst,
                frame_width=FRAME_WIDTH,
                must_exist=False,
            )
        elif choice == 2:
            raw = input(
                f"Exclude-Ebenen 1-{MAX_EXCLUDE_LEVELS} [{settings.exclude_levels}]: "
            ).strip()
            if raw.isdigit():
                settings.exclude_levels = clamp_levels(int(raw))
        elif choice == 3:
            edit_list(
                "Exclude-Pfade",
                settings.exclude_paths,
                "Quellrelativer Pfad (z. B. _tmp oder archive/old)",
                clean_exclude_path,
            )
        elif choice == 4:
            edit_list(
                "Exclude-Ordnernamen",
                settings.exclude_dirs,
                "Ordnername (z. B. textures)",
                clean_dir_name,
            )
        elif choice == 5:
            edit_list(
                "Extensions",
                settings.extensions,
                "Extension (z. B. .webp)",
                clean_extension,
            )
        elif choice == 6:
            raw = input(
                f"Max-Dateigröße in MB (0 = ohne Limit) "
                f"[{settings.max_file_size_mb}]: "
            ).strip()
            if raw.isdigit():
                settings.max_file_size_mb = max(0, int(raw))
        config.save(config_path)


def choose_preset(config: PresetConfig, config_path: Path) -> None:
    names = sorted(config.presets)
    options = [
        f"{name} {'(aktiv)' if name == config.active_preset else ''}".rstrip()
        for name in names
    ]
    choice = sm.choose("Preset wählen", options, framed=True, frame_width=FRAME_WIDTH)
    if choice is None:
        return
    config.active_preset = names[choice]
    apply_settings(config.current_settings())
    config.save(config_path)


def save_preset_as(config: PresetConfig, config_path: Path) -> None:
    raw = input(f"Preset-Name [{config.current_name()} Kopie]: ").strip()
    name = clean_preset_name(raw) or f"{config.current_name()} Kopie"
    if name in config.presets:
        print(f"{sm.RED}Preset existiert bereits.{sm.RESET}")
        sm.pause()
        return
    config.presets[name] = Settings(**asdict(config.current_settings()))
    config.active_preset = name
    config.save(config_path)


def delete_preset(config: PresetConfig, config_path: Path) -> None:
    if len(config.presets) <= 1:
        print(f"{sm.DIM}Mindestens ein Preset muss erhalten bleiben.{sm.RESET}")
        sm.pause()
        return
    names = sorted(config.presets)
    choice = sm.choose(
        "Preset löschen",
        names + ["Abbrechen"],
        framed=True,
        frame_width=FRAME_WIDTH,
    )
    if choice is None or choice == len(names):
        return
    deleted = names[choice]
    del config.presets[deleted]
    config.ensure_valid()
    apply_settings(config.current_settings())
    config.save(config_path)


def run_flow(settings: Settings) -> None:
    if not settings.src or not sm.is_accessible_directory(settings.src):
        print(f"{sm.RED}Fehler: Quelle nicht erreichbar: {settings.src or '(leer)'}{sm.RESET}")
        sm.pause()
        return
    if not settings.dst:
        print(f"{sm.RED}Fehler: Kein Zielordner gesetzt.{sm.RESET}")
        sm.pause()
        return
    destination = Path(settings.dst)
    if destination.exists() and not destination.is_dir():
        print(f"{sm.RED}Fehler: Ziel ist kein Verzeichnis: {destination}{sm.RESET}")
        sm.pause()
        return
    if not settings.extensions:
        print(f"{sm.RED}Fehler: Keine Extensions konfiguriert.{sm.RESET}")
        sm.pause()
        return

    apply_settings(settings)
    destination.mkdir(parents=True, exist_ok=True)
    with sm.suspend_alternate_screen():
        try:
            nc.run(settings.src, settings.dst)
        except KeyboardInterrupt:
            print("\nAbgebrochen.")
        except RuntimeError as exc:
            print(f"{sm.RED}Fehler: {exc}{sm.RESET}")
        except SystemExit as exc:
            print(f"{sm.RED}Abgebrochen: {exc}{sm.RESET}")
        sm.pause()


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

    if not nc.IS_WINDOWS:
        parser.error("this robocopy-based tool requires Windows")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error("an interactive terminal is required")

    if sm.msvcrt is not None:
        sm._enable_windows_ansi()

    config = PresetConfig.load(args.config)
    config.ensure_valid()
    apply_settings(config.current_settings())

    with sm.alternate_screen():
        while True:
            settings = config.current_settings()
            choice = sm.choose(
                "Hauptmenü",
                [
                    "Kopieren starten",
                    "Preset wählen",
                    "Preset speichern unter ...",
                    "Preset löschen",
                    "Einstellungen bearbeiten",
                    "Beenden",
                ],
                preamble=summary_rows(config),
                framed=True,
                frame_width=FRAME_WIDTH,
            )
            if choice in (None, 5):
                return 0
            if choice == 0:
                run_flow(settings)
            elif choice == 1:
                choose_preset(config, args.config)
            elif choice == 2:
                save_preset_as(config, args.config)
            elif choice == 3:
                delete_preset(config, args.config)
            else:
                edit_settings(settings, config, args.config)
                config.save(args.config)


if __name__ == "__main__":
    sys.exit(main())
