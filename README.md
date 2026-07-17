# net2hex

Convert [SUMO](https://eclipse.dev/sumo/) road networks into [H3](https://h3geo.org/) hexagonal spatial indexes, export edge-to-cell mappings, and visualize the results on interactive maps.

`net2hex` supports two related workflows:

1. **Full-network conversion** — map every usable SUMO edge to one or more H3 cells.
2. **Visited-edge extraction** — reduce a full mapping to only the edges and H3 cells observed in route or simulation data.

The repository also includes selected [Urban Routing Benchmark (URB)](https://github.com/COeXISTENCE-PROJECT/URB) networks and example outputs for Ingolstadt, Provins, and Saint-Arnoult.

## Features

- Reads SUMO `*.net.xml` road networks recursively.
- Converts SUMO coordinates to longitude and latitude.
- Maps road centerlines to H3 cells using line sampling and metric buffering.
- Supports explicit H3 resolutions or approximate cell-radius selection.
- Exports rich and minimal edge-to-H3 lookup tables.
- Produces GeoJSON for road edges and H3 polygons.
- Creates interactive Folium maps.
- Extracts visited edges from plain edge lists or route/path snapshot CSV files.
- Handles multiple SUMO networks and optionally namespaces duplicate edge IDs.
- Limits only HTML rendering for large networks while keeping CSV and GeoJSON exports complete.

## Repository layout

```text
net2hex/
├── sumo_networks_to_h3_updated.py   # Recommended full-network converter
├── sumo_networks_to_h3.py           # Earlier/basic converter
├── visited_edges_to_h3_subset.py    # Filter a mapping to visited edges
├── requirements_h3_sumo.txt
├── all_departures.csv               # Example route/departure data
├── networks/                        # Included SUMO/URB networks
├── ingolstadt/                      # Example full-network outputs
├── provins/                         # Example full-network outputs
├── saint_arnoult/                   # Example full-network outputs
└── subset_ingolstadt/               # Example visited-edge subset
```

## Requirements

- Python 3.10 or newer
- `git`, only when using `--clone-repo`
- A georeferenced SUMO `*.net.xml` network whose coordinates can be converted with `sumolib`

Python dependencies:

- `h3`
- `pandas`
- `folium`
- `pyproj`
- `shapely`
- `tqdm`
- `sumolib`

## Installation

```bash
git clone https://github.com/Limexcyan/net2hex.git
cd net2hex

python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements_h3_sumo.txt
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

## Quick start

Convert all SUMO networks below the included URB network directory:

```bash
python sumo_networks_to_h3_updated.py \
  --input "networks/COeXISTENCE-PROJECT URB main networks" \
  --resolution 10 \
  --buffer-m 20 \
  --out out_h3
```

The output directory will contain:

```text
out_h3/
├── edge_hex_mapping.csv
├── hex_edge_dictionary.csv
├── edges.geojson
├── hexes.geojson
└── network_hex_map.html
```

Open `network_hex_map.html` in a web browser to inspect the mapped road edges and H3 cells.

## Full-network conversion

Use `sumo_networks_to_h3_updated.py` for new conversions. It recursively finds every `*.net.xml` file below the input directory.

### Use a local network directory

```bash
python sumo_networks_to_h3_updated.py \
  --input path/to/networks \
  --resolution 10 \
  --out out_h3
```

The input directory may contain one network or multiple network subdirectories.

### Clone the bundled network source automatically

```bash
python sumo_networks_to_h3_updated.py \
  --clone-repo \
  --resolution 10 \
  --buffer-m 20 \
  --out out_h3
```

This performs a sparse clone of the repository subfolder configured by the script. `git` must be available on the command line.

### Select an approximate hexagon radius

H3 uses integer resolution levels rather than an exact radius in metres. The following command selects the H3 resolution whose average edge length is closest to the requested value:

```bash
python sumo_networks_to_h3_updated.py \
  --input path/to/networks \
  --hex-radius-m 65 \
  --buffer-m 20 \
  --out out_h3
```

When both `--hex-radius-m` and `--resolution` are supplied, the radius-based selection takes precedence. For reproducible experiments, prefer recording the selected integer H3 resolution.

### Mapping method

For every SUMO edge, the converter:

1. reads the edge geometry with `sumolib`;
2. converts SUMO coordinates to WGS84 longitude/latitude;
3. selects a local UTM coordinate reference system;
4. samples points along the edge centerline;
5. assigns sampled points to H3 cells;
6. optionally adds neighbouring H3 rings;
7. buffers the centerline in metres and adds cells covered by the buffered geometry;
8. writes the resulting edge–cell relations and visualization files.

Combining sampling and buffering improves coverage of narrow linear road geometries because H3 polygon filling is based on cell centers.

### Main options

| Option | Description | Default |
|---|---|---:|
| `--input PATH` | Root directory containing SUMO `*.net.xml` files | — |
| `--clone-repo` | Sparse-clone the configured repository network folder | off |
| `--resolution N` | H3 resolution from `0` to `15` | `10` |
| `--hex-radius-m M` | Select the closest H3 resolution by average edge length | — |
| `--buffer-m M` | Buffer around each edge centerline in metres | `0.35 ×` H3 average edge length |
| `--sample-step-m M` | Distance between centerline samples | `0.33 ×` H3 average edge length |
| `--ring-k K` | Add `K` neighbouring H3 rings around sampled cells | `0` |
| `--include-internal` | Include SUMO internal or special edges | off |
| `--include-junctions` | Include junction endpoints in edge geometry | off |
| `--out PATH` | Output directory | `out_h3` |
| `--simple-dict-name NAME` | Name of the minimal lookup CSV | `hex_edge_dictionary.csv` |
| `--simple-edge-id-format FORMAT` | Use `raw` or `networked` edge IDs | `raw` |
| `--map-max-edges N` | Maximum edges rendered in the HTML map | `20000` |
| `--map-max-hexes N` | Maximum hexagons rendered in the HTML map | `20000` |

Run the built-in help for the complete interface:

```bash
python sumo_networks_to_h3_updated.py --help
```

## Full-network outputs

### `edge_hex_mapping.csv`

A rich table with one row for every edge–H3-cell relation. It includes fields such as:

- network name;
- SUMO edge ID;
- H3 cell ID and resolution;
- projected and SUMO edge lengths;
- lane count and speed;
- edge type and function;
- mapping buffer, sample interval, and ring size;
- source network file.

An edge may appear in multiple rows because a road can cross multiple H3 cells.

### `hex_edge_dictionary.csv`

A minimal two-column lookup:

```csv
hex_id,edge_id
8a1f8d...,123456
8a1f8d...,123457
```

Use `--simple-edge-id-format networked` when different networks may reuse the same raw SUMO edge ID:

```csv
hex_id,edge_id
8a1f8d...,ingolstadt_custom2::123456
```

### `edges.geojson`

Complete road-edge geometries in WGS84, with SUMO and mapping metadata stored as feature properties.

### `hexes.geojson`

H3 polygon geometries with properties including edge counts, participating networks, cell centers, and previews of associated edge IDs.

### `network_hex_map.html`

An interactive Folium map containing separate road-edge and H3 layers. The map may render only the first configured number of features, but the CSV and GeoJSON files remain complete.

## Extracting a visited-edge subset

After creating a full mapping, use `visited_edges_to_h3_subset.py` to retain only the edges and H3 cells found in simulation results or route data.

### Route/path snapshot input

The script can parse route values stored as Python tuples or lists, JSON-like lists, or comma-, semicolon-, and space-separated strings.

Example input:

```csv
exp_id,time,agent_id,path
0,6,0,"('315358244', '379897807', '137685155#0')"
```

Run:

```bash
python visited_edges_to_h3_subset.py \
  --mapping ingolstadt/edge_hex_mapping.csv \
  --visited-edges all_departures.csv \
  --input-format paths \
  --path-column path \
  --time-column time \
  --agent-column agent_id \
  --experiment-column exp_id \
  --edges-geojson ingolstadt/edges.geojson \
  --hexes-geojson ingolstadt/hexes.geojson \
  --out subset_ingolstadt
```

### Automatic input detection

When a conventional route column such as `path`, `route`, `edge_path`, or `visited_edges` is present, the default `auto` mode attempts to detect it:

```bash
python visited_edges_to_h3_subset.py \
  --mapping ingolstadt/edge_hex_mapping.csv \
  --visited-edges all_departures.csv \
  --edges-geojson ingolstadt/edges.geojson \
  --hexes-geojson ingolstadt/hexes.geojson \
  --out subset_ingolstadt
```

### Plain edge-list input

CSV:

```csv
edge_id
315358244
379897807
137685155#0
```

or a text file with one edge ID per line:

```text
315358244
379897807
137685155#0
```

Run:

```bash
python visited_edges_to_h3_subset.py \
  --mapping out_h3/edge_hex_mapping.csv \
  --visited-edges visited_edges.csv \
  --input-format edge-list \
  --out out_visited_h3
```

For a nonstandard CSV column name, provide `--edge-column`. For multi-network data, provide a network column or use IDs in the form `network::edge_id`.

### Visited-edge options

| Option | Description | Default |
|---|---|---:|
| `--mapping PATH` | Full `edge_hex_mapping.csv` | required |
| `--visited-edges PATH` | Edge list or route/path table | required |
| `--input-format FORMAT` | `auto`, `edge-list`, or `paths` | `auto` |
| `--edge-column NAME` | Edge-ID column for edge-list input | auto-detect |
| `--path-column NAME` | Route/path column | auto-detect |
| `--network-column NAME` | Optional network identifier column | auto-detect |
| `--time-column NAME` | Optional simulation-time column | auto-detect |
| `--agent-column NAME` | Optional agent or vehicle column | auto-detect |
| `--experiment-column NAME` | Optional experiment/run column | auto-detect |
| `--edges-geojson PATH` | Full edge GeoJSON used for the subset overlay | — |
| `--hexes-geojson PATH` | Full hex GeoJSON used to preserve/filter polygons | — |
| `--simple-edge-id-format FORMAT` | `raw` or `networked` output IDs | `raw` |
| `--no-map` | Skip HTML-map generation | off |
| `--debug` | Print edge-matching diagnostics | off |
| `--out PATH` | Output directory | `out_visited_h3` |

Run:

```bash
python visited_edges_to_h3_subset.py --help
```

for all available options.

## Visited-edge outputs

```text
out_visited_h3/
├── visited_edges_extracted.csv
├── visited_edge_hex_mapping.csv
├── visited_edge_hex_dictionary.csv
├── visited_hexes.csv
├── visited_hexes.geojson
├── visited_edges.geojson          # when --edges-geojson is supplied
└── visited_network_hex_map.html   # unless --no-map is supplied
```

- `visited_edges_extracted.csv` — normalized unique visited edges with visit counts and available snapshot metadata.
- `visited_edge_hex_mapping.csv` — full mapping rows restricted to visited edges.
- `visited_edge_hex_dictionary.csv` — minimal `hex_id,edge_id` lookup for visited edges.
- `visited_hexes.csv` — one row per used H3 cell, including visit and edge summaries.
- `visited_hexes.geojson` — polygons for used H3 cells.
- `visited_edges.geojson` — filtered road geometries.
- `visited_network_hex_map.html` — interactive subset visualization.

## Choosing mapping parameters

- **Higher H3 resolution** produces smaller cells and a more detailed mapping.
- **Smaller `--sample-step-m` values** sample roads more densely but increase runtime and output size.
- **Larger `--buffer-m` values** associate roads with more nearby cells.
- **Positive `--ring-k` values** deliberately expand coverage around every sampled cell.
- Use consistent parameters across networks when comparing spatial statistics.
- Record the H3 resolution rather than only the requested approximate radius.

The correct settings depend on whether the mapping is intended for coarse regional aggregation, local traffic-state features, visualization, or route-level analysis.

## Working with multiple networks

SUMO edge IDs are not guaranteed to be globally unique. When processing several networks together, use:

```bash
--simple-edge-id-format networked
```

This keeps the minimal CSV at two columns while representing an edge as:

```text
network_name::edge_id
```

The rich mapping always retains the network name separately.

## Performance notes

- GeoJSON and CSV exports are complete even when the HTML map is capped with `--map-max-edges` or `--map-max-hexes`.
- Use `--no-map` in the subset script when the HTML visualization is unnecessary.
- Large networks, high H3 resolutions, small sampling intervals, and nonzero neighbour rings can significantly increase the number of edge–cell relations.
- For large experiments, generate the full network mapping once and reuse it for multiple visited-edge datasets.

## Troubleshooting

### No `*.net.xml` files found

Check that `--input` points to a directory containing SUMO network files, either directly or in subdirectories.

### Coordinates cannot be converted correctly

The converter relies on the SUMO network projection and `net.convertXY2LonLat`. Verify that the network contains valid georeferencing information.

### Visited edges do not match the mapping

Run the subset script with:

```bash
--debug
```

Also check:

- whether the visited file uses the same SUMO edge IDs as the mapped network;
- whether internal edges were omitted during full conversion;
- whether network names are required to disambiguate duplicate edge IDs;
- whether route strings are stored in the expected column.

### Duplicate edge IDs across networks

Regenerate the minimal dictionaries with:

```bash
--simple-edge-id-format networked
```

### The HTML map is too large

Lower `--map-max-edges` and `--map-max-hexes`, or use `--no-map` for visited-edge subsets. These settings do not truncate CSV or GeoJSON exports.

## Legacy converter

`sumo_networks_to_h3.py` is the earlier/basic full-network converter. It produces:

- `edge_hex_mapping.csv`;
- `edges.geojson`;
- `hexes.geojson`;
- `network_hex_map.html`.

For new work, prefer `sumo_networks_to_h3_updated.py`, which additionally creates the minimal `hex_edge_dictionary.csv` and provides explicit raw/networked edge-ID handling.

## Data provenance

The included network collection is derived from resources used by the [COeXISTENCE Urban Routing Benchmark](https://github.com/COeXISTENCE-PROJECT/URB). Consult the original projects and individual data files for their provenance, terms, and citation requirements.

## Contributing

Issues and pull requests are welcome. Useful contributions include:

- tests for additional SUMO network projections;
- validation against alternative edge-to-grid mapping methods;
- packaging and command-line entry points;
- performance improvements for large networks;
- examples for downstream traffic-analysis or reinforcement-learning pipelines.

## License

No license file is currently included in this repository. Until a license is added, copyright law reserves reuse and redistribution rights to the repository owner. The included network datasets may also have separate upstream terms.
