
import logging
from typing import Callable

import sqlglot

from .database_loader import DatabaseLoader
from .sql_utils import get_join_sequences, get_tables_and_aliases
from environment.m3.python_tools.tools.slot_filling_tools import (
    aggregate_data,
    filter_data,
    sort_data,
    retrieve_data,
    transform_data,
    select_unique_values,
    group_data_by,
    concatenate_data,
    Calculator
)
from environment.m3.python_tools.tools.sql_tools import initialize_active_data
from .sql_query_components import make_query_safe

from .sql_dataset_builder import (
    SqlDatasetBuilder,
    identify_transformation_expression,
    identify_expression_structure,
    create_structured_api_call,
    EXPR_TYPE_COLUMN,
    EXPR_TYPE_LITERAL,
    EXPR_TYPE_AGGREGATION,
    EXPR_TYPE_ARITHMETIC
)
from environment.m3.python_tools.tools.tool_registry import INITIALIZE_ACTIVE_DATA, SlotFillingTools

class SqlSlotFillingDatasetBuilder(SqlDatasetBuilder):
    def __init__(self, loader: DatabaseLoader) -> None:
        super().__init__(loader)
        self.logger = logging.getLogger(__name__)

        # Use the same cache location for the temp database and the IO wrappers
        self.toolbox = SlotFillingTools(io_cache=self.loader.database_cache_location, use_io_wrappers=False, use_pydantic_signatures=True)
        


    def translate_query_from_sql_tree(self, query: str) -> tuple[list,list]:

        if query.count("SELECT") > 1:
            raise Exception("Can't support multiple SELECT statements in single query. ")
        
        # Make the query safe
        query = make_query_safe(query)

        # Parse the query into an AST
        glot_ast = sqlglot.parse_one(query)

        # Collect JOIN parameters
        alias_to_table_dict = get_tables_and_aliases(glot_ast)
        self.alias_to_table_dict = alias_to_table_dict  # Need to save this in self for other parsing methods to access (where clause)
        join_sequence = get_join_sequences(glot_ast)
        key_names_and_descriptions = self.loader.get_query_specific_columns_and_descriptions(alias_to_table_dict, join_sequence)

        STARTING_TABLE_VAR = "starting_table_var"

        # Load the starting table
        required_api_calls = []
        required_api_calls.append(create_structured_api_call(
            initialize_active_data, INITIALIZE_ACTIVE_DATA, {
                "condition_sequence": join_sequence, "alias_to_table_dict": alias_to_table_dict, "database_path": self.loader.cache_file},
              STARTING_TABLE_VAR))

        # Handle 'where'
        where_calls = self.process_where_clauses(glot_ast, required_api_calls[-1]['label'])
        required_api_calls.extend(where_calls)

        # # Handle 'group by'
        # groupby_calls = self._process_groupby(glot_ast, required_api_calls[-1]['label'])
        # required_api_calls.extend(groupby_calls)

        # Handle 'order'
        order_calls = self._process_orderby_clause(glot_ast, required_api_calls[-1]['label'])
        required_api_calls.extend(order_calls)

        # Create a get for the select
        parsed_select = self.parse_select_clause(glot_ast)
        get_agg_fcn = self._process_select_and_aggregate(parsed_select, required_api_calls[-1]['label'])
        required_api_calls.extend(get_agg_fcn)

        return required_api_calls, key_names_and_descriptions


    # def _process_groupby(self, ast: sqlglot.exp.Expression, table_var: str) -> dict:
    #     if 'group' not in ast.args:
    #         return []

    #     expression = ast.args['group'].expressions[0]
    #     get_groupby_column = self.process_column_object(expression)
    #     groupby_args = {"data_source": f'${table_var}.{FILEPATH_KEY}$', "key_name": get_groupby_column}
    #     groupby_fcn = create_structured_api_call(group_data_by, group_data_by.__name__, groupby_args, 'GROUPED')
    #     return [groupby_fcn]

    def _get_calculator_input(self, expr_struct: dict, generated_calls: list) -> any:
        """Determine Calculator input - either a variable reference or literal value.

        For column expressions, extracts the specific column value from retrieve_data result.
        For aggregation/other expressions, returns reference to the full result.
        For literal expressions, returns the literal value directly.

        Args:
            expr_struct: Expression structure dict
            generated_calls: List of API calls generated for this expression

        Returns:
            Either a variable reference string like '$VAR$' or '$VAR.key$' or a literal value
        """
        if expr_struct.get("type") == EXPR_TYPE_LITERAL:
            # Return literal value directly
            return expr_struct.get("value")
        else:
            # Return variable reference to last generated call
            if generated_calls:
                label = generated_calls[-1]["label"]

                # If this was a column retrieve, extract the specific column from the result dict
                # retrieve_data returns {'col_name': [values]}, but Calculator needs just [values]
                if expr_struct.get("type") == EXPR_TYPE_COLUMN:
                    col_name = expr_struct.get("name")
                    return f'${label}.{col_name}$'  # Extract specific column list
                else:
                    return f'${label}$'  # Use full result (for aggregations, etc.)
            else:
                # Should not happen if expression was properly processed
                raise Exception(f"No calls generated for non-literal expression: {expr_struct}")

    def _generate_expression_calls(
        self,
        expr_struct: dict,
        input_var: str,
        distinct: bool,
        limit: int,
        output_prefix: str,
        call_counter: list
    ) -> list:
        """Recursively generate tool calls for structured expression.

        Handles columns, literals, aggregations, and arithmetic operations.
        Returns list of API calls needed to compute the expression.

        Args:
            expr_struct: Expression structure dict from identify_expression_structure
            input_var: Input variable name for retrieve_data calls
            distinct: Whether to apply distinct filter
            limit: Limit for result size (-1 for no limit)
            output_prefix: Prefix for output variable names
            call_counter: List with single integer for generating unique variable names

        Returns:
            List of API call dicts
        """
        if expr_struct is None:
            return []

        expr_type = expr_struct.get("type")
        api_calls = []

        if expr_type == EXPR_TYPE_COLUMN:
            # Generate retrieve_data call for column
            col_name = expr_struct.get("name")
            select_args = {
                "data": f'${input_var}$',
                "key_name": col_name,
                "distinct": distinct,
                "limit": limit
            }
            call_counter[0] += 1
            output_var = f'{output_prefix}_{call_counter[0]}'
            api_calls.append(
                create_structured_api_call(retrieve_data, retrieve_data.__name__, select_args, output_var)
            )

        elif expr_type == EXPR_TYPE_LITERAL:
            # Literals don't need API calls - they're used directly in Calculator
            pass

        elif expr_type == EXPR_TYPE_AGGREGATION:
            # Generate calls for operand, then aggregate
            operation = expr_struct.get("operation")
            operand = expr_struct.get("operand")

            # Generate calls for the operand
            operand_calls = self._generate_expression_calls(
                operand, input_var, distinct, limit, output_prefix, call_counter
            )
            api_calls.extend(operand_calls)

            # Generate aggregate_data call
            # Input is the last operand call (or input_var if operand was empty/literal)
            if operand_calls:
                data_source = f'${operand_calls[-1]["label"]}$'
                # For aggregations, we use the key from operand if it's a column retrieve
                # Otherwise use empty string for operations on full result
                if operand.get("type") == EXPR_TYPE_COLUMN:
                    key_name = operand.get("name", "")
                else:
                    # For complex operands, aggregate operates on the result
                    key_name = ""
            else:
                # Operand was literal or empty (like COUNT(*))
                data_source = f'${input_var}$'
                key_name = operand.get("name", "") if operand else ""

            agg_args = {
                "data": data_source,
                "key_name": key_name,
                "aggregation_operation": operation,
                "distinct": distinct,
                "limit": limit
            }
            output_var = operation.upper()
            api_calls.append(
                create_structured_api_call(aggregate_data, aggregate_data.__name__, agg_args, output_var)
            )

        elif expr_type == EXPR_TYPE_ARITHMETIC:
            # Generate calls for left and right operands, then Calculator
            operation = expr_struct.get("operation")
            left = expr_struct.get("left")
            right = expr_struct.get("right")

            # Generate calls for left operand
            left_calls = self._generate_expression_calls(
                left, input_var, distinct, limit, output_prefix, call_counter
            )
            api_calls.extend(left_calls)

            # Generate calls for right operand
            right_calls = self._generate_expression_calls(
                right, input_var, distinct, limit, output_prefix, call_counter
            )
            api_calls.extend(right_calls)

            # Determine inputs for Calculator
            input_1 = self._get_calculator_input(left, left_calls)
            input_2 = self._get_calculator_input(right, right_calls)

            # Generate Calculator call
            calc_args = {
                "input_1": input_1,
                "input_2": input_2,
                "operation": operation
            }
            call_counter[0] += 1
            output_var = f'CALC_{call_counter[0]}'
            api_calls.append(
                create_structured_api_call(Calculator, Calculator.__name__, calc_args, output_var)
            )

        return api_calls

    def _process_select_and_aggregate(self, parsed_select: dict, table_var: str) -> dict:
        api_calls = []
        call_counter = [0]  # Counter for generating unique variable names

        limit = parsed_select['limit']
        distinct = parsed_select['distinct']

        for idx, clause in enumerate(parsed_select['clauses']):
            col_name = clause[0]
            expr_struct = clause[1]

            # Handle simple column case (no expression structure)
            if expr_struct is None:
                select_args = {
                    "data": f'${table_var}$',
                    "key_name": col_name,
                    "distinct": distinct,
                    "limit": limit
                }
                api_calls.append(
                    create_structured_api_call(retrieve_data, retrieve_data.__name__, select_args, 'SELECT_COL_'+str(idx))
                )
                continue

            # Handle expressions with structure (aggregations, arithmetic, etc.)
            expr_type = expr_struct.get("type")

            # Simple aggregation (backward compatible path)
            if expr_type == EXPR_TYPE_AGGREGATION and expr_struct.get("operand", {}).get("type") == EXPR_TYPE_COLUMN:
                # Legacy path for simple aggregations like SUM(col)
                operation = expr_struct.get("operation")
                select_args = {
                    "data": f'${table_var}$',
                    "key_name": col_name,
                    "aggregation_operation": operation,
                    "distinct": distinct,
                    "limit": limit
                }
                api_calls.append(
                    create_structured_api_call(aggregate_data, aggregate_data.__name__, select_args, operation.upper())
                )
            else:
                # Complex expression - use recursive generation
                expr_calls = self._generate_expression_calls(
                    expr_struct,
                    table_var,
                    distinct,
                    limit,
                    f'SELECT_{idx}',
                    call_counter
                )
                api_calls.extend(expr_calls)

        return api_calls

    def process_where_clauses(self, ast: sqlglot.exp.Expression, input_df_key: str) -> list[Callable]:
        if 'where' not in ast.args:
            return []
        
        where = ast.args['where']
        parsed_where = self.parse_where_clause(where)
        all_where_apis = []
        i = 0
        for path in parsed_where:  # Will only be > 1 if there is an OR.
            where_apis = []
            table_name = f'${input_df_key}$'
            for clause in path:
                comparison_column = clause[0]
                value = clause[1]
                comparison_operator = clause[2]
                is_transform = clause[3]
                if is_transform:
                    if comparison_operator == "substring":
                        operation_args = {'start_index': value[0], 'end_index': value[1]}
                    elif comparison_operator == "abs":
                        operation_args = {}
                    elif comparison_operator == "datetime":
                        operation_args = {"pattern": value}
                    transform_args = {"data": table_name,
                                    "key_name": comparison_column,
                                    "operation_type": comparison_operator,
                                    "operation_args": operation_args}
                    output_df = f'TRANSFORMED_DF_{str(i)}'
                    api = create_structured_api_call(transform_data, transform_data.__name__, transform_args, output_df)
                else:
                    filter_args = {"data": table_name, "key_name": comparison_column, "value": value, "condition": comparison_operator}
                    output_df = 'FILTERED_DF_'+str(i)
                    api = create_structured_api_call(filter_data, filter_data.__name__, filter_args, output_df)
                i += 1
                table_name = f'${output_df}$'
                where_apis.append(api)
            all_where_apis.append(where_apis)

        if len(all_where_apis) > 1:
            assert len(all_where_apis) == 2  # Only support a single OR for now.
            output_table_var_1 = '$' + all_where_apis[0][-1]['label'] + '$'
            output_table_var_2 = '$' + all_where_apis[1][-1]['label'] + '$'
            concat_args = {"data_1": output_table_var_1, "data_2": output_table_var_2}
            api = create_structured_api_call(concatenate_data, concatenate_data.__name__, concat_args, "CONCAT_DATA")
            return all_where_apis[0] + all_where_apis[1] + [api]
        else:
            return all_where_apis[0]


    def _process_orderby_clause(self, ast: sqlglot.exp.Expression, input_df_key: str) -> list[Callable]:
        if 'order' not in ast.args:
            return []

        api_calls = []
        call_counter = [0]
        orderby = ast.args['order'].expressions[0]
        assert len(ast.args['order'].expressions) == 1, "Need to implement multiple 'order by' clauses"
        ascending = not bool(orderby.args['desc'])  # return results in descending order

        # Use new expression structure parsing
        expr_struct = identify_expression_structure(orderby.args['this'], self)

        if expr_struct is None:
            # Simple column case - no expression structure
            orderby_column = self.process_column_object(orderby.this)
            table_name = f'${input_df_key}$'
        else:
            expr_type = expr_struct.get("type")

            if expr_type == EXPR_TYPE_COLUMN:
                # Simple column
                orderby_column = expr_struct.get("name")
                table_name = f'${input_df_key}$'

            elif expr_type == EXPR_TYPE_AGGREGATION:
                # Legacy aggregation handling
                operation = expr_struct.get("operation")
                operand = expr_struct.get("operand", {})

                if operand.get("type") == EXPR_TYPE_COLUMN:
                    # Simple aggregation like SUM(col)
                    orderby_column = operand.get("name")
                    get_agg_args = {
                        "data": f'${input_df_key}$',
                        "key_name": orderby_column,
                        "aggregation_operation": operation,
                        "distinct": False,
                        "limit": -1
                    }
                    operation_upper = operation.upper() if operation else "AGG"
                    get_agg_fcn = create_structured_api_call(
                        aggregate_data, aggregate_data.__name__, get_agg_args, operation_upper
                    )
                    api_calls.append(get_agg_fcn)
                    table_name = f"${operation_upper}$"
                    orderby_column = operand.get("name")
                else:
                    # Complex aggregation like SUM(col1 / col2)
                    expr_calls = self._generate_expression_calls(
                        expr_struct,
                        input_df_key,
                        False,  # distinct
                        -1,     # limit
                        'ORDERBY',
                        call_counter
                    )
                    if expr_calls:
                        api_calls.extend(expr_calls)
                        # Sort by the result of the last expression call
                        table_name = f'${input_df_key}$'
                        orderby_column = expr_calls[-1]["label"]
                    else:
                        # Fallback if no calls generated
                        orderby_column = self.process_column_object(orderby.this)
                        table_name = f'${input_df_key}$'

            elif expr_type == EXPR_TYPE_ARITHMETIC:
                # Arithmetic expression like col1 / col2 or CAST(col AS REAL) / col2
                expr_calls = self._generate_expression_calls(
                    expr_struct,
                    input_df_key,
                    False,  # distinct
                    -1,     # limit
                    'ORDERBY',
                    call_counter
                )
                if expr_calls:
                    api_calls.extend(expr_calls)
                    # The last call (Calculator) output contains ranking values
                    # Pass this as ranking_array to sort_data instead of key_name
                    table_name = f'${input_df_key}$'
                    ranking_var = f'${expr_calls[-1]["label"]}$'  # e.g., "$CALC_1$"

                    # Create sort_data call with ranking_array
                    orderby_args = {
                        "data": table_name,
                        "key_name": '',  # Not used when ranking_array is provided
                        "ascending": ascending,
                        "ranking_array": ranking_var
                    }
                    api = create_structured_api_call(sort_data, sort_data.__name__, orderby_args, 'SORTED_DF')
                    api_calls.append(api)
                    return api_calls  # Early return - don't execute common code below
                else:
                    # Fallback if no calls generated
                    orderby_column = self.process_column_object(orderby.this)
                    table_name = f'${input_df_key}$'

            else:
                # Fallback - try legacy transformation handling
                transform, transform_args = identify_transformation_expression(orderby.args['this'])
                if transform:
                    orderby_column = self.process_column_object(orderby.args['this'].this)
                    get_trans_args = {
                        "data": f'${input_df_key}$',
                        "key_name": orderby_column,
                        "operation_type": transform,
                        "operation_args": transform_args
                    }
                    get_trans_fcn = create_structured_api_call(
                        transform_data, transform_data.__name__, get_trans_args, transform.upper()
                    )
                    api_calls.append(get_trans_fcn)
                    table_name = f"${transform.upper()}$"
                else:
                    orderby_column = self.process_column_object(orderby.this)
                    table_name = f'${input_df_key}$'

        orderby_args = {"data": table_name, 'key_name': orderby_column, 'ascending': ascending}
        api = create_structured_api_call(sort_data, sort_data.__name__, orderby_args, 'SORTED_DF')
        api_calls.append(api)
        return api_calls
