# Architecture

This directory implements a pipeline that converts BIRD benchmark SQL queries into API call sequences for two benchmark formats: SEL-BIRD (selection-style) and SLOT-BIRD (slot-filling-style).

## File Overview

### Entry Points

**`run_bird_translation.py`**
CLI entry point. Parses arguments (`-m`, `-s`, `-d`, `-api`), resolves which databases to process, instantiates the appropriate loader and dataset builder, and calls `main()`.

**`main_fcn.py`**
Core processing loop. Iterates over queries, calls `dataset_builder.translate_query_from_sql_tree()` on each, validates results via `validate_output()`, skips entries listed in `high_memory_errors.json`, and writes the output JSON files.

### Dataset Builders

**`sql_dataset_builder.py`** (`SqlDatasetBuilder`)
Abstract base class. Contains SQL AST parsing logic shared by both benchmark types: `parse_select_clause()`, `parse_where_clause()`, `parse_transform()`, and `identify_expression_structure()` (converts SQL expressions into structured dicts). Subclasses must implement `translate_query_from_sql_tree()`.

**`sql_selection_dataset_builder.py`** (`SqlSelectionDatasetBuilder`)
Translates SQL into SEL-BIRD format using dynamic getter functions (`get_<col>s`) and aggregation compute functions (`compute_data_<agg>`). Handles WHERE filters, ORDER BY (including arithmetic expressions), and SELECT projections.

**`sql_slot_filling_dataset_builder.py`** (`SqlSlotFillingDatasetBuilder`)
Translates SQL into SLOT-BIRD format using `retrieve_data` and related slot-filling tools. The model fills argument slots in a fixed API schema rather than choosing among getter functions.

### Database Loading

**`database_loader.py`** (`DatabaseLoader`)
Base class for database access. Copies the source SQLite file to a cache location with safe column names applied. Subclasses implement `load_lazy()` and `_load_keys()`.

**`bird_database_loader.py`** (`BirdDatabaseLoader`)
BIRD-specific loader. Reads schema metadata from `database_description/*.csv` files and primary/foreign key relationships from `train_tables.json` / `dev_tables.json`. Provides `get_query_specific_columns_and_descriptions()` to scope the API toolbox to columns relevant to a given query.

### Helpers and Utilities

**`execution_helpers.py`**
Validates translations by executing both the original SQL query and the translated API call sequence against the cached database, then comparing results with `check_equality_without_order()` (order-insensitive). Also contains `execute_api_stack()`, which resolves `$VAR_NAME$` references between sequential API calls.

**`sql_query_components.py`**
Low-level SQLite utilities: safe column/table name handling (`make_safe`, `safe_name_columns`, `make_query_safe`), SQLite reserved keyword detection, and basic database operations (`database_get_connection`, `database_get_table`, etc.).

**`utils.py`**
SQL parsing utilities built on sqlglot: `get_tables_and_aliases()`, `get_join_sequences()`, and `safe_cast()` / `clean_for_json()` for JSON-safe serialization of numpy and decimal types.

### Data

**`high_memory_errors.json`**
List of query identifiers that are skipped during processing due to excessive memory usage.

## Data Flow

```
run_bird_translation.py
  └─ main_fcn.py
       ├─ BirdDatabaseLoader      ← loads + caches SQLite DB, scopes columns per query
       ├─ SqlSelectionDatasetBuilder  (or SqlSlotFillingDatasetBuilder)
       │    └─ SqlDatasetBuilder  ← parses SQL AST via sqlglot
       └─ execution_helpers.py    ← validates output by comparing SQL vs API results
```

## Output Format

Each output JSON file is a list of entries:

```json
{
  "query": "<SQL string>",
  "input": "<natural language question>",
  "dataset_name": "<database name>",
  "sample_id": 0,
  "gold_answer": "<API execution result>",
  "output": [{"name": "...", "arguments": {}, "label": "..."}],
  "key_values_and_descriptions": {},
  "tools": []
}
```

Variable references between API calls use `$VAR_NAME$` syntax; subkey access uses `$VAR_NAME.subkey$`.
