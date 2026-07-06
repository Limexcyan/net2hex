#!/usr/bin/env python3
"""
Reduce a full SUMO edge-H3 mapping to the H3 cells used by visited edges.

This script accepts either a simple visited-edge list or simulation snapshot CSVs.
It is designed to work with departure/snapshot files such as:

    exp_id,time,agent_id,path
    0,6,0,"('315358244', '379897807', '137685155#0')"

Typical workflow:
  1. Run sumo_networks_to_h3_updated.py once to create edge_hex_mapping.csv.
  2. Run this script with either a simple edge list or a simulation path CSV.

Examples:
  # Auto-detect all_departures.csv with a path column
  python visited_edges_to_h3_subset.py \
    --mapping ingolstadt/edge_hex_mapping.csv \
    --visited-edges all_departures.csv \
    --edges-geojson ingolstadt/edges.geojson \
    --out subset_ingolstadt

  # Explicitly parse route/path snapshots
  python visited_edges_to_h3_subset.py \
    --mapping ingolstadt/edge_hex_mapping.csv \
    --visited-edges all_departures.csv \
    --input-format paths \
    --path-column path \
    --time-column time \
    --agent-column agent_id \
    --experiment-column exp_id \
    --edges-geojson ingolstadt/edges.geojson \
    --out subset_ingolstadt

  # Simple one edge per row / edge_id column
  python visited_edges_to_h3_subset.py \
    --mapping out_h3/edge_hex_mapping.csv \
    --visited-edges visited_edges.csv \
    --input-format edge-list \
    --out out_visited

Outputs:
  1. visited_edges_extracted.csv       normalized visited edges with visit counts
  2. visited_edge_hex_mapping.csv      rich mapping rows for only visited edges
  3. visited_edge_hex_dictionary.csv   minimal CSV: hex_id,edge_id
  4. visited_hexes.csv                 one row per used H3 cell
  5. visited_hexes.geojson             polygons for used H3 cells
  6. visited_edges.geojson             filtered edge geometries, if --edges-geojson is supplied
  7. visited_network_hex_map.html      interactive map
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import folium
import pandas as pd


PATH_COLUMN_CANDIDATES = [
    "path",
    "route",
    "routes",
    "edge_path",
    "edge_paths",
    "edges_path",
    "visited_edges",
    "edge_sequence",
    "edge_ids",
]

EDGE_COLUMN_CANDIDATES = ["edge_id", "edge", "edgeid", "id", "visited_edge", "visited_edge_id"]
NETWORK_COLUMN_CANDIDATES = ["network", "network_id", "network_name", "net", "scenario"]
TIME_COLUMN_CANDIDATES = ["time", "t", "step", "simulation_time", "sim_time"]
AGENT_COLUMN_CANDIDATES = ["agent_id", "agent", "vehicle_id", "veh_id", "person_id", "id"]
EXPERIMENT_COLUMN_CANDIDATES = ["exp_id", "experiment_id", "experiment", "run_id", "run", "seed"]


# -----------------------------
# H3 compatibility helpers
# -----------------------------

def _import_h3():
    try:
        import h3  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "The h3 package is required to build visited_hexes.geojson when --hexes-geojson "
            "is not supplied. Install requirements_h3_sumo.txt or pass --hexes-geojson "
            "from the full export."
        ) from exc
    return h3


def h3_cell_to_boundary_latlng(cell: str) -> list[tuple[float, float]]:
    h3 = _import_h3()
    if hasattr(h3, "cell_to_boundary"):
        return list(h3.cell_to_boundary(cell))
    return list(h3.h3_to_geo_boundary(cell, geo_json=False))


def h3_cell_to_latlng(cell: str) -> tuple[float, float]:
    h3 = _import_h3()
    if hasattr(h3, "cell_to_latlng"):
        return h3.cell_to_latlng(cell)
    return h3.h3_to_geo(cell)


def h3_cell_polygon_geojson(cell: str) -> dict[str, Any]:
    boundary = h3_cell_to_boundary_latlng(cell)
    coords = [[lng, lat] for lat, lng in boundary]
    coords.append(coords[0])
    return {"type": "Polygon", "coordinates": [coords]}


# -----------------------------
# General helpers
# -----------------------------

def norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clean_edge_id(value: Any) -> str:
    """Normalize an edge id parsed from CSV/path text."""
    text = norm_text(value)
    if not text:
        return ""
    # Remove common wrapping quotes left by fallback parsing.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def lower_column_map(df: pd.DataFrame) -> dict[str, str]:
    return {str(col).strip().lower(): str(col) for col in df.columns}


def find_existing_column(
    df: pd.DataFrame,
    candidates: Iterable[str],
    explicit: str | None = None,
    required: bool = False,
    role: str = "column",
) -> str | None:
    if explicit:
        if explicit in df.columns:
            return explicit
        # Case-insensitive convenience.
        normalized = lower_column_map(df)
        if explicit.strip().lower() in normalized:
            return normalized[explicit.strip().lower()]
        if required:
            raise ValueError(f"Requested --{role.replace(' ', '-')} '{explicit}' not found. Available columns: {list(df.columns)}")
        return None

    normalized = lower_column_map(df)
    for candidate in candidates:
        if candidate.lower() in normalized:
            return normalized[candidate.lower()]
    if required:
        raise ValueError(f"Could not find a {role}. Tried: {', '.join(candidates)}. Available columns: {list(df.columns)}")
    return None


def read_table_auto(path: Path) -> pd.DataFrame:
    """Read CSV/TSV/semicolon-separated files with pandas delimiter inference."""
    return pd.read_csv(path, sep=None, engine="python", dtype=str).fillna("")


def value_looks_like_path(value: Any) -> bool:
    text = norm_text(value)
    if not text:
        return False
    if (text.startswith("(") and text.endswith(")")) or (text.startswith("[") and text.endswith("]")):
        return True
    # SUMO edge paths often contain several quoted IDs separated by commas.
    if text.count("'") >= 4 and "," in text:
        return True
    if text.count('"') >= 4 and "," in text:
        return True
    # Fallback for space/semicolon/comma separated routes.
    if any(sep in text for sep in [";", ",", " "]) and len(text.split()) > 1:
        return True
    return False


def parse_path_value(value: Any) -> list[str]:
    """
    Parse one path/route cell into an ordered list of edge IDs.

    Handles Python tuples/lists, JSON lists, comma/semicolon/space separated paths,
    and values that already arrived as lists/tuples.
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return [edge for edge in (clean_edge_id(v) for v in value) if edge]

    text = norm_text(value)
    if not text:
        return []

    # Safe parser for strings such as "('edgeA', 'edgeB')" or "['edgeA', 'edgeB']".
    if (text.startswith("(") and text.endswith(")")) or (text.startswith("[") and text.endswith("]")):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple, set)):
                return [edge for edge in (clean_edge_id(v) for v in parsed) if edge]
            if isinstance(parsed, str):
                return [clean_edge_id(parsed)] if clean_edge_id(parsed) else []
        except Exception:
            # Continue to delimiter fallback below.
            pass

    # Quoted tokens fallback. This works for partially malformed tuple-like strings.
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", text)
    if quoted:
        return [edge for edge in (clean_edge_id(v) for v in quoted) if edge]

    # Delimiter fallback.
    if ";" in text:
        parts = text.split(";")
    elif "," in text:
        parts = text.split(",")
    else:
        parts = text.split()

    return [edge for edge in (clean_edge_id(part) for part in parts) if edge]


# -----------------------------
# Visited-edge parsing
# -----------------------------

def read_simple_visited_edges(
    path: Path,
    edge_column: str | None,
    network_column: str | None,
) -> pd.DataFrame:
    """Return columns edge_id, optionally network, and visit_count for simple edge-list files."""
    if path.suffix.lower() in {".txt", ".list"}:
        values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
        values = [value for value in values if value and not value.startswith("#")]
        raw = pd.DataFrame({"edge_id": values})
    else:
        raw = read_table_auto(path)
        if raw.empty:
            return pd.DataFrame(columns=["edge_id", "visit_count"])

        edge_col = find_existing_column(raw, EDGE_COLUMN_CANDIDATES, edge_column, required=False, role="edge column")
        if edge_col is None:
            edge_col = str(raw.columns[0])

        data: dict[str, Any] = {"edge_id": raw[edge_col].map(clean_edge_id)}

        network_col = find_existing_column(raw, NETWORK_COLUMN_CANDIDATES, network_column, required=False, role="network column")
        if network_col is not None:
            data["network"] = raw[network_col].map(clean_edge_id)
        raw = pd.DataFrame(data)

    raw = raw[raw["edge_id"].astype(str).str.len() > 0].copy()

    # Support one-column files with values like network::edge_id.
    has_network_col = "network" in raw.columns and raw["network"].astype(str).str.len().any()
    contains_networked_ids = raw["edge_id"].astype(str).str.contains("::", regex=False).any()
    if not has_network_col and contains_networked_ids:
        split = raw["edge_id"].astype(str).str.split("::", n=1, expand=True)
        raw["network"] = split[0].str.strip()
        raw["edge_id"] = split[1].str.strip()

    group_cols = ["network", "edge_id"] if "network" in raw.columns else ["edge_id"]
    visited = raw.groupby(group_cols, dropna=False).size().reset_index(name="visit_count")
    return visited.sort_values(group_cols).reset_index(drop=True)


def read_path_snapshot_edges(
    path: Path,
    path_column: str | None,
    edge_column: str | None,
    network_column: str | None,
    time_column: str | None,
    agent_column: str | None,
    experiment_column: str | None,
) -> pd.DataFrame:
    """
    Read simulation snapshots/departures where each row stores a route/path.

    Returns one row per unique visited edge with visit_count and optional first/last
    time, agent_count, and experiment_count.
    """
    raw = read_table_auto(path)
    if raw.empty:
        return pd.DataFrame(columns=["edge_id", "visit_count"])

    path_col = find_existing_column(raw, PATH_COLUMN_CANDIDATES, path_column, required=False, role="path column")
    if path_col is None:
        # Allow --edge-column as a path column if the user explicitly supplied it.
        path_col = find_existing_column(raw, [], edge_column, required=False, role="edge column")
    if path_col is None:
        raise ValueError(
            "Could not auto-detect a path/route column. Use --path-column path, or use "
            "--input-format edge-list for simple edge lists. Available columns: " + str(list(raw.columns))
        )

    network_col = find_existing_column(raw, NETWORK_COLUMN_CANDIDATES, network_column, required=False, role="network column")
    time_col = find_existing_column(raw, TIME_COLUMN_CANDIDATES, time_column, required=False, role="time column")
    agent_col = find_existing_column(raw, AGENT_COLUMN_CANDIDATES, agent_column, required=False, role="agent column")
    experiment_col = find_existing_column(raw, EXPERIMENT_COLUMN_CANDIDATES, experiment_column, required=False, role="experiment column")

    counts: Counter[tuple[str, str | None]] = Counter()
    first_time: dict[tuple[str, str | None], str] = {}
    last_time: dict[tuple[str, str | None], str] = {}
    agents: defaultdict[tuple[str, str | None], set[str]] = defaultdict(set)
    experiments: defaultdict[tuple[str, str | None], set[str]] = defaultdict(set)

    total_path_rows = 0
    total_edge_appearances = 0

    for _, row in raw.iterrows():
        edges = parse_path_value(row[path_col])
        if not edges:
            continue

        total_path_rows += 1
        network = clean_edge_id(row[network_col]) if network_col else None
        time_value = clean_edge_id(row[time_col]) if time_col else ""
        agent_value = clean_edge_id(row[agent_col]) if agent_col else ""
        experiment_value = clean_edge_id(row[experiment_col]) if experiment_col else ""

        for edge_id in edges:
            key = (edge_id, network)
            counts[key] += 1
            total_edge_appearances += 1
            if time_value:
                first_time.setdefault(key, time_value)
                last_time[key] = time_value
            if agent_value:
                agents[key].add(agent_value)
            if experiment_value:
                experiments[key].add(experiment_value)

    rows: list[dict[str, Any]] = []
    for (edge_id, network), count in counts.items():
        record: dict[str, Any] = {"edge_id": edge_id, "visit_count": int(count)}
        if network is not None:
            record["network"] = network
        if first_time:
            record["first_time"] = first_time.get((edge_id, network), "")
            record["last_time"] = last_time.get((edge_id, network), "")
        if agents:
            record["agent_count"] = len(agents.get((edge_id, network), set()))
        if experiments:
            record["experiment_count"] = len(experiments.get((edge_id, network), set()))
        rows.append(record)

    visited = pd.DataFrame(rows)
    if visited.empty:
        return pd.DataFrame(columns=["edge_id", "visit_count"])

    sort_cols = ["network", "edge_id"] if "network" in visited.columns else ["edge_id"]
    visited = visited.sort_values(sort_cols).reset_index(drop=True)

    print(
        f"Parsed path snapshots from column '{path_col}': "
        f"{total_path_rows} rows with paths, {total_edge_appearances} edge appearances, "
        f"{len(visited)} unique visited edges."
    )
    return visited


def read_visited_edges(
    path: Path,
    input_format: str,
    edge_column: str | None,
    network_column: str | None,
    path_column: str | None,
    time_column: str | None,
    agent_column: str | None,
    experiment_column: str | None,
) -> pd.DataFrame:
    """Dispatch reader for simple edge lists or path snapshot CSVs."""
    if input_format == "edge-list":
        return read_simple_visited_edges(path, edge_column, network_column)

    if input_format == "paths":
        return read_path_snapshot_edges(
            path=path,
            path_column=path_column,
            edge_column=edge_column,
            network_column=network_column,
            time_column=time_column,
            agent_column=agent_column,
            experiment_column=experiment_column,
        )

    # auto
    if path.suffix.lower() in {".txt", ".list"}:
        return read_simple_visited_edges(path, edge_column, network_column)

    raw_peek = read_table_auto(path)
    if raw_peek.empty:
        return pd.DataFrame(columns=["edge_id", "visit_count"])

    path_col = find_existing_column(raw_peek, PATH_COLUMN_CANDIDATES, path_column, required=False, role="path column")
    if path_col is not None:
        non_empty = raw_peek[path_col][raw_peek[path_col].astype(str).str.len() > 0]
        if not non_empty.empty and value_looks_like_path(non_empty.iloc[0]):
            return read_path_snapshot_edges(
                path=path,
                path_column=path_col,
                edge_column=edge_column,
                network_column=network_column,
                time_column=time_column,
                agent_column=agent_column,
                experiment_column=experiment_column,
            )

    # If there is no explicit edge column but there is a path-like first non-empty value
    # in any candidate-looking column, prefer path mode.
    edge_col = find_existing_column(raw_peek, EDGE_COLUMN_CANDIDATES, edge_column, required=False, role="edge column")
    if edge_col is None:
        for col in raw_peek.columns:
            non_empty = raw_peek[col][raw_peek[col].astype(str).str.len() > 0]
            if not non_empty.empty and value_looks_like_path(non_empty.iloc[0]):
                return read_path_snapshot_edges(
                    path=path,
                    path_column=str(col),
                    edge_column=edge_column,
                    network_column=network_column,
                    time_column=time_column,
                    agent_column=agent_column,
                    experiment_column=experiment_column,
                )

    return read_simple_visited_edges(path, edge_column, network_column)


# -----------------------------
# Filtering and outputs
# -----------------------------

def find_mapping_column(df: pd.DataFrame, candidates: list[str], role: str) -> str:
    col = find_existing_column(df, candidates, required=True, role=role)
    assert col is not None
    return col


def debug_examples(mapping: pd.DataFrame, visited: pd.DataFrame, edge_col: str, network_col: str | None, n: int = 10) -> str:
    mapping_edges = set(mapping[edge_col].astype(str).str.strip())
    visited_edges = set(visited["edge_id"].astype(str).str.strip())
    overlap = mapping_edges & visited_edges
    msg = []
    msg.append(f"Mapping unique edge IDs: {len(mapping_edges)}")
    msg.append(f"Visited unique edge IDs: {len(visited_edges)}")
    msg.append(f"Raw edge-ID overlap: {len(overlap)}")
    msg.append(f"Mapping edge examples: {sorted(list(mapping_edges))[:n]}")
    msg.append(f"Visited edge examples: {sorted(list(visited_edges))[:n]}")
    msg.append(f"Visited-not-in-mapping examples: {sorted(list(visited_edges - mapping_edges))[:n]}")
    if network_col and "network" in visited.columns:
        mapping_pairs = set(zip(mapping[network_col].astype(str).str.strip(), mapping[edge_col].astype(str).str.strip()))
        visited_pairs = set(zip(visited["network"].astype(str).str.strip(), visited["edge_id"].astype(str).str.strip()))
        msg.append(f"Network+edge overlap: {len(mapping_pairs & visited_pairs)}")
        msg.append(f"Mapping networks: {sorted(mapping[network_col].astype(str).str.strip().unique().tolist())[:n]}")
        msg.append(f"Visited networks: {sorted(visited['network'].astype(str).str.strip().unique().tolist())[:n]}")
    return "\n".join(msg)


def filter_mapping_to_visited(mapping: pd.DataFrame, visited: pd.DataFrame, debug: bool = False) -> tuple[pd.DataFrame, str, str, str | None]:
    hex_col = find_mapping_column(mapping, ["h3_cell", "hex_id", "hex", "h3", "cell"], "hex/H3 column")
    edge_col = find_mapping_column(mapping, ["edge_id", "edge", "edgeid", "id"], "edge id column")
    network_col = find_existing_column(mapping, NETWORK_COLUMN_CANDIDATES, required=False, role="network column")

    mapping = mapping.copy()
    mapping[edge_col] = mapping[edge_col].map(clean_edge_id)
    mapping[hex_col] = mapping[hex_col].map(clean_edge_id)
    if network_col:
        mapping[network_col] = mapping[network_col].map(clean_edge_id)

    visited = visited.copy()
    visited["edge_id"] = visited["edge_id"].map(clean_edge_id)
    visited = visited[visited["edge_id"].str.len() > 0].copy()
    if "network" in visited.columns:
        visited["network"] = visited["network"].map(clean_edge_id)

    if debug:
        print("\n--- Matching diagnostics before filtering ---")
        print(debug_examples(mapping, visited, edge_col, network_col))
        print("--- End diagnostics ---\n")

    if "network" in visited.columns and network_col and visited["network"].astype(str).str.len().any():
        filtered = mapping.merge(
            visited.rename(columns={"edge_id": edge_col, "network": network_col}),
            on=[network_col, edge_col],
            how="inner",
        )
    else:
        filtered = mapping[mapping[edge_col].isin(set(visited["edge_id"]))].copy()

        if network_col:
            possible_ambiguous = (
                mapping[[network_col, edge_col]]
                .drop_duplicates()
                .groupby(edge_col, dropna=False)[network_col]
                .nunique()
            )
            duplicate_count = int((possible_ambiguous > 1).sum())
            if duplicate_count:
                print(
                    "WARNING: The full mapping contains raw edge_id values appearing in multiple networks. "
                    "Provide network,edge_id in the visited file or use network::edge_id to avoid ambiguity."
                )

    # Add visit statistics to the filtered mapping so downstream outputs can use them.
    stats_cols = [c for c in ["network", "edge_id", "visit_count", "first_time", "last_time", "agent_count", "experiment_count"] if c in visited.columns]
    if not filtered.empty and stats_cols:
        if "network" in visited.columns and network_col and visited["network"].astype(str).str.len().any():
            stats = visited[stats_cols].rename(columns={"edge_id": edge_col, "network": network_col})
            filtered = filtered.merge(stats, on=[network_col, edge_col], how="left")
        else:
            stats = visited[[c for c in stats_cols if c != "network"]].rename(columns={"edge_id": edge_col})
            # If a simple edge list includes duplicate rows, stats has unique edge ids by construction.
            filtered = filtered.merge(stats, on=edge_col, how="left")

    if filtered.empty:
        raise ValueError(
            "No visited edges matched the edge-H3 mapping.\n" +
            debug_examples(mapping, visited, edge_col, network_col) +
            "\nMost common cause: the visited file was parsed as the wrong format. "
            "For snapshot files with a path column, use --input-format paths --path-column path."
        )

    return filtered, hex_col, edge_col, network_col


def build_simple_dictionary(filtered: pd.DataFrame, hex_col: str, edge_col: str, network_col: str | None, edge_id_format: str) -> pd.DataFrame:
    if filtered.empty:
        return pd.DataFrame(columns=["hex_id", "edge_id"])

    simple = filtered[[hex_col, edge_col]].copy().rename(columns={hex_col: "hex_id", edge_col: "edge_id"})
    if edge_id_format == "networked":
        if not network_col:
            raise ValueError("--simple-edge-id-format networked requires a network column in the full mapping.")
        simple["edge_id"] = filtered[network_col].astype(str) + "::" + filtered[edge_col].astype(str)
    elif edge_id_format != "raw":
        raise ValueError("edge_id_format must be 'raw' or 'networked'.")

    return simple[["hex_id", "edge_id"]].drop_duplicates().sort_values(["hex_id", "edge_id"]).reset_index(drop=True)


def build_visited_hex_summary(filtered: pd.DataFrame, hex_col: str, edge_col: str, network_col: str | None) -> pd.DataFrame:
    if filtered.empty:
        return pd.DataFrame(columns=["hex_id", "visited_edge_count", "visited_edge_ids"])

    rows = []
    for hex_id, group in filtered.groupby(hex_col, sort=True):
        if network_col:
            edge_ids = sorted({f"{row[network_col]}::{row[edge_col]}" for _, row in group.iterrows()})
        else:
            edge_ids = sorted(group[edge_col].astype(str).unique())

        row: dict[str, Any] = {
            "hex_id": hex_id,
            "visited_edge_count": len(edge_ids),
            "visited_edge_ids": ";".join(edge_ids),
        }
        if "visit_count" in group.columns:
            row["total_edge_visits"] = int(pd.to_numeric(group["visit_count"], errors="coerce").fillna(0).sum())
        rows.append(row)

    return pd.DataFrame(rows)


def load_geojson(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def build_visited_hexes_geojson_from_full(
    full_hexes_geojson: dict[str, Any],
    visited_hexes: pd.DataFrame,
) -> dict[str, Any]:
    wanted = set(visited_hexes["hex_id"].astype(str))
    summary = visited_hexes.set_index("hex_id").to_dict(orient="index")
    features = []

    for feature in full_hexes_geojson.get("features", []):
        props = feature.get("properties", {})
        cell = str(props.get("hex_id") or props.get("h3_cell") or "").strip()
        if cell not in wanted:
            continue
        new_feature = json.loads(json.dumps(feature))  # deep copy JSON-compatible object
        new_props = dict(new_feature.get("properties", {}))
        new_props.update(summary.get(cell, {}))
        new_props["hex_id"] = cell
        new_props["h3_cell"] = cell
        new_feature["properties"] = new_props
        features.append(new_feature)

    missing = wanted - {
        str((feature.get("properties", {}).get("hex_id") or feature.get("properties", {}).get("h3_cell") or "")).strip()
        for feature in features
    }
    if missing:
        print(
            f"WARNING: {len(missing)} visited hexes were not found in --hexes-geojson. "
            "They will be generated with h3 if possible."
        )
        generated = build_visited_hexes_geojson(visited_hexes[visited_hexes["hex_id"].isin(missing)])
        features.extend(generated.get("features", []))

    return {"type": "FeatureCollection", "features": features}


def build_visited_hexes_geojson(visited_hexes: pd.DataFrame, resolution: int | None = None) -> dict[str, Any]:
    features = []
    for _, row in visited_hexes.iterrows():
        cell = str(row["hex_id"])
        lat, lon = h3_cell_to_latlng(cell)
        props = {
            "hex_id": cell,
            "h3_cell": cell,
            "visited_edge_count": int(row["visited_edge_count"]),
            "visited_edge_ids": row.get("visited_edge_ids", ""),
            "center_lat": lat,
            "center_lon": lon,
        }
        if "total_edge_visits" in row:
            props["total_edge_visits"] = int(row["total_edge_visits"])
        if resolution is not None:
            props["h3_resolution"] = int(resolution)
        features.append({"type": "Feature", "geometry": h3_cell_polygon_geojson(cell), "properties": props})

    return {"type": "FeatureCollection", "features": features}


def filter_edges_geojson(edges_geojson: dict[str, Any], filtered: pd.DataFrame, edge_col: str, network_col: str | None) -> dict[str, Any]:
    if filtered.empty:
        return {"type": "FeatureCollection", "features": []}

    if network_col:
        wanted = set(zip(filtered[network_col].astype(str), filtered[edge_col].astype(str)))
    else:
        wanted_edges = set(filtered[edge_col].astype(str))
        wanted = None

    features = []
    for feature in edges_geojson.get("features", []):
        props = feature.get("properties", {})
        edge_id = clean_edge_id(props.get("edge_id", ""))
        network = clean_edge_id(props.get("network", ""))
        keep = (network, edge_id) in wanted if wanted is not None else edge_id in wanted_edges
        if keep:
            features.append(feature)

    return {"type": "FeatureCollection", "features": features}


def get_map_center(hexes_geojson: dict[str, Any], edges_geojson: dict[str, Any] | None = None) -> tuple[float, float]:
    coords = []
    if edges_geojson:
        for feature in edges_geojson.get("features", []):
            geom = feature.get("geometry", {})
            if geom.get("type") == "LineString":
                coords.extend(geom.get("coordinates", []))

    if coords:
        avg_lon = sum(c[0] for c in coords) / len(coords)
        avg_lat = sum(c[1] for c in coords) / len(coords)
        return avg_lat, avg_lon

    centers = []
    for feature in hexes_geojson.get("features", []):
        props = feature.get("properties", {})
        if "center_lat" in props and "center_lon" in props:
            centers.append((float(props["center_lat"]), float(props["center_lon"])))

    if not centers:
        return 0.0, 0.0

    avg_lat = sum(lat for lat, _ in centers) / len(centers)
    avg_lon = sum(lon for _, lon in centers) / len(centers)
    return avg_lat, avg_lon


def create_visited_map(
    hexes_geojson: dict[str, Any],
    out_html: Path,
    visited_edges_geojson: dict[str, Any] | None = None,
    map_max_edges: int = 20000,
    map_max_hexes: int = 20000,
) -> None:
    center_lat, center_lon = get_map_center(hexes_geojson, visited_edges_geojson)
    fmap = folium.Map(location=[center_lat, center_lon], zoom_start=12, tiles="CartoDB positron")

    rendered_hexes = {
        "type": "FeatureCollection",
        "features": hexes_geojson.get("features", [])[:map_max_hexes],
    }

    # Include only tooltip fields that are present in the first rendered feature.
    default_hex_fields = ["hex_id", "visited_edge_count", "total_edge_visits", "visited_edge_ids"]
    if rendered_hexes["features"]:
        props = rendered_hexes["features"][0].get("properties", {})
        hex_fields = [field for field in default_hex_fields if field in props]
    else:
        hex_fields = ["hex_id", "visited_edge_count"]
    aliases = {
        "hex_id": "H3 hex",
        "visited_edge_count": "Visited edge count",
        "total_edge_visits": "Total edge visits",
        "visited_edge_ids": "Visited edge IDs",
    }

    folium.GeoJson(
        rendered_hexes,
        name=f"Visited H3 hexes, rendered {len(rendered_hexes['features'])}/{len(hexes_geojson.get('features', []))}",
        style_function=lambda feature: {
            "fillColor": "#ff7800",
            "color": "#222222",
            "weight": 0.7,
            "fillOpacity": min(0.8, 0.2 + 0.05 * feature["properties"].get("visited_edge_count", 1)),
        },
        tooltip=folium.GeoJsonTooltip(
            fields=hex_fields,
            aliases=[aliases[field] for field in hex_fields],
            sticky=True,
        ),
    ).add_to(fmap)

    if visited_edges_geojson:
        rendered_edges = {
            "type": "FeatureCollection",
            "features": visited_edges_geojson.get("features", [])[:map_max_edges],
        }
        if rendered_edges["features"]:
            folium.GeoJson(
                rendered_edges,
                name=f"Visited SUMO edges, rendered {len(rendered_edges['features'])}/{len(visited_edges_geojson.get('features', []))}",
                style_function=lambda feature: {
                    "color": "#0055ff",
                    "weight": 2,
                    "opacity": 0.8,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["network", "edge_id"],
                    aliases=["Network", "Edge ID"],
                    sticky=True,
                ),
            ).add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.save(str(out_html))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Induce used H3 cells from visited SUMO edges or simulation path snapshots.")
    parser.add_argument("--mapping", type=Path, required=True, help="Full edge_hex_mapping.csv from the first script.")
    parser.add_argument("--visited-edges", type=Path, required=True, help="CSV/TXT file containing visited edges or path snapshots.")
    parser.add_argument("--edges-geojson", type=Path, default=None, help="Optional edges.geojson from the first script for map/network overlay.")
    parser.add_argument("--hexes-geojson", type=Path, default=None, help="Optional hexes.geojson from the first script. Avoids needing h3 to rebuild polygons.")
    parser.add_argument("--out", type=Path, default=Path("out_visited_h3"), help="Output directory.")

    parser.add_argument(
        "--input-format",
        choices=["auto", "edge-list", "paths"],
        default="auto",
        help="auto detects path snapshots with a path/route column; edge-list reads edge IDs directly; paths parses each row as a route/path.",
    )
    parser.add_argument("--edge-column", default=None, help="Column name containing visited edge ids for edge-list input.")
    parser.add_argument("--path-column", default=None, help="Column name containing path/route tuples/lists for paths input, e.g. path.")
    parser.add_argument("--network-column", default=None, help="Optional column name containing network ids/names in visited file.")
    parser.add_argument("--time-column", default=None, help="Optional time column for path snapshots, e.g. time.")
    parser.add_argument("--agent-column", default=None, help="Optional agent/vehicle column for path snapshots, e.g. agent_id.")
    parser.add_argument("--experiment-column", default=None, help="Optional experiment/run column for path snapshots, e.g. exp_id.")

    parser.add_argument(
        "--simple-edge-id-format",
        choices=["raw", "networked"],
        default="raw",
        help="Use networked to write edge_id as network::edge_id in the minimal two-column output.",
    )
    parser.add_argument("--map-max-edges", type=int, default=20000, help="Maximum edge features rendered in HTML map.")
    parser.add_argument("--map-max-hexes", type=int, default=20000, help="Maximum H3 polygon features rendered in HTML map.")
    parser.add_argument("--debug", action="store_true", help="Print matching diagnostics and example edge IDs.")
    parser.add_argument(
        "--no-map",
        action="store_true",
        help="Write CSV/GeoJSON outputs but skip the HTML map. Useful for very large subsets.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    mapping = pd.read_csv(args.mapping, dtype=str).fillna("")
    visited = read_visited_edges(
        path=args.visited_edges,
        input_format=args.input_format,
        edge_column=args.edge_column,
        network_column=args.network_column,
        path_column=args.path_column,
        time_column=args.time_column,
        agent_column=args.agent_column,
        experiment_column=args.experiment_column,
    )

    if visited.empty:
        raise ValueError(f"No visited edges found in {args.visited_edges}")

    extracted_csv = args.out / "visited_edges_extracted.csv"
    visited.to_csv(extracted_csv, index=False)

    filtered, hex_col, edge_col, network_col = filter_mapping_to_visited(mapping, visited, debug=args.debug)

    resolution = None
    if "h3_resolution" in filtered.columns and filtered["h3_resolution"].astype(str).str.len().any():
        resolution = int(filtered["h3_resolution"].astype(str).iloc[0])

    visited_mapping_csv = args.out / "visited_edge_hex_mapping.csv"
    visited_dict_csv = args.out / "visited_edge_hex_dictionary.csv"
    visited_hexes_csv = args.out / "visited_hexes.csv"
    visited_hexes_geojson_path = args.out / "visited_hexes.geojson"
    visited_edges_geojson_path = args.out / "visited_edges.geojson"
    visited_map_html = args.out / "visited_network_hex_map.html"

    filtered.to_csv(visited_mapping_csv, index=False)

    simple = build_simple_dictionary(filtered, hex_col, edge_col, network_col, args.simple_edge_id_format)
    simple.to_csv(visited_dict_csv, index=False)

    visited_hexes = build_visited_hex_summary(filtered, hex_col, edge_col, network_col)
    visited_hexes.to_csv(visited_hexes_csv, index=False)

    if args.hexes_geojson is not None:
        full_hexes_geojson = load_geojson(args.hexes_geojson)
        hexes_geojson = build_visited_hexes_geojson_from_full(full_hexes_geojson, visited_hexes)
    else:
        hexes_geojson = build_visited_hexes_geojson(visited_hexes, resolution=resolution)
    write_json(visited_hexes_geojson_path, hexes_geojson)

    visited_edges_geojson = None
    if args.edges_geojson is not None:
        edges_geojson = load_geojson(args.edges_geojson)
        visited_edges_geojson = filter_edges_geojson(edges_geojson, filtered, edge_col, network_col)
        write_json(visited_edges_geojson_path, visited_edges_geojson)

    if not args.no_map:
        create_visited_map(
            hexes_geojson=hexes_geojson,
            visited_edges_geojson=visited_edges_geojson,
            out_html=visited_map_html,
            map_max_edges=args.map_max_edges,
            map_max_hexes=args.map_max_hexes,
        )

    print("\nDone.")
    print(f"Visited unique edges extracted:   {len(visited)}")
    print(f"Visited edge-H3 relations:        {len(filtered)}")
    print(f"Used H3 hexes:                    {len(visited_hexes)}")
    print(f"Extracted visited edges CSV:      {extracted_csv}")
    print(f"Rich visited mapping CSV:         {visited_mapping_csv}")
    print(f"Minimal hex_id,edge_id CSV:       {visited_dict_csv}")
    print(f"Visited hex summary CSV:          {visited_hexes_csv}")
    print(f"Visited hexes GeoJSON:            {visited_hexes_geojson_path}")
    if visited_edges_geojson is not None:
        print(f"Visited edges GeoJSON:            {visited_edges_geojson_path}")
    if not args.no_map:
        print(f"Interactive visited map:          {visited_map_html}")


if __name__ == "__main__":
    main()
