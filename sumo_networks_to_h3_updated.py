#!/usr/bin/env python3
"""
Export SUMO road-network edges to H3 hex cells and create an interactive map.

Outputs:
  1. edge_hex_mapping.csv          rich one-row-per edge-H3-cell relation
  2. hex_edge_dictionary.csv       minimal CSV: hex_id,edge_id
  3. edges.geojson                 network edge geometries in lon/lat
  4. hexes.geojson                 H3 hex polygons with edge counts
  5. network_hex_map.html          interactive Folium map

Examples:
  python sumo_networks_to_h3_updated.py \
    --clone-repo \
    --resolution 10 \
    --buffer-m 20 \
    --out out_h3

  python sumo_networks_to_h3_updated.py \
    --input "net2hex/networks/COeXISTENCE-PROJECT URB main networks" \
    --hex-radius-m 65 \
    --buffer-m 20 \
    --out out_h3

Notes:
  - H3 resolutions are integer levels, not exact meter radii.  --hex-radius-m
    chooses the H3 resolution whose average hexagon edge length is closest to
    the requested value.
  - The minimal dictionary intentionally has only two columns, hex_id and edge_id.
    If the same SUMO edge_id can appear in multiple networks, use
    --simple-edge-id-format networked so edge_id becomes network::edge_id while
    keeping the two-column format.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Sequence

import folium
import h3
import pandas as pd
from pyproj import CRS, Transformer
from shapely.geometry import LineString, mapping
from shapely.ops import transform
from tqdm import tqdm

from sumolib.net import readNet


DEFAULT_REPO_URL = "https://github.com/Limexcyan/net2hex.git"
DEFAULT_REPO_SUBPATH = "networks/COeXISTENCE-PROJECT URB main networks"


# -----------------------------
# H3 compatibility helpers
# -----------------------------

def h3_latlng_to_cell(lat: float, lng: float, res: int) -> str:
    if hasattr(h3, "latlng_to_cell"):
        return h3.latlng_to_cell(lat, lng, res)
    return h3.geo_to_h3(lat, lng, res)


def h3_cell_to_boundary_latlng(cell: str) -> list[tuple[float, float]]:
    if hasattr(h3, "cell_to_boundary"):
        return list(h3.cell_to_boundary(cell))
    return list(h3.h3_to_geo_boundary(cell, geo_json=False))


def h3_cell_to_latlng(cell: str) -> tuple[float, float]:
    if hasattr(h3, "cell_to_latlng"):
        return h3.cell_to_latlng(cell)
    return h3.h3_to_geo(cell)


def h3_average_edge_length_m(res: int) -> float:
    if hasattr(h3, "average_hexagon_edge_length"):
        return float(h3.average_hexagon_edge_length(res, unit="m"))
    # h3-py v3 fallback
    return float(h3.edge_length(res, unit="m"))


def h3_geo_to_cells(geo, res: int) -> set[str]:
    """
    Convert a WGS84 Shapely geometry to H3 cells.

    h3-py v4 can consume objects with __geo_interface__ in many cases.  The
    fallback to mapping(geo) keeps this robust across h3-py installations.
    """
    if hasattr(h3, "geo_to_cells"):
        try:
            return set(h3.geo_to_cells(geo, res))
        except TypeError:
            return set(h3.geo_to_cells(mapping(geo), res))

    # h3-py v3 fallback
    return set(h3.polyfill(mapping(geo), res, geo_json_conformant=True))


def h3_grid_disk(cell: str, k: int) -> set[str]:
    if k <= 0:
        return {cell}
    if hasattr(h3, "grid_disk"):
        return set(h3.grid_disk(cell, k))
    return set(h3.k_ring(cell, k))


# -----------------------------
# CRS / geometry helpers
# -----------------------------

def utm_crs_from_lonlat(lon: float, lat: float) -> CRS:
    """
    Pick a local UTM CRS from a lon/lat point.
    This is suitable for city/regional SUMO networks.
    """
    zone = int((lon + 180.0) // 6.0) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def make_transformers_for_geometry(line_wgs84: LineString):
    lon, lat = line_wgs84.centroid.x, line_wgs84.centroid.y
    metric_crs = utm_crs_from_lonlat(lon, lat)

    to_metric = Transformer.from_crs("EPSG:4326", metric_crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True)

    return (
        lambda geom: transform(to_metric.transform, geom),
        lambda geom: transform(to_wgs84.transform, geom),
        metric_crs,
    )


def sample_line_cells(
    line_metric: LineString,
    to_wgs84_func,
    resolution: int,
    sample_step_m: float,
    ring_k: int,
) -> set[str]:
    """
    Sample points along an edge centerline and assign them to H3 cells.
    ring_k optionally expands the sampled cells to neighboring cells.
    """
    cells: set[str] = set()

    length = float(line_metric.length)
    if length <= 0:
        return cells

    n_steps = max(1, int(math.ceil(length / sample_step_m)))

    for i in range(n_steps + 1):
        d = min(length, i * sample_step_m)
        pt_metric = line_metric.interpolate(d)
        pt_wgs84 = to_wgs84_func(pt_metric)
        lon, lat = pt_wgs84.x, pt_wgs84.y

        cell = h3_latlng_to_cell(lat, lon, resolution)
        cells.update(h3_grid_disk(cell, ring_k))

    return cells


def buffered_line_cells(
    line_metric: LineString,
    to_wgs84_func,
    resolution: int,
    buffer_m: float,
) -> set[str]:
    """
    Buffer the edge centerline in meters and use H3 polygon filling.
    H3 polygon filling is centroid-based, so this is combined with line sampling.
    """
    if buffer_m <= 0:
        return set()

    poly_metric = line_metric.buffer(buffer_m, cap_style=2, join_style=2)
    if poly_metric.is_empty:
        return set()

    poly_wgs84 = to_wgs84_func(poly_metric)
    return h3_geo_to_cells(poly_wgs84, resolution)


def h3_cell_polygon_geojson(cell: str) -> dict:
    """
    Return GeoJSON polygon geometry for an H3 cell.
    H3 returns boundary as lat/lng; GeoJSON needs lon/lat.
    """
    boundary = h3_cell_to_boundary_latlng(cell)
    coords = [[lng, lat] for lat, lng in boundary]
    coords.append(coords[0])
    return {
        "type": "Polygon",
        "coordinates": [coords],
    }


# -----------------------------
# Repo / network handling
# -----------------------------

def clone_repo_subfolder(repo_url: str, subpath: str, workdir: Path) -> Path:
    """
    Sparse-clone only the required networks subfolder.
    Requires git installed.
    """
    repo_dir = workdir / "net2hex"

    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo_url, str(repo_dir)],
        check=True,
    )

    subprocess.run(
        ["git", "-C", str(repo_dir), "sparse-checkout", "set", subpath],
        check=True,
    )

    root = repo_dir / subpath
    if not root.exists():
        raise FileNotFoundError(f"Sparse checkout completed, but subpath not found: {root}")

    return root


def find_net_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.net.xml") if p.is_file())


def choose_resolution_from_radius_m(target_radius_m: float) -> int:
    """
    H3 does not define cells by an exact meter radius; it uses integer resolutions.
    For a regular hexagon, side length equals circumradius, so this chooses the
    closest H3 average hexagon edge length.
    """
    candidates = {res: h3_average_edge_length_m(res) for res in range(16)}
    return min(candidates, key=lambda res: abs(candidates[res] - target_radius_m))


# -----------------------------
# Main extraction
# -----------------------------

def extract_edges_from_sumo_net(
    net_file: Path,
    include_internal: bool,
    include_junctions: bool,
) -> list[dict]:
    """
    Read a SUMO .net.xml and return edge records with WGS84 LineString geometry.
    """
    net = readNet(str(net_file), withInternal=include_internal)
    network_name = net_file.parent.name

    records: list[dict] = []

    for edge in net.getEdges():
        edge_function = edge.getFunction() or ""

        if edge_function and not include_internal:
            continue

        shape_xy = edge.getShape(includeJunctions=include_junctions)
        if not shape_xy or len(shape_xy) < 2:
            continue

        lonlat = []
        for x, y in shape_xy:
            lon, lat = net.convertXY2LonLat(x, y)
            lonlat.append((float(lon), float(lat)))

        # Remove duplicated consecutive points.
        cleaned = [lonlat[0]]
        for pt in lonlat[1:]:
            if pt != cleaned[-1]:
                cleaned.append(pt)

        if len(cleaned) < 2:
            continue

        line = LineString(cleaned)
        if line.is_empty or line.length == 0:
            continue

        records.append(
            {
                "network": network_name,
                "net_file": str(net_file),
                "edge_id": edge.getID(),
                "edge_name": edge.getName() or "",
                "edge_type": edge.getType() or "",
                "edge_function": edge_function,
                "speed_mps": float(edge.getSpeed()) if edge.getSpeed() is not None else None,
                "length_m_sumo": float(edge.getLength()) if edge.getLength() is not None else None,
                "lane_count": int(edge.getLaneNumber()),
                "geometry": line,
            }
        )

    return records


def build_mapping(
    edge_records: Sequence[dict],
    resolution: int,
    buffer_m: float,
    sample_step_m: float,
    ring_k: int,
) -> tuple[pd.DataFrame, dict, dict]:
    """
    Build edge-H3 mapping and GeoJSON feature collections.
    """
    mapping_rows: list[dict] = []
    edge_features: list[dict] = []
    hex_to_edges: dict[str, set[str]] = defaultdict(set)
    hex_to_networks: dict[str, set[str]] = defaultdict(set)

    for rec in tqdm(edge_records, desc="Mapping edges to H3"):
        line_wgs84: LineString = rec["geometry"]

        to_metric, to_wgs84, metric_crs = make_transformers_for_geometry(line_wgs84)
        line_metric = to_metric(line_wgs84)

        cells = set()

        # 1. Sample along edge centerline.
        cells.update(
            sample_line_cells(
                line_metric=line_metric,
                to_wgs84_func=to_wgs84,
                resolution=resolution,
                sample_step_m=sample_step_m,
                ring_k=ring_k,
            )
        )

        # 2. Add cells whose centroids fall inside an edge buffer.
        cells.update(
            buffered_line_cells(
                line_metric=line_metric,
                to_wgs84_func=to_wgs84,
                resolution=resolution,
                buffer_m=buffer_m,
            )
        )

        sorted_cells = sorted(cells)
        edge_key = f"{rec['network']}::{rec['edge_id']}"

        for cell in sorted_cells:
            mapping_rows.append(
                {
                    "network": rec["network"],
                    "edge_id": rec["edge_id"],
                    "h3_cell": cell,
                    "h3_resolution": resolution,
                    "edge_length_m_projected": round(float(line_metric.length), 3),
                    "edge_length_m_sumo": rec["length_m_sumo"],
                    "buffer_m": buffer_m,
                    "sample_step_m": sample_step_m,
                    "ring_k": ring_k,
                    "lane_count": rec["lane_count"],
                    "speed_mps": rec["speed_mps"],
                    "edge_type": rec["edge_type"],
                    "edge_function": rec["edge_function"],
                    "net_file": rec["net_file"],
                }
            )
            hex_to_edges[cell].add(edge_key)
            hex_to_networks[cell].add(rec["network"])

        props = {k: v for k, v in rec.items() if k != "geometry"}
        props["h3_cell_count"] = len(sorted_cells)
        props["h3_cells_preview"] = ";".join(sorted_cells[:20])
        props["metric_crs"] = metric_crs.to_string()

        edge_features.append(
            {
                "type": "Feature",
                "geometry": mapping(line_wgs84),
                "properties": props,
            }
        )

    mapping_df = pd.DataFrame(mapping_rows)

    edges_geojson = {
        "type": "FeatureCollection",
        "features": edge_features,
    }

    hex_features = []
    for cell, edges in tqdm(hex_to_edges.items(), desc="Building H3 polygons"):
        lat, lon = h3_cell_to_latlng(cell)
        edge_list = sorted(edges)
        networks = sorted(hex_to_networks[cell])

        hex_features.append(
            {
                "type": "Feature",
                "geometry": h3_cell_polygon_geojson(cell),
                "properties": {
                    "h3_cell": cell,
                    "hex_id": cell,
                    "h3_resolution": resolution,
                    "edge_count": len(edge_list),
                    "network_count": len(networks),
                    "networks": ";".join(networks),
                    "center_lat": lat,
                    "center_lon": lon,
                    "edge_ids_preview": ";".join(edge_list[:30]),
                    "more_edges": max(0, len(edge_list) - 30),
                },
            }
        )

    hexes_geojson = {
        "type": "FeatureCollection",
        "features": hex_features,
    }

    return mapping_df, edges_geojson, hexes_geojson


def build_simple_edge_hex_dictionary(mapping_df: pd.DataFrame, edge_id_format: str) -> pd.DataFrame:
    """
    Create the requested minimal two-column dictionary: hex_id,edge_id.

    edge_id_format:
      - raw:       edge_id is the original SUMO edge id.
      - networked: edge_id is network::edge_id, useful when multiple networks
                   may contain the same raw edge id.
    """
    if mapping_df.empty:
        return pd.DataFrame(columns=["hex_id", "edge_id"])

    simple = mapping_df[["h3_cell", "edge_id"]].copy()
    simple = simple.rename(columns={"h3_cell": "hex_id"})

    if edge_id_format == "networked":
        simple["edge_id"] = mapping_df["network"].astype(str) + "::" + mapping_df["edge_id"].astype(str)
    elif edge_id_format != "raw":
        raise ValueError("edge_id_format must be 'raw' or 'networked'.")

    simple = simple[["hex_id", "edge_id"]].drop_duplicates()
    return simple.sort_values(["hex_id", "edge_id"]).reset_index(drop=True)


def warn_duplicate_edge_ids(mapping_df: pd.DataFrame, edge_id_format: str) -> None:
    if mapping_df.empty or edge_id_format != "raw" or "network" not in mapping_df.columns:
        return

    duplicate_edges = (
        mapping_df[["network", "edge_id"]]
        .drop_duplicates()
        .groupby("edge_id", dropna=False)["network"]
        .nunique()
    )
    duplicate_count = int((duplicate_edges > 1).sum())
    if duplicate_count:
        print(
            "WARNING: "
            f"{duplicate_count} raw edge_id values appear in more than one network. "
            "The minimal hex_id,edge_id CSV will still be written, but consider "
            "--simple-edge-id-format networked to avoid ambiguity."
        )


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def create_interactive_map(
    edges_geojson: dict,
    hexes_geojson: dict,
    out_html: Path,
    map_max_edges: int,
    map_max_hexes: int,
) -> None:
    """
    Create a Folium map with H3 polygons and SUMO edges.
    Large networks can make HTML files heavy, so map rendering can be capped.
    CSV/GeoJSON exports are always complete.
    """
    all_coords = []

    for feat in edges_geojson["features"]:
        coords = feat["geometry"]["coordinates"]
        all_coords.extend(coords)

    if not all_coords:
        raise ValueError("No coordinates found for map creation.")

    avg_lon = sum(c[0] for c in all_coords) / len(all_coords)
    avg_lat = sum(c[1] for c in all_coords) / len(all_coords)

    fmap = folium.Map(location=[avg_lat, avg_lon], zoom_start=12, tiles="CartoDB positron")

    rendered_hexes = {
        "type": "FeatureCollection",
        "features": hexes_geojson["features"][:map_max_hexes],
    }

    rendered_edges = {
        "type": "FeatureCollection",
        "features": edges_geojson["features"][:map_max_edges],
    }

    folium.GeoJson(
        rendered_hexes,
        name=f"H3 hexes, rendered {len(rendered_hexes['features'])}/{len(hexes_geojson['features'])}",
        style_function=lambda feature: {
            "fillColor": "#ff7800",
            "color": "#222222",
            "weight": 0.7,
            "fillOpacity": min(0.75, 0.15 + 0.04 * feature["properties"].get("edge_count", 1)),
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["h3_cell", "edge_count", "networks", "edge_ids_preview", "more_edges"],
            aliases=["H3 cell", "Edge count", "Networks", "Edge IDs preview", "More edges"],
            sticky=True,
        ),
    ).add_to(fmap)

    folium.GeoJson(
        rendered_edges,
        name=f"SUMO edges, rendered {len(rendered_edges['features'])}/{len(edges_geojson['features'])}",
        style_function=lambda feature: {
            "color": "#0055ff",
            "weight": 2,
            "opacity": 0.75,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["network", "edge_id", "lane_count", "speed_mps", "length_m_sumo", "h3_cell_count"],
            aliases=["Network", "Edge ID", "Lanes", "Speed m/s", "SUMO length m", "Mapped H3 cells"],
            sticky=True,
        ),
    ).add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.save(str(out_html))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert SUMO network edges to H3 edge-hex mapping and an interactive map."
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input",
        type=Path,
        help="Local path to the root folder containing SUMO network subfolders.",
    )
    source.add_argument(
        "--clone-repo",
        action="store_true",
        help="Sparse-clone the default Limexcyan/net2hex networks folder.",
    )

    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument("--repo-subpath", default=DEFAULT_REPO_SUBPATH)

    parser.add_argument(
        "--resolution",
        type=int,
        default=None,
        help="H3 resolution, 0-15. Use this or --hex-radius-m. If neither is given, defaults to 10.",
    )
    parser.add_argument(
        "--hex-radius-m",
        type=float,
        default=None,
        help=(
            "Choose the H3 resolution whose average hexagon edge length is closest "
            "to this meter value. For a planar regular hexagon, edge length equals circumradius."
        ),
    )

    parser.add_argument(
        "--buffer-m",
        type=float,
        default=None,
        help=(
            "Buffer radius around each edge centerline, in meters. "
            "Default is 0.35 * H3 average edge length."
        ),
    )
    parser.add_argument(
        "--sample-step-m",
        type=float,
        default=None,
        help=(
            "Distance between sampled points along each edge, in meters. "
            "Default is 0.33 * H3 average edge length."
        ),
    )
    parser.add_argument(
        "--ring-k",
        type=int,
        default=0,
        help="Add k rings of neighboring H3 cells around every sampled centerline cell.",
    )

    parser.add_argument(
        "--include-internal",
        action="store_true",
        help="Include SUMO internal/special edges. Default: skip them.",
    )
    parser.add_argument(
        "--include-junctions",
        action="store_true",
        help="Use edge geometry including junction endpoints.",
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=Path("out_h3"),
        help="Output directory.",
    )
    parser.add_argument(
        "--simple-dict-name",
        default="hex_edge_dictionary.csv",
        help="Filename for the minimal two-column hex_id,edge_id CSV.",
    )
    parser.add_argument(
        "--simple-edge-id-format",
        choices=["raw", "networked"],
        default="raw",
        help=(
            "How edge_id is written in the minimal dictionary. Use 'networked' "
            "to write network::edge_id while still keeping only hex_id,edge_id columns."
        ),
    )

    parser.add_argument(
        "--map-max-edges",
        type=int,
        default=20000,
        help="Maximum edge features rendered in HTML map. Exports remain complete.",
    )
    parser.add_argument(
        "--map-max-hexes",
        type=int,
        default=20000,
        help="Maximum H3 polygon features rendered in HTML map. Exports remain complete.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    temp_dir_obj = None

    if args.clone_repo:
        temp_dir_obj = tempfile.TemporaryDirectory()
        root = clone_repo_subfolder(
            repo_url=args.repo_url,
            subpath=args.repo_subpath,
            workdir=Path(temp_dir_obj.name),
        )
    else:
        root = args.input

    if not root.exists():
        raise FileNotFoundError(f"Input path does not exist: {root}")

    if args.hex_radius_m is not None:
        resolution = choose_resolution_from_radius_m(args.hex_radius_m)
        print(f"Chosen H3 resolution {resolution} for target radius {args.hex_radius_m:.2f} m")
    elif args.resolution is not None:
        resolution = args.resolution
    else:
        resolution = 10

    if not (0 <= resolution <= 15):
        raise ValueError("H3 resolution must be between 0 and 15.")

    avg_edge_len_m = h3_average_edge_length_m(resolution)
    buffer_m = args.buffer_m if args.buffer_m is not None else 0.35 * avg_edge_len_m
    sample_step_m = args.sample_step_m if args.sample_step_m is not None else 0.33 * avg_edge_len_m

    print(f"Input root: {root}")
    print(f"H3 resolution: {resolution}")
    print(f"H3 average edge length: {avg_edge_len_m:.3f} m")
    print(f"Edge buffer: {buffer_m:.3f} m")
    print(f"Sample step: {sample_step_m:.3f} m")
    print(f"Neighbor ring k: {args.ring_k}")

    net_files = find_net_files(root)
    if not net_files:
        raise FileNotFoundError(f"No *.net.xml files found below: {root}")

    print(f"Found {len(net_files)} SUMO .net.xml files")

    edge_records: list[dict] = []
    for net_file in tqdm(net_files, desc="Reading SUMO networks"):
        edge_records.extend(
            extract_edges_from_sumo_net(
                net_file=net_file,
                include_internal=args.include_internal,
                include_junctions=args.include_junctions,
            )
        )

    if not edge_records:
        raise ValueError("No usable SUMO edges found.")

    print(f"Extracted {len(edge_records)} edges")

    mapping_df, edges_geojson, hexes_geojson = build_mapping(
        edge_records=edge_records,
        resolution=resolution,
        buffer_m=buffer_m,
        sample_step_m=sample_step_m,
        ring_k=args.ring_k,
    )

    mapping_csv = args.out / "edge_hex_mapping.csv"
    simple_dict_csv = args.out / args.simple_dict_name
    edges_geojson_path = args.out / "edges.geojson"
    hexes_geojson_path = args.out / "hexes.geojson"
    map_html = args.out / "network_hex_map.html"

    mapping_df.to_csv(mapping_csv, index=False)

    warn_duplicate_edge_ids(mapping_df, args.simple_edge_id_format)
    simple_df = build_simple_edge_hex_dictionary(mapping_df, args.simple_edge_id_format)
    simple_df.to_csv(simple_dict_csv, index=False)

    write_json(edges_geojson_path, edges_geojson)
    write_json(hexes_geojson_path, hexes_geojson)

    create_interactive_map(
        edges_geojson=edges_geojson,
        hexes_geojson=hexes_geojson,
        out_html=map_html,
        map_max_edges=args.map_max_edges,
        map_max_hexes=args.map_max_hexes,
    )

    print("\nDone.")
    print(f"Rich edge-H3 mapping CSV:      {mapping_csv}")
    print(f"Minimal hex_id,edge_id CSV:    {simple_dict_csv}")
    print(f"Edges GeoJSON:                 {edges_geojson_path}")
    print(f"H3 hexes GeoJSON:             {hexes_geojson_path}")
    print(f"Interactive map:              {map_html}")

    if temp_dir_obj is not None:
        temp_dir_obj.cleanup()


if __name__ == "__main__":
    main()
