#!/usr/bin/env python3
"""
Modes:
1. decode:
   {{ source('schema', 'table_id') }} -> database.table_name

2. encode:
   database.table_name -> {{ source('schema', 'table_id') }}

Config resolution order:
1. --config
2. --config-dir with interactive selection
3. lib/config/db_v2/prod.yaml
4. fail if nothing is found

Examples:
    python table_name_codec.py decode \
        --input insert.sql \
        --output insert_decoded.sql

    python table_name_codec.py encode \
        --config lib/config/db_v2/dev.yaml \
        --input insert_decoded.sql \
        --output insert_encoded.sql

    python table_name_codec.py decode \
        --config-dir ./configs \
        --input insert.sql \
        --in-place
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml


SOURCE_RE = re.compile(
    r"""
    \{\{\s*source\(
        \s*(?P<q1>['"])(?P<schema>[^'"]+)(?P=q1)
        \s*,\s*
        (?P<q2>['"])(?P<table>[^'"]+)(?P=q2)
        \s*
    \)\s*\}\}
    """,
    re.VERBOSE,
)

# For decode, protect comments only, because source(...) contains string literals inside it
COMMENTS_RE = re.compile(
    r"(--[^\n]*|/\*.*?\*/)",
    re.DOTALL,
)

# For encode, protect both strings and comments to avoid replacing inside literals
STRINGS_AND_COMMENTS_RE = re.compile(
    r"('(?:''|\\'|[^'])*'|--[^\n]*|/\*.*?\*/)",
    re.DOTALL,
)

# Matches chains like:
#   db.table
#   db.schema.table
#   `db`.`table`
#   "db"."table"
#   active-axle-335414.analytics.main_data_prod
IDENTIFIER_CHAIN_RE = re.compile(
    r'(?<![\w`"])'
    r'(?P<name>'
    r'(?:`[^`]+`|"[^"]+"|[A-Za-z_][A-Za-z0-9_-]*)'
    r'(?:\.(?:`[^`]+`|"[^"]+"|[A-Za-z_][A-Za-z0-9_-]*))+'
    r')'
    r'(?![\w`"])'
)


class ConfigError(Exception):
    pass


class DecodeError(Exception):
    pass


class EncodeError(Exception):
    pass


@dataclass(frozen=True)
class TableRef:
    source_name: str
    table_id: str
    database: str
    physical_name: str

    @property
    def physical_full_name(self) -> str:
        return f"{self.database}.{self.physical_name}"


def load_table_refs(config_path: Path) -> list[TableRef]:
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ConfigError(f"Invalid YAML: expected a dict at root level, file: {config_path}")

    sources = data.get("sources")
    if not isinstance(sources, list):
        raise ConfigError(f"Missing 'sources' list in YAML, file: {config_path}")

    refs: list[TableRef] = []

    for source in sources:
        if not isinstance(source, dict):
            raise ConfigError("Each item in 'sources' must be an object")

        source_name = source.get("name")
        database = source.get("database")
        tables = source.get("tables", [])

        if not source_name or not database:
            raise ConfigError(
                f"Each source must have both 'name' and 'database': {source}"
            )

        if not isinstance(tables, list):
            raise ConfigError(f"'tables' must be a list for source={source_name}")

        for table in tables:
            if not isinstance(table, dict):
                raise ConfigError(
                    f"Each entry in tables must be an object. source={source_name}"
                )

            table_id = table.get("id")
            if not table_id:
                raise ConfigError(
                    f"Each table must have an 'id'. source={source_name}, table={table}"
                )

            physical_name = table.get("name") or table_id

            refs.append(
                TableRef(
                    source_name=source_name,
                    table_id=table_id,
                    database=database,
                    physical_name=physical_name,
                )
            )

    return refs


def list_yaml_files(config_dir: Path) -> list[Path]:
    if not config_dir.exists():
        raise ConfigError(f"Config directory not found: {config_dir}")

    if not config_dir.is_dir():
        raise ConfigError(f"Expected a config directory, got: {config_dir}")

    yaml_files = [*config_dir.glob("*.yaml"), *config_dir.glob("*.yml")]
    yaml_files = sorted((path.resolve() for path in yaml_files), key=lambda p: p.stem.lower())

    if not yaml_files:
        raise ConfigError(f"No YAML config files found in directory: {config_dir}")

    return yaml_files


def resolve_custom_config_path(raw_path: str) -> Path:
    custom_path = Path(raw_path).expanduser()

    if not custom_path.exists():
        raise ConfigError(f"Custom config file not found: {custom_path}")

    if not custom_path.is_file():
        raise ConfigError(f"Custom path is not a file: {custom_path}")

    return custom_path.resolve()


def select_config_from_dir(config_dir: Path) -> Path:
    yaml_files = list_yaml_files(config_dir)

    print("Available configs:")
    for i, path in enumerate(yaml_files, start=1):
        print(f"{i}. {path.stem}")

    user_input = input("Enter config number or provide a custom file path: ").strip()

    if not user_input:
        raise ConfigError("No config selected")

    if user_input.isdigit():
        idx = int(user_input)
        if not (1 <= idx <= len(yaml_files)):
            raise ConfigError(
                f"Invalid config number: {idx}. Expected a value from 1 to {len(yaml_files)}"
            )
        return yaml_files[idx - 1]

    return resolve_custom_config_path(user_input)


def resolve_config_path(
    explicit_config: Path | None,
    config_dir: Path | None,
    default_config: Path = Path("lib/config/db_v2/prod.yaml"),
) -> Path:
    if explicit_config is not None:
        config_path = explicit_config.expanduser()

        if not config_path.exists():
            raise ConfigError(f"Config file not found: {config_path}")

        if not config_path.is_file():
            raise ConfigError(f"Config path is not a file: {config_path}")

        return config_path.resolve()

    if config_dir is not None:
        return select_config_from_dir(config_dir)

    default_path = default_config.expanduser()

    if default_path.exists():
        if not default_path.is_file():
            raise ConfigError(f"Default config path is not a file: {default_path}")
        return default_path.resolve()

    raise ConfigError(
        "No config was found. Provide --config, provide --config-dir, "
        f"or ensure the default config exists: {default_config}"
    )


def build_forward_map(refs: list[TableRef]) -> dict[tuple[str, str], str]:
    """
    (source_name, table_id) -> database.table_name
    """
    forward: dict[tuple[str, str], str] = {}
    duplicates: list[str] = []

    for ref in refs:
        key = (ref.source_name, ref.table_id)
        value = ref.physical_full_name

        if key in forward and forward[key] != value:
            duplicates.append(
                f"{ref.source_name}.{ref.table_id}: {forward[key]} <> {value}"
            )

        forward[key] = value

    if duplicates:
        joined = "\n".join(duplicates)
        raise ConfigError(
            "Found conflicting mappings in YAML for the same source/id:\n"
            f"{joined}"
        )

    return forward


def build_reverse_maps(
    refs: list[TableRef],
) -> tuple[dict[str, TableRef], dict[str, list[TableRef]]]:
    """
    unique_map:
        database.table_name -> TableRef
    ambiguous_map:
        database.table_name -> [TableRef, TableRef, ...]
    """
    grouped: dict[str, list[TableRef]] = defaultdict(list)

    for ref in refs:
        grouped[ref.physical_full_name].append(ref)

    unique_map: dict[str, TableRef] = {}
    ambiguous_map: dict[str, list[TableRef]] = {}

    for physical_name, ref_list in grouped.items():
        if len(ref_list) == 1:
            unique_map[physical_name] = ref_list[0]
        else:
            ambiguous_map[physical_name] = ref_list

    return unique_map, ambiguous_map


def replace_outside_protected(
    text: str,
    protected_re: re.Pattern[str],
    replacer: Callable[[str], str],
) -> str:
    """
    Apply replacer only to unprotected parts of the text.
    Protected parts (strings/comments) are preserved as is.
    """
    result: list[str] = []
    last = 0

    for match in protected_re.finditer(text):
        result.append(replacer(text[last:match.start()]))
        result.append(match.group(0))
        last = match.end()

    result.append(replacer(text[last:]))
    return "".join(result)


def normalize_identifier_chain(raw: str) -> str:
    """
    Normalize:
      poker.core_table
      `poker`.`core_table`
      "poker"."core_table"
    into:
      poker.core_table
    """
    return ".".join(part.strip('`"') for part in raw.split("."))


def decode_sql(
    sql_text: str,
    forward_map: dict[tuple[str, str], str],
    skip_missing_sources: bool = False,
) -> str:
    def replace_chunk(chunk: str) -> str:
        def replace_source(match: re.Match[str]) -> str:
            key = (match.group("schema"), match.group("table"))

            if key not in forward_map:
                if skip_missing_sources:
                    return match.group(0)
                raise DecodeError(
                    f"Mapping not found in YAML for source('{key[0]}', '{key[1]}')"
                )

            return forward_map[key]

        return SOURCE_RE.sub(replace_source, chunk)

    return replace_outside_protected(sql_text, COMMENTS_RE, replace_chunk)


def encode_sql(
    sql_text: str,
    reverse_map: dict[str, TableRef],
    ambiguous_map: dict[str, list[TableRef]],
) -> str:
    def replace_chunk(chunk: str) -> str:
        def replace_identifier(match: re.Match[str]) -> str:
            raw = match.group("name")
            normalized = normalize_identifier_chain(raw)

            if normalized in ambiguous_map:
                variants = ", ".join(
                    f"{ref.source_name}.{ref.table_id}"
                    for ref in ambiguous_map[normalized]
                )
                raise EncodeError(
                    "Ambiguous reverse mapping found for "
                    f"'{normalized}'. Possible variants: {variants}"
                )

            ref = reverse_map.get(normalized)
            if ref is None:
                return raw

            return "{{ source('%s', '%s') }}" % (ref.source_name, ref.table_id)

        return IDENTIFIER_CHAIN_RE.sub(replace_identifier, chunk)

    return replace_outside_protected(sql_text, STRINGS_AND_COMMENTS_RE, replace_chunk)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Encode or decode table names using a YAML config."
    )

    parser.add_argument(
        "mode",
        choices=["decode", "encode"],
        help="decode: source(...) -> database.table, encode: database.table -> source(...)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to a specific YAML config file",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        help="Directory with YAML config files for interactive selection",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to the input SQL file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Path to the output SQL file. If omitted and --in-place is not set, the result is printed to stdout",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input file in place",
    )
    parser.add_argument(
        "--skip-missing-sources",
        action="store_true",
        help="Decode only: skip unknown source(...) mappings instead of failing",
    )

    args = parser.parse_args()

    if args.output and args.in_place:
        parser.error("Cannot use --output and --in-place at the same time")

    return args


def main() -> int:
    args = parse_args()

    try:
        config_path = resolve_config_path(
            explicit_config=args.config,
            config_dir=args.config_dir,
        )
        print(f"Selected config: {config_path}")

        refs = load_table_refs(config_path)
        forward_map = build_forward_map(refs)
        reverse_map, ambiguous_map = build_reverse_maps(refs)

        sql_text = args.input.read_text(encoding="utf-8")

        if args.mode == "decode":
            result = decode_sql(
                sql_text=sql_text,
                forward_map=forward_map,
                skip_missing_sources=args.skip_missing_sources,
            )
        else:
            result = encode_sql(
                sql_text=sql_text,
                reverse_map=reverse_map,
                ambiguous_map=ambiguous_map,
            )

        if args.in_place:
            args.input.write_text(result, encoding="utf-8")
        elif args.output:
            args.output.write_text(result, encoding="utf-8")
        else:
            sys.stdout.write(result)

        return 0

    except (ConfigError, DecodeError, EncodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e}", file=sys.stderr)
        return 1
    except yaml.YAMLError as e:
        print(f"ERROR: failed to parse YAML: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())