from collections import Counter
from typing import Callable
import json
import numpy as np
from pandas import DataFrame, Series
from pydantic import RootModel

from .sql_query_components import database_close_connection, database_get_connection, make_query_safe
from .utils import safe_cast


def execute_single_api(api_name: str, api_args: dict, api_pool: dict[str, Callable]):
    try:
        arg_fcn = api_pool[api_name]
    except:
        raise Exception(f"Fcn {api_name} not in API pool. ")
    output = arg_fcn(**api_args)
    return output

def parse_argval(arg_val: str):
    arg_val = arg_val.lstrip("$").rstrip("$")
    subkey = None
    if '.' in arg_val:
        ls = arg_val.split('.')
        arg_val = ls[0]
        subkey = ls[1]
    return arg_val, subkey

def execute_api_stack(apis: list[dict], api_pool: dict[str, Callable]):
    output_dict = {}
    for api in apis:
        api_args_filled_in = {}
        for arg_key, arg_val in api['arguments'].items():
            if isinstance(arg_val, str) and arg_val.startswith("$") and arg_val.endswith("$"):
                arg_val, subkey = parse_argval(arg_val)
                argument = output_dict[arg_val] # Throw an error if the necessary argument isn't found

                # Unwrap Pydantic RootModel instances FIRST
                if isinstance(argument, RootModel):
                    argument = argument.root

                # Then extract subkey if needed
                if subkey is not None:
                    assert isinstance(argument, dict), f"Cannot extract subkey '{subkey}' from non-dict type: {type(argument)}"
                    argument = argument[subkey]

                api_args_filled_in[arg_key] = argument
            else:
                api_args_filled_in[arg_key] = arg_val
        try:
            output = execute_single_api(api['name'], api_args_filled_in, api_pool)
            # Update the output dict with the result of this call
            output_dict[api['label']] = output
        except Exception as e:
            # If there is a bad function call, skip it and try the next one.
            output_dict[api['label']] = None

    return output_dict


def validate_sql_output(database_file: str, query: str):
    """
    Run the sql query, simplify the result, check that it's jsonify-able
    """
    # First execute the sql query against the cached database
    # Note that this will fail if executed against the original database
    # The cached database has had all of the column names made safe, 
    # so we need to also do the same with the query. 
    safe_query = make_query_safe(query)
    conn = database_get_connection(database_file)
    cursor = conn.cursor()
    cursor.execute(safe_query)
    query_results = cursor.fetchall()
    database_close_connection(conn)

    arr = np.array(query_results)
    query_results = arr.squeeze().transpose().tolist()

    # Check that json.dump won't fail later
    try:
        json.dumps(query_results)
    except:
        query_results = safe_cast(query_results)

    return query_results

def validate_api_output(required_api_calls: list, api_pool: dict[str, Callable]):
    """
    Run the api sequence, simplify the result, check that it's jsonify-able
    """

    # First check the api calls
    for api in required_api_calls:
        try:
            json.dumps(api['arguments'])
        except:
            for k, v in api['arguments'].items():
                api['arguments'][k] = safe_cast(v)

    # Execute the required api calls and simplify    
    api_result_dict = execute_api_stack(required_api_calls, api_pool)

    # All outputs that aren't in inputs to another call will be stitched together
    # into the final output in the order in which they appear
    api_input_args = []
    for call in required_api_calls:
        args = list(call['arguments'].values())
        args = [a.lstrip('$').rstrip('$') for a in args if isinstance(a, str)]
        api_input_args.extend(args)
    api_input_args = [parse_argval(arg) for arg in api_input_args] # Each row is now a 2-tuple of key-subkey
    parsed_args = {}
    for key, subkey in api_input_args:
        parsed_args[key] = subkey
    output_results_list = []
    for result_key in api_result_dict.keys():
        if result_key not in parsed_args.keys():
            # This is an output key
            result_key, subkey = parse_argval(result_key)
            result = api_result_dict[result_key]
            if subkey is not None:
                result = result[subkey]
            output_results = simplify_and_check_serialization(result)
            output_results_list.append(output_results)
    
    if len(output_results_list) == 1:
        output_results_list = output_results_list[0]
    return output_results_list

def simplify_and_check_serialization(api_results):
    # Unwrap Pydantic RootModel instances first
    if isinstance(api_results, RootModel):
        api_results = api_results.root

    if isinstance(api_results, list) and len(api_results) == 1:
        api_results = api_results[0]
    elif isinstance(api_results, DataFrame) or isinstance(api_results, Series):
        api_results = api_results.squeeze()
        if len(api_results.shape) == 1:
            api_results = tuple(api_results.tolist())
    elif isinstance(api_results, dict):
        api_results = list(api_results.values())
        squeezed_list = []
        for a in api_results:
            if isinstance(a, list) and len(a) == 1:
                squeezed_list.append(a[0])
            else:
                squeezed_list.append(a)
        if len(squeezed_list) > 1:
            api_results = squeezed_list
        elif len(squeezed_list) == 1:
            api_results = squeezed_list[0]
        else:
            api_results = squeezed_list

    if isinstance(api_results, tuple):
        api_results = list(api_results)

    # Check that json.dump won't fail later
    try:
        json.dumps(api_results)
    except:
        api_results = safe_cast(api_results)
    return api_results


def check_equality_without_order(results_version_1, results_version_2) -> bool:

    correct_answer = False
    if isinstance(results_version_1, list) and isinstance(results_version_2, list):
        arr1 = np.array(results_version_1, dtype=object)
        arr2 = np.array(results_version_2, dtype=object)
        if arr1.shape == arr2.shape:  # Only the same if they are the same shape
            for i in range(len(arr1)):
                if isinstance(arr1[i], float) and np.isnan(arr1[i]):
                    arr1[i] = None
                if isinstance(arr2[i], float) and np.isnan(arr2[i]):
                    arr2[i] = None
            if len(arr1.shape) > 1:  # Compare 2D arrays (multiple select statements)
                # correct_answer = np.array_equal(arr1, arr2)
                rows1 = sorted(map(tuple, arr1.transpose()))
                rows2 = sorted(map(tuple, arr2.transpose()))
                correct_answer = rows1 == rows2
            else:  # Compare lists without order
                # TODO: do this more efficiently
                query_no_order = Counter([str(r) for r in arr1])
                api_no_order = Counter([str(r) for r in arr2])
                if query_no_order == api_no_order:
                    correct_answer = True
    else:  # Compare scalar values
        correct_answer = bool(results_version_1 == results_version_2)
    return correct_answer

def validate_output(database_file: str, query: str, required_api_calls: list, api_pool: dict[str, Callable]):
    query_results = validate_sql_output(database_file=database_file, query=query)
    api_results = validate_api_output(required_api_calls, api_pool)
    correct_answer = check_equality_without_order(results_version_1=query_results, results_version_2=api_results)
    return correct_answer, query_results, api_results

