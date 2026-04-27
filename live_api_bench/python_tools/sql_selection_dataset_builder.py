import logging
from typing import Callable

import sqlglot

from .database_loader import DatabaseLoader
from .sql_utils import get_join_sequences, get_tables_and_aliases
from environment.m3.python_tools.tools.slot_filling_tools import (
    transform_data_to_substring,
    transform_data_to_absolute_value,
    transform_data_to_datetime_part,
    truncate,
    select_unique_values,
    group_data_by,
    concatenate_data,
    Calculator,
)
from .sql_query_components import make_query_safe

from .sql_dataset_builder import (
    SqlDatasetBuilder,
    identify_aggregation_expression,
    identify_expression_structure,
    create_structured_api_call,
    EXPR_TYPE_COLUMN,
    EXPR_TYPE_LITERAL,
    EXPR_TYPE_AGGREGATION,
    EXPR_TYPE_ARITHMETIC
)
from environment.m3.python_tools.tools.tool_registry import (
    AGGREGATE_DATA,
    CALCULATOR,
    CONCATENATE_DATA,
    FILTER_DATA,
    GROUP_DATA_BY,
    INITIALIZE_ACTIVE_DATA,
    RETRIEVE_DATA,
    SELECT_UNIQUE_VALUES,
    SORT_DATA,
    TRANSFORM_DATA,
    TRANSFORM_DATA_TO_SUBSTRING,
    TRANSFORM_DATA_TO_ABSOLUTE_VALUE,
    TRANSFORM_DATA_TO_DATETIME_PART,
    TRUNCATE,
    SelectionTools
)


class SqlSelectionDatasetBuilder(SqlDatasetBuilder):
    _TRANSFORM_OPERATORS: dict[str, Callable] = {
        "substring": transform_data_to_substring,
        "abs": transform_data_to_absolute_value,
        "datetime": transform_data_to_datetime_part,
    }
    _FILTER_TOOL_NAMES: dict[str, str] = {
        "greater_than_equal_to": "select_data_greater_than_equal_to",
        "less_than_equal_to": "select_data_less_than_equal_to",
        "not_equal_to": "select_data_not_equal_to",
        "greater_than": "select_data_greater_than",
        "less_than": "select_data_less_than",
        "like": "select_data_like",
        "in": "select_data_equal_to",
        "equal_to": "select_data_equal_to",
    }

    def __init__(self, loader: DatabaseLoader) -> None:
        super().__init__(loader)
        self.logger = logging.getLogger(__name__)

        # Use the same cache location for the temp database and the IO wrappers
        self.toolbox = SelectionTools(io_cache=self.loader.database_cache_location, use_io_wrappers=False, use_pydantic_signatures=True)


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
            self.toolbox.tools[INITIALIZE_ACTIVE_DATA], INITIALIZE_ACTIVE_DATA, {
                "condition_sequence": join_sequence, "alias_to_table_dict": alias_to_table_dict, 
                "database_path": self.loader.cache_file}, STARTING_TABLE_VAR))
        
        # Add the getters
        available_api_dict = self.toolbox.get_toolbox_with_schema(key_names_and_descriptions)
        # Cache for ORDER BY expression generation
        self._cached_available_api_dict = available_api_dict
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
        select_calls = self._process_select_and_aggregate(parsed_select, required_api_calls[-1]['label'], available_api_dict)
        required_api_calls.extend(select_calls)

        return required_api_calls, key_names_and_descriptions
    
    def _process_select_and_aggregate(self, parsed_select: dict, input_df_key: str, available_api_dict: dict[str, callable]) -> dict:

        api_calls = []

        limit = parsed_select['limit']
        distinct = parsed_select['distinct']

        for idx, clause in enumerate(parsed_select['clauses']):
            col_name = clause[0]
            agg_expr = clause[1]
            if agg_expr:
                # Handle new expression structure format (dict) vs old format (string)
                if isinstance(agg_expr, dict) and agg_expr.get("type") == EXPR_TYPE_AGGREGATION:
                    # Extract aggregation operation from expression structure
                    agg_operation = agg_expr.get("operation")
                    agg_fcn = available_api_dict["compute_data_" + agg_operation]
                    agg_fcn = create_structured_api_call(
                        agg_fcn, agg_fcn.__name__, {"data": f'${input_df_key}$', 'key_name': col_name, 'distinct': distinct}, agg_operation.upper())
                elif isinstance(agg_expr, str):
                    # Legacy string format for backward compatibility
                    agg_fcn = available_api_dict["compute_data_" + agg_expr]
                    agg_fcn = create_structured_api_call(
                        agg_fcn, agg_fcn.__name__, {"data": f'${input_df_key}$', 'key_name': col_name, 'distinct': distinct}, agg_expr.upper())
                else:
                    # Non-aggregation expression - skip for now
                    continue
                api_calls.append(agg_fcn)
            else:
                getter_name = f'get_{col_name}s'
                getter_fcn = available_api_dict[getter_name]
                get_fcn_struct = create_structured_api_call(
                    getter_fcn, getter_fcn.__name__, {"data": f"${input_df_key}$"}, 'SELECT_COL_'+str(idx))
                api_calls.append(get_fcn_struct)

                if distinct:
                    distinct_fcn = create_structured_api_call(
                        self.toolbox.tools[SELECT_UNIQUE_VALUES], SELECT_UNIQUE_VALUES, {"unique_array": f"${api_calls[-1]['label']}$"}, 'DISTINCT_COL_'+str(idx))
                    api_calls.append(distinct_fcn)

                if limit != -1:
                    limit_fcn = create_structured_api_call(truncate, TRUNCATE, {"truncate_array": f'${api_calls[-1]['label']}$', 'n': limit}, 'LIMIT_'+str(idx))
                    api_calls.append(limit_fcn)

        return api_calls

    def _get_calculator_input_for_selection(self, expr_struct: dict, generated_calls: list) -> any:
        """Determine Calculator input - either a variable reference or literal value.

        Selection builder uses getters which return lists directly (not dicts like retrieve_data),
        so we don't need subkey extraction like Slot Filling builder.

        Args:
            expr_struct: Expression structure dict
            generated_calls: List of API calls generated for this expression

        Returns:
            Either a variable reference string like '$VAR$' or a literal value
        """
        if expr_struct.get("type") == EXPR_TYPE_LITERAL:
            return expr_struct.get("value")
        else:
            if generated_calls:
                label = generated_calls[-1]["label"]
                # Getters return lists directly - no subkey needed
                return f'${label}$'
            else:
                raise Exception(f"No calls generated for non-literal expression: {expr_struct}")

    def _generate_orderby_expression_calls(
        self,
        expr_struct: dict,
        input_var: str,
        available_api_dict: dict,
        output_prefix: str,
        call_counter: list
    ) -> list:
        """Generate tool calls for ORDER BY expressions using Selection tools.

        Uses dynamic getters instead of retrieve_data, Calculator for arithmetic.

        Args:
            expr_struct: Expression structure from identify_expression_structure
            input_var: Input variable name for getter calls
            available_api_dict: Dict of available tools (includes getters)
            output_prefix: Prefix for output variable names (e.g., 'ORDERBY')
            call_counter: List with single integer for unique variable names

        Returns:
            List of API call dicts
        """
        if expr_struct is None:
            return []

        expr_type = expr_struct.get("type")
        api_calls = []

        if expr_type == EXPR_TYPE_COLUMN:
            # Generate getter call for column
            col_name = expr_struct.get("name")
            getter_name = f'get_{col_name}s'

            if getter_name not in available_api_dict:
                available_getters = [k for k in available_api_dict.keys() if k.startswith('get_')]
                raise Exception(
                    f"Getter '{getter_name}' not found for column '{col_name}'. "
                    f"Available getters: {available_getters}"
                )

            getter_fcn = available_api_dict[getter_name]
            call_counter[0] += 1
            output_var = f'{output_prefix}_{call_counter[0]}'

            api_calls.append(
                create_structured_api_call(
                    getter_fcn,
                    getter_fcn.__name__,
                    {"data": f'${input_var}$'},
                    output_var
                )
            )

        elif expr_type == EXPR_TYPE_LITERAL:
            # Literals don't need API calls
            pass

        elif expr_type == EXPR_TYPE_AGGREGATION:
            # Generate calls for operand, then aggregate
            operation = expr_struct.get("operation")
            operand = expr_struct.get("operand")

            # Recurse on operand
            operand_calls = self._generate_orderby_expression_calls(
                operand, input_var, available_api_dict, output_prefix, call_counter
            )
            api_calls.extend(operand_calls)

            # Generate aggregate call
            agg_fcn_name = f"compute_data_{operation}"
            if agg_fcn_name not in available_api_dict:
                raise Exception(f"Aggregation function '{agg_fcn_name}' not found")

            agg_fcn = available_api_dict[agg_fcn_name]

            if operand.get("type") == EXPR_TYPE_COLUMN:
                col_name = operand.get("name")
                agg_args = {
                    "data": f'${input_var}$',
                    "key_name": col_name,
                    "distinct": False
                }
            else:
                # Aggregate on complex operand result
                agg_args = {
                    "data": f'${operand_calls[-1]["label"]}$' if operand_calls else f'${input_var}$',
                    "key_name": "",
                    "distinct": False
                }

            output_var = operation.upper()
            api_calls.append(
                create_structured_api_call(agg_fcn, agg_fcn.__name__, agg_args, output_var)
            )

        elif expr_type == EXPR_TYPE_ARITHMETIC:
            # Arithmetic expression - the key case we're adding
            operation = expr_struct.get("operation")
            left = expr_struct.get("left")
            right = expr_struct.get("right")

            # Recurse on left operand
            left_calls = self._generate_orderby_expression_calls(
                left, input_var, available_api_dict, output_prefix, call_counter
            )
            api_calls.extend(left_calls)

            # Recurse on right operand
            right_calls = self._generate_orderby_expression_calls(
                right, input_var, available_api_dict, output_prefix, call_counter
            )
            api_calls.extend(right_calls)

            # Generate Calculator call
            input_1 = self._get_calculator_input_for_selection(left, left_calls)
            input_2 = self._get_calculator_input_for_selection(right, right_calls)

            calc_args = {
                "input_1": input_1,
                "input_2": input_2,
                "operation": operation
            }
            call_counter[0] += 1
            output_var = f'CALC_{call_counter[0]}'

            api_calls.append(
                create_structured_api_call(
                    self.toolbox.tools[CALCULATOR],
                    CALCULATOR,
                    calc_args,
                    output_var
                )
            )

        return api_calls

    # def _process_groupby(self, ast: sqlglot.exp.Expression, table_var: str) -> dict:
    #     if 'group' not in ast.args:
    #         return []

    #     expression = ast.args['group'].expressions[0]
    #     get_groupby_column = self.process_column_object(expression)
    #     groupby_args = {"data": f"${table_var}$", "key_name": get_groupby_column}
    #     groupby_fcn = create_structured_api_call(group_data_by, group_data_by.__name__, groupby_args, 'GROUPED')
    #     return [groupby_fcn]
    
    def process_where_clauses(self, ast: sqlglot.exp.Expression, input_df_key: str) -> list[Callable]:
        if 'where' not in ast.args:
            return []
        
        all_where_apis = []
        where = ast.args['where']
        parsed_where_list = self.parse_where_clause(where)
        i=0
        for parsed_where in parsed_where_list:
            where_apis = []
            table_name = f'${input_df_key}$'
            for clause in parsed_where:
                comparison_column = clause[0]
                value = clause[1]
                comparison_operator = clause[2]
                is_transform = clause[3]
                if is_transform:
                    transform_args = {"data": table_name,
                                        "key_name": comparison_column}
                    if comparison_operator not in self._TRANSFORM_OPERATORS:
                        raise Exception(f"Transform operator {comparison_operator} not supported for selection dataset. ")
                    transform_operator = self._TRANSFORM_OPERATORS[comparison_operator]
                    if comparison_operator == "substring":
                        transform_args['start_index'] = value[0]
                        transform_args['end_index'] = value[1]
                    elif comparison_operator == "datetime":
                        transform_args["datetime_pattern"] = value
                    output_df = f'TRANSFORMED_DF_{str(i)}'
                    api = create_structured_api_call(
                        transform_operator, 
                        transform_operator.__name__, transform_args, output_df
                        )
                else:
                    filter_args = {"data": table_name, "key_name": comparison_column, "value": value}
                    output_df = 'FILTERED_DF_'+str(i)
                    # Extract the appropriate comparison operator from the sql expression
                    if comparison_operator == "between":
                        raise Exception("Haven't implemented BETWEEN filter")
                    filter_op = self.toolbox.tools[self._FILTER_TOOL_NAMES[comparison_operator]]

                    api = create_structured_api_call(filter_op, filter_op.__name__, filter_args, output_df)
                i+=1
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
        """Process ORDER BY clause, including arithmetic expressions.

        Supports:
        - Simple columns: ORDER BY col
        - Aggregations: ORDER BY SUM(col)
        - Arithmetic: ORDER BY col1 / col2, ORDER BY CAST(col AS REAL) / col2
        """
        if 'order' not in ast.args:
            return []

        api_calls = []
        call_counter = [0]
        orderby = ast.args['order'].expressions[0]
        assert len(ast.args['order'].expressions) == 1, "Multiple ORDER BY clauses not yet supported"

        is_descending = bool(orderby.args['desc'])

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
                # Aggregation expression
                operation = expr_struct.get("operation")
                operand = expr_struct.get("operand", {})

                if operand.get("type") == EXPR_TYPE_COLUMN:
                    # Simple aggregation: SUM(col), MAX(col), etc.
                    orderby_column = operand.get("name")
                    agg_fcn_name = f"compute_data_{operation}"
                    agg_fcn = self._cached_available_api_dict[agg_fcn_name]

                    agg_args = {
                        "data": f"${input_df_key}$",
                        "key_name": orderby_column,
                        "distinct": False
                    }
                    operation_upper = operation.upper()
                    api_calls.append(
                        create_structured_api_call(agg_fcn, agg_fcn.__name__, agg_args, operation_upper)
                    )
                    table_name = f"${operation_upper}$"
                    orderby_column = orderby_column
                else:
                    # Complex aggregation: SUM(col1 / col2)
                    expr_calls = self._generate_orderby_expression_calls(
                        expr_struct,
                        input_df_key,
                        self._cached_available_api_dict,
                        'ORDERBY',
                        call_counter
                    )
                    if expr_calls:
                        api_calls.extend(expr_calls)
                        table_name = f'${input_df_key}$'
                        orderby_column = expr_calls[-1]["label"]
                    else:
                        # Fallback
                        orderby_column = self.process_column_object(orderby.this)
                        table_name = f'${input_df_key}$'

            elif expr_type == EXPR_TYPE_ARITHMETIC:
                # ARITHMETIC EXPRESSION - the main fix
                expr_calls = self._generate_orderby_expression_calls(
                    expr_struct,
                    input_df_key,
                    self._cached_available_api_dict,
                    'ORDERBY',
                    call_counter
                )
                if expr_calls:
                    api_calls.extend(expr_calls)
                    table_name = f'${input_df_key}$'
                    ranking_var = f'${expr_calls[-1]["label"]}$'  # Calculator output

                    # Use specialized sort function with ranking_array
                    if is_descending:
                        sort_operator = self.toolbox.tools["sort_data_descending"]
                    else:
                        sort_operator = self.toolbox.tools["sort_data_ascending"]

                    orderby_args = {
                        "data": table_name,
                        "key_name": '',  # Not used when ranking_array provided
                        "ranking_array": ranking_var
                    }
                    api = create_structured_api_call(
                        sort_operator, sort_operator.__name__, orderby_args, 'SORTED_DF'
                    )
                    api_calls.append(api)
                    return api_calls  # Early return
                else:
                    # Fallback
                    orderby_column = self.process_column_object(orderby.this)
                    table_name = f'${input_df_key}$'

            else:
                # Unknown type - fallback to simple processing
                orderby_column = self.process_column_object(orderby.this)
                table_name = f'${input_df_key}$'

        # Standard sort (non-arithmetic path)
        if is_descending:
            sort_operator = self.toolbox.tools["sort_data_descending"]
        else:
            sort_operator = self.toolbox.tools["sort_data_ascending"]

        orderby_args = {"data": table_name, 'key_name': orderby_column}
        api = create_structured_api_call(
            sort_operator, sort_operator.__name__, orderby_args, 'SORTED_DF'
        )
        api_calls.append(api)
        return api_calls

