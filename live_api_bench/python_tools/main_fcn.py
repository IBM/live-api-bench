"""Core SQL to API translation processing module.

This module provides the main translation pipeline for converting BIRD benchmark
SQL queries into API call sequences. It processes batches of queries, validates
the translations against expected results, and generates datasets for SEL-BIRD
and SLOT-BIRD benchmarks.
"""

import json
import logging
import os
from collections import defaultdict
from typing import Any

from .execution_helpers import validate_output
from .sql_dataset_builder import SqlDatasetBuilder

# Module-level logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Default maximum items to display when condensing output
DEFAULT_CONDENSE_LIMIT = 20


def condense_output(data: Any, max_items: int = DEFAULT_CONDENSE_LIMIT) -> Any:
    """Condense list and dict outputs for logging by limiting the number of items.

    Args:
        data: The data structure to condense (list, dict, or other type).
        max_items: Maximum number of items to retain. Defaults to DEFAULT_CONDENSE_LIMIT.

    Returns:
        Condensed version of the data:
        - For lists: first max_items elements, with nested lists also condensed
        - For dicts: first max_items key-value pairs, with nested structures also condensed
        - For other types: returns data unchanged
    """
    if isinstance(data, list):
        # Truncate outer list and recursively condense each element
        truncated = data[:max_items]
        return [condense_output(item, max_items) for item in truncated]
    elif isinstance(data, dict):
        # Truncate dict and recursively condense each value
        truncated_items = list(data.items())[:max_items]
        return {k: condense_output(v, max_items) for k, v in truncated_items}
    return data


def load_skip_configuration() -> dict[str, Any]:
    """Load configuration for skipping problematic high-memory data points.

    Reads the high_memory_errors.json file which contains a mapping of database
    names to data point indices that should be skipped during translation.

    Returns:
        Dictionary mapping database names to either "all" (skip entire database)
        or a list of integer indices (skip specific data points).

    Raises:
        FileNotFoundError: If high_memory_errors.json cannot be found.
        json.JSONDecodeError: If the JSON file is malformed.
    """
    config_path = os.path.join(os.path.dirname(__file__), "high_memory_errors.json")
    with open(config_path) as f:
        return json.load(f)


def main(
    database: str,
    output_file: str,
    queries: list[str],
    questions: list[str],
    dataset_builder: SqlDatasetBuilder
) -> dict[str, Any]:
    """Process SQL queries and translate them into API call sequences.

    This is the main entry point for the translation pipeline. It processes each
    SQL query from the BIRD benchmark, translates it to API calls, validates the
    results, and generates dataset entries for successful translations.

    Args:
        database: Name of the BIRD database being processed (e.g., "california_schools").
        output_file: Path where the generated dataset JSON should be written.
        queries: List of SQL queries to translate.
        questions: List of natural language questions corresponding to each query.
        dataset_builder: SqlDatasetBuilder instance (selection or slot-filling mode).

    Returns:
        Dictionary containing translation statistics:
        - total_queries (int): Total number of queries processed
        - correct_queries (int): Number of successfully translated queries
        - incorrect_queries (int): Number of queries with validation mismatches
        - errored_queries (int): Number of queries that raised exceptions
        - errors (dict): Mapping of error messages to lists of affected indices
    """
    logger.info(f"Starting translation for database '{database}' with {len(queries)} queries")

    failure_count = incorrect_count = correct_count = 0
    sql_to_api_translations = []
    errors = defaultdict(list)

    # Load configuration for skipping problematic data points
    points_to_skip = load_skip_configuration()

    for idx, (question, query) in enumerate(zip(questions, queries)):
        try:
            # Skip high-memory data points based on configuration
            if database in points_to_skip:
                if points_to_skip[database] == "all" or idx in points_to_skip[database]:
                    raise Exception(f"High memory datapoint, skipping: {database}, {idx}")
            # Translate SQL query to API call sequence
            required_api_calls, key_names_and_descriptions = dataset_builder.translate_query_from_sql_tree(query)
            api_pool = dataset_builder.toolbox.get_toolbox_with_schema(key_names_and_descriptions)
            database_file = dataset_builder.loader.cache_file

            # Validate that API calls produce same result as SQL query
            correct, query_result, api_result = validate_output(database_file, query, required_api_calls, api_pool)

            if correct:
                # Successful translation - add to dataset
                payload = {
                    "query": query,
                    "input": question,
                    "dataset_name": database,
                    "sample_id": idx,
                    "gold_answer": api_result,
                    "output": [{'name': r['name'], 'arguments': r['arguments'], 'label': r['label']} for r in required_api_calls],  # Leave off callable 'fcn'
                    "key_values_and_descriptions": key_names_and_descriptions
                }

                sql_to_api_translations.append(payload)
                correct_count += 1
                logger.warning(f"Datapoint {idx} out of {(len(questions))} == Success")
            else:
                # Translation produced different result than SQL query
                logger.warning("\n" + "=" * 40)
                logger.warning(f"QUERY {idx} / {len(queries)} = {query}")
                logger.warning(f"QUERY RESULTS = {condense_output(query_result)}")
                logger.warning(f"API RESULTS = {condense_output(api_result)}")
                logger.warning(f"ANSWERS MATCH = {correct}")
                logger.warning("=" * 40 + "\n")
                incorrect_count += 1

            logger.info(f"DATA POINT {idx} -> SCORE = {correct}")
            dataset_builder.cleanup_temp_files(keep_db=True)

        except Exception as e:
            # Translation or validation failed
            failure_count += 1
            logger.error("=" * 40)
            logger.error(f"DATAPOINT NUMBER: {idx} / {len(queries)}")
            logger.error(f"QUESTION = {question}")
            logger.error(f"QUERY = {query}")
            logger.error(f"FAILED: {e}")
            logger.error("=" * 40)

            try:
                es = str(e)
                errors[es].append(idx)
            except Exception:
                # Failed to record error, continue processing
                pass

    # Log final summary
    logger.warning("\n" + "*" * 40)
    logger.warning(
        f"There were {correct_count} correct answers, {incorrect_count} incorrect answers "
        f"and {failure_count} failures out of a total of {len(queries)} queries."
    )
    logger.warning("*" * 40 + "\n")

    dataset_builder.cleanup_temp_files()

    report = {
        "total_queries": len(queries),
        "correct_queries": correct_count,
        "incorrect_queries": incorrect_count,
        "errored_queries": failure_count,
        "errors": errors
    }

    # Write dataset to output file
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    if len(sql_to_api_translations) > 0:
        logger.info(f"Dumping to {output_file}")
        with open(output_file, 'w') as f:
            json.dump(sql_to_api_translations, f)
    else:
        logger.warning(f"Not creating output file for empty results from dataset {database}")

    return report
