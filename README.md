# net2hex

`net2hex` converts SUMO road networks into H3 hexagons.

It can:

* convert SUMO road edges to H3 cells;
* create edge-to-H3 mapping files;
* export GeoJSON files;
* create interactive HTML maps;
* extract only the edges and hexagons visited during a simulation.

## Installation

Clone the repository, create a virtual environment, activate it, and install the required dependencies.

```bash
git clone https://github.com/Limexcyan/net2hex.git
cd net2hex

python -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements_h3_sumo.txt
```

On Windows PowerShell, activate the virtual environment with:

```powershell
.venv\Scripts\Activate.ps1
```

## Requirements

* Python 3.10+
* A georeferenced SUMO `.net.xml` network

Main Python packages:

* `h3`
* `pandas`
* `folium`
* `pyproj`
* `shapely`
* `tqdm`
* `sumolib`

---

## 1. Convert a SUMO network with H3 resolution

Use this when you want to choose the H3 resolution directly.

```bash
python sumo_networks_to_h3_updated.py \
  --input path/to/networks \
  --resolution 10 \
  --out out_h3
```

The script searches the input directory for SUMO `.net.xml` files and converts their road edges to H3 cells.

A higher H3 resolution creates smaller and more detailed hexagons.

---

## 2. Convert using an approximate hexagon size

Use this when you prefer to define the approximate H3 cell size in metres.

```bash
python sumo_networks_to_h3_updated.py \
  --input path/to/networks \
  --hex-radius-m 65 \
  --buffer-m 20 \
  --out out_h3
```

Here:

* `--hex-radius-m 65` selects an H3 resolution close to the requested cell size;
* `--buffer-m 20` adds a 20-metre buffer around each road;
* `--out out_h3` defines the output folder.

The converter automatically chooses the closest available H3 resolution.

---

## 3. Create a subset using visited edges

After generating the full H3 mapping, you can keep only roads that were actually visited during a simulation.

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

This reads routes from `all_departures.csv`, finds the corresponding SUMO edges, and creates a smaller H3 dataset containing only visited parts of the network.

---

## Full-network output

The conversion script creates files such as:

* `edge_hex_mapping.csv` — full mapping between SUMO edges and H3 cells;
* `hex_edge_dictionary.csv` — simple `hex_id, edge_id` lookup;
* `edges.geojson` — road geometries;
* `hexes.geojson` — H3 polygons;
* `network_hex_map.html` — interactive map.

A single SUMO edge can belong to several H3 cells.

---

## Visited-edge output

The subset script creates files such as:

* `visited_edges_extracted.csv` — visited SUMO edges;
* `visited_edge_hex_mapping.csv` — mapping for visited edges only;
* `visited_edge_hex_dictionary.csv` — simple H3-to-edge lookup;
* `visited_hexes.csv` — H3 cells used by visited roads;
* `visited_hexes.geojson` — visited H3 polygons;
* `visited_edges.geojson` — visited road geometries;
* `visited_network_hex_map.html` — interactive subset map.

---

## How the conversion works

For every SUMO road edge, the converter:

1. reads the road geometry;
2. converts SUMO coordinates to longitude and latitude;
3. samples points along the road;
4. converts these points to H3 cells;
5. optionally adds a buffer around the road;
6. saves the edge-to-H3 relationship.

---

## Important parameters

`--resolution`

Sets the H3 resolution directly. Higher values create smaller hexagons.

`--hex-radius-m`

Selects an H3 resolution based on an approximate cell size in metres.

`--buffer-m`

Adds an area around each road so nearby H3 cells can also be included.

`--sample-step-m`

Controls how densely points are sampled along the road.

`--ring-k`

Adds neighbouring H3 cells around sampled cells.

`--out`

Defines the output directory.

---

## Multiple networks

SUMO edge IDs are not always unique between different networks.

When working with several networks, the converter can use IDs in this form:

```text
network_name::edge_id
```

This prevents edges from different networks from being confused.

---

## Performance

Large networks can generate many H3 cells.

Runtime and output size increase when you use:

* higher H3 resolutions;
* smaller sampling steps;
* larger buffers;
* additional neighbouring H3 rings.

For repeated experiments, it is usually best to create the full network mapping once and reuse it for different visited-edge datasets.

---

## Main files

`sumo_networks_to_h3_updated.py`

Main script for converting SUMO networks to H3.

`visited_edges_to_h3_subset.py`

Creates a smaller mapping containing only visited roads and H3 cells.

`requirements_h3_sumo.txt`

Python dependencies.

`networks/`

Example SUMO networks.

`all_departures.csv`

Example route and simulation data.

---

## Troubleshooting

### No SUMO networks found

Make sure the input directory contains `.net.xml` files.

### Wrong coordinates

The SUMO network must contain valid geographic projection information.

### Visited edges are missing

Check that the edge IDs in the simulation data match the edge IDs used in the full mapping.

### HTML map is too large

Large networks can create very large interactive maps. CSV and GeoJSON outputs are better suited for processing large datasets.

---

## Data

The repository contains selected networks from the COeXISTENCE Urban Routing Benchmark.

Check the original datasets for their provenance, terms, and citation requirements.
