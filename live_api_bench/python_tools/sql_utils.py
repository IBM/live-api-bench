from collections import defaultdict
from decimal import Decimal
import math
import sqlglot

import numpy as np


def clean_for_json(obj):
    if isinstance(obj, dict):
        return {str(k): clean_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_for_json(v) for v in obj]
    elif isinstance(obj, set):
        return [clean_for_json(v) for v in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, (int, str, bool)) or obj is None:
        return obj
    else:
        # fallback: convert unknown types to string
        return str(obj)


def safe_cast(obj):
    """
    Recursively convert numpy data types to base Python types for JSON serialization.

    Args:
        obj: A list, tuple, list of tuples, string, or numeric type (int/float).

    Returns:
        A JSON serializable version of the input, with numpy types converted to native Python types.
    """

    # If the object is a numpy scalar (e.g., np.int64, np.float64, etc.), convert it to a native type
    if isinstance(obj, np.generic):
        return obj.item()

    elif isinstance(obj, Decimal):
        return float(obj)

    # If the object is a numpy array, recursively apply the conversion to its elements
    elif isinstance(obj, np.ndarray):
        return [safe_cast(item) for item in obj]

    # If the object is a list or a tuple, recursively convert elements
    elif isinstance(obj, (list, tuple)):
        return [safe_cast(item) for item in obj]

    # If the object is a string or numeric, just return it (strings and numbers are JSON serializable)
    else:
        return obj


def get_tables_and_aliases(tree: sqlglot.exp.Expression) -> dict[str, str]:
    # Get aliases
    alias_to_table_dict = {}
    tables = defaultdict(list)
    for t in tree.find_all(sqlglot.exp.Table):
        table_name = str(t.this)
        table_alias = t.alias
        tables[table_name].append(table_alias)

    for table_name in tables.keys():
        count = 0
        for table_alias in tables[table_name]:
            modified_table_name = table_name
            if table_alias != '':
                if count > 0:
                    modified_table_name += "_" + str(count)
            alias_to_table_dict[table_alias] = {
                'original_table_name': table_name,
                'modified_table_name': modified_table_name,
            }
            count += 1
    return alias_to_table_dict


def get_join_sequences(tree: sqlglot.exp.Expression) -> list[tuple[str]]:
    # Identify columns and join types from the expression
    condition_sequences = []
    for j in tree.find_all(sqlglot.exp.Join):
        join_type = j.kind
        condition = str(j).split("ON")[1].split("=")
        col1 = condition[0].strip()
        col2 = condition[1].strip()
        condition_sequences.append((col1, col2, join_type))
    return condition_sequences