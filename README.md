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
| `-d`, `--dataset` | any BIRD db name | — | Process a single named database (overrides `-m`) |
| `-api`, `--api_style` | `slot`, `sel` | `slot` | Output format: slot-filling or selection |
| `--db-path` | any path | see above | Path to the BIRD database directory |

### Examples

```bash
# Defaults: all dev databases, slot-filling output
python live_api_bench/python_tools/run_bird_translation.py

# All train databases, selection output
python live_api_bench/python_tools/run_bird_translation.py -m train -api sel

# Single specific database
python live_api_bench/python_tools/run_bird_translation.py -d formula_1 -api slot
```

### Output

Results are written to `output/slot/<dataset>.json` or `output/sel/<dataset>.json`. Each file is a list of entries with the translated API call sequence, the original SQL and natural-language question, the gold answer, and the tool specifications used.

## Query Support

Not all BIRD SQL queries can be translated. The pipeline supports a subset of SQL patterns; unsupported patterns are skipped and logged in the run report.

### Supported

- Simple `SELECT` with column projections, `WHERE` filters (equality, `<`, `>`, `LIKE`), `ORDER BY`, and `LIMIT`
- `INNER JOIN` across two or more tables on a single equality condition
- Aggregations in `SELECT`: `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`
- `GROUP BY` with aggregations
- `CAST` expressions in `SELECT` (Slot-filling only)
- Arithmetic expressions in `SELECT` (e.g. `col1 / col2`, `col1 - col2`) (slot-filling only)
- `DISTINCT`

### Not Supported

| Pattern | Affects | Notes |
|---|---|---|
| Subqueries (any nested `SELECT`) | both | Includes scalar subqueries, correlated `EXISTS`, derived tables in `FROM`, and multi-`SELECT` arithmetic. The most common failure category. |
| Arithmetic in `WHERE` | both | Expressions like `WHERE col1 / col2 > 0.3` or `WHERE col1 + col2 > 500` are not parsed. Arithmetic is only supported in `SELECT`. |
| Window functions (`RANK() OVER`, etc.) | both | No translation strategy exists for window functions. |
| `!=` / `<>` operator in `WHERE` | both | Only equality and inequality comparisons (`=`, `<`, `>`, `<=`, `>=`, `LIKE`) are handled. |
| Math functions in `ORDER BY` (`ABS`, etc.) | both | Math function calls in `ORDER BY` are not recognized as column expressions. |
| Multi-condition `JOIN ON` | both | `JOIN t ON t.a = x AND t.b = y` — only a single equality per `ON` clause is supported. |
| `IN (val, val, ...)` in `WHERE` | both | Literal-list `IN` clauses are not handled by the WHERE parser. |
| Multiple `OR` in `WHERE` | both | Arbitrary numbers of `AND` clauses and up to one `OR` clause is supported, but two or more `OR` clauses are not. |
| Missing column getter | sel only | If a column appears in `SELECT` but no getter exists in the toolbox for it, translation fails. |

For a detailed breakdown with sample IDs and fix-complexity estimates, see [QUERY_SUPPORT.md](QUERY_SUPPORT.md).


### Vakra Compatibility

To run the translated benchmark against the vakra MCP server, each sample must be registered in `environment/configs/mcp_tool_universe_id_mapping.yaml` inside the vakra repo. The MCP server loads this file at startup and uses it as a lookup table: given a sample's UUID, it retrieves the `initialize_active_data` arguments (table aliases, join sequence, database path) and the server type (`slot_filling` or `selection`) needed to set up the data environment before the agent starts issuing tool calls.

#### Why the mapping file is needed

Each benchmark sample targets a specific set of tables in a specific SQLite database, possibly with aliased table names and a join sequence that must be established before any tool calls can execute. Rather than embedding this configuration in the benchmark input itself, the MCP server externalizes it: the agent passes a `tool_universe_id` (the sample UUID) to `get_data()`, and the server looks up the corresponding configuration in the YAML to initialize the active data correctly.

#### Regenerating the mapping

After placing new benchmark output files in `data/test/capability_1_bi_apis/output/` inside the vakra repo, run:

```bash
# From the vakra repo root
python environment/configs/generate_task_1_tool_universe_mapping.py
```

This scans all `*.json` files under `data/test/capability_1_bi_apis/output/`, extracts the `initialize_active_data` arguments and server type from each sample's ground-truth tool call sequence, and writes the result to `environment/configs/mcp_tool_universe_id_mapping.yaml`.

Pass `--dry-run` to print the entry count without writing the file.

#### Input format expected by the generate script

Each JSON file must be a list of records in vakra's output format:

```json
{
  "uuid": "<unique sample ID>",
  "domain": "<database name>",
  "output": [
    {
      "turn_id": 0,
      "query": "<natural language question>",
      "sequence": [
        {
          "tool_call": [
            {
              "name": "initialize_active_data",
              "arguments": {
                "alias_to_table_dict": { ... },
                "condition_sequence": [ ... ],
                "database_path": "<path to .sqlite>"
              },
              "label": "starting_table_var"
            },
            ...
          ]
        }
      ]
    }
  ]
}
```

The server type (`slot_filling` or `selection`) is inferred automatically from which tool names appear in the tool call sequence. Note that this format differs from live-api-bench's native output (which uses `sample_id`/`dataset_name` and a flat tool call list); a conversion step is required before running the generate script.
