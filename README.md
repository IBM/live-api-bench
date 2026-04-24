# live-api-bench

Converts [BIRD benchmark](https://bird-bench.github.io/) SQL queries into API call sequences, producing two benchmark types:

- **SLOT-BIRD** (`slot`): Slot-filling-style — generic data manipulation functions
- **SEL-BIRD** (`sel`): Selection-style — specific data manipulation functions and table dependent getter functions

## Installation

### 1. Clone this repo

```bash
git clone <git@github.com:IBM/live-api-bench.git> live-api-bench
cd live-api-bench
```

### 2. Install Python packages

```bash
pip install -r requirements.txt
```

This installs [`vakra-benchmark`](https://github.com/IBM/vakra) (the tool-calling library) along with other dependencies.

### 3. Place the BIRD databases

The pipeline reads SQLite databases and query JSON files from a database directory. The location is resolved in this order:

1. `--db-path <path>` CLI argument
2. `BIRD_DB_PATH` environment variable
3. Default: `db/` at the repo root

The expected layout is:

```
live-api-bench/db/
├── train.json               # BIRD train query set
├── dev.json                 # BIRD dev query set
├── train_queries/           # Per-dataset prefiltered query files (auto-generated)
├── dev_queries/             # Per-dataset prefiltered query files (auto-generated)
├── train_databases/         # BIRD train SQLite databases
│   └── <dataset>/
│       ├── <dataset>.sqlite
│       └── database_description/
│           └── *.csv
└── dev_databases/           # BIRD dev SQLite databases
    └── <dataset>/
        ├── <dataset>.sqlite
        └── database_description/
            └── *.csv
```

Download the BIRD benchmark data from [bird-bench.github.io](https://bird-bench.github.io/) and place `train.json` / `dev.json` and the `train_databases/` / `dev_databases/` folders under `db/`.

## Usage

Run the translation pipeline from the `live-api-bench` directory:

```bash
python live_api_bench/python_tools/run_bird_translation.py [OPTIONS]
```

### Options

| Flag | Choices | Default | Description |
|------|---------|---------|-------------|
| `-m`, `--mode` | `train`, `dev` | `dev` | Which BIRD split to use |
| `-s`, `--size` | `small`, `large` | `small` | Process one representative database or the full set |
| `-d`, `--dataset` | any BIRD db name | — | Process a single named database (overrides `-m`/`-s`) |
| `-api`, `--api_style` | `slot`, `sel` | `slot` | Output format: slot-filling or selection |
| `--db-path` | any path | see above | Path to the BIRD database directory |

### Examples

```bash
# Defaults: dev split, small (california_schools), slot-filling output
python live_api_bench/python_tools/run_bird_translation.py

# Train split, small (disney), selection output
python live_api_bench/python_tools/run_bird_translation.py -m train -api sel

# All dev databases, slot-filling output
python live_api_bench/python_tools/run_bird_translation.py -m dev -s large

# Single specific database
python live_api_bench/python_tools/run_bird_translation.py -d formula_1 -api slot
```

### Output

Results are written to `output/slot/<dataset>.json` or `output/sel/<dataset>.json`. Each file is a list of entries with the translated API call sequence, the original SQL and natural-language question, the gold answer, and the tool specifications used.
