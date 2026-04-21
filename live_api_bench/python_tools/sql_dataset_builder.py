
import logging
from typing import Callable, Union
import sqlglot
import sqlglot.expressions

from .database_loader import DatabaseLoader

# Expression type constants
EXPR_TYPE_COLUMN = "column"
EXPR_TYPE_LITERAL = "literal"
EXPR_TYPE_AGGREGATION = "aggregation"
EXPR_TYPE_ARITHMETIC = "arithmetic"

where_dict = {
    sqlglot.expressions.Is: "equal_to",
    sqlglot.expressions.EQ: "equal_to",
    sqlglot.expressions.Not: "not_equal_to",
    sqlglot.expressions.Like: "like",
    sqlglot.expressions.In: "in",
    sqlglot.expressions.GTE: "greater_than_equal_to",
    sqlglot.expressions.GT: "greater_than", 
    sqlglot.expressions.LTE: "less_than_equal_to", 
    sqlglot.expressions.LT: "less_than",
    sqlglot.expressions.Between: "between"
}
single_where_clause_types = [sqlglot.expressions.Is, sqlglot.expressions.EQ, sqlglot.expressions.Like, sqlglot.expressions.In,
                            sqlglot.expressions.GTE, sqlglot.expressions.GT, sqlglot.expressions.LTE, sqlglot.expressions.LT, 
                            sqlglot.expressions.Between, sqlglot.expressions.Not]
transform_dict = {
    sqlglot.expressions.Substring: "substring",
    sqlglot.expressions.StrToTime: "str2time",
    sqlglot.expressions.StrToDate: "str2date", 
    sqlglot.expressions.Abs: "abs", 
    sqlglot.expressions.Anonymous: "datetime"
}


class SqlDatasetBuilder:
    def __init__(self, loader: DatabaseLoader) -> None:
        self.logger = logging.getLogger(__name__)

        self.loader = loader
        self.loader.load()
        self.available_function_dict = {}
        self.alias_to_table_dict = {}
        self.toolbox = None
    
    def cleanup_temp_files(self, keep_db=False):
        self.loader.cleanup_temp_files(keep_db=keep_db)

    def translate_query_from_sql_tree(self, query: str) -> dict:
        raise NotImplementedError("Subclass must implement 'translate_query_from_sql_tree")

    def _reformat_compound_column_name(self, name: str) -> str:
        # Change "table_alias.column_name" to table_name_column_name
        if '.' in name:
            name_lst = name.split('.')
            name = self.alias_to_table_dict[name_lst[0]]['modified_table_name'] + "_" + name_lst[1]
        return name
    
    def _extract_legacy_column_name(self, expr_struct: Union[dict, None]) -> Union[str, None]:
        """Extract column name for backward compatibility with legacy code.

        For simple columns and aggregations, returns the column name.
        For complex expressions (arithmetic), attempts to extract a meaningful name.
        """
        if expr_struct is None:
            return None

        expr_type = expr_struct.get("type")

        # Simple column
        if expr_type == EXPR_TYPE_COLUMN:
            return expr_struct["name"]

        # Aggregation - extract column name from operand
        elif expr_type == EXPR_TYPE_AGGREGATION:
            operand = expr_struct.get("operand", {})
            if operand.get("type") == EXPR_TYPE_COLUMN:
                return operand["name"]
            # For complex operands, try to extract from first column found
            return self._find_first_column_name(operand)

        # Arithmetic or other complex expression - find first column
        else:
            return self._find_first_column_name(expr_struct)

    def _find_first_column_name(self, expr_struct: Union[dict, None]) -> Union[str, None]:
        """Recursively find the first column name in an expression structure."""
        if expr_struct is None:
            return None

        expr_type = expr_struct.get("type")

        if expr_type == EXPR_TYPE_COLUMN:
            return expr_struct.get("name")
        elif expr_type == EXPR_TYPE_AGGREGATION:
            return self._find_first_column_name(expr_struct.get("operand"))
        elif expr_type == EXPR_TYPE_ARITHMETIC:
            # Try left first, then right
            left_name = self._find_first_column_name(expr_struct.get("left"))
            if left_name:
                return left_name
            return self._find_first_column_name(expr_struct.get("right"))

        return None

    def parse_select_clause(self, ast: sqlglot.Expression) -> dict:

        parsed_select = {
            'clauses': [],
            'limit': -1,
            'distinct': False
        }
        if ast.args.get('limit'):
            parsed_select['limit'] = int(str(ast.args['limit']).split()[1])

        parsed_select['distinct'] = False
        if ast.args.get('distinct', False):
            parsed_select['distinct'] = True

        if isinstance(ast.expressions[0], sqlglot.expressions.Distinct):
            pass # TODO: implement this

        for expression in ast.expressions:
            # Simple column case - no expression structure needed
            if isinstance(expression, sqlglot.expressions.Column):
                col_name = self.process_column_object(expression)
                parsed_select['clauses'].append((col_name, None))
            else:
                # Use new structured expression parsing
                expr_struct = identify_expression_structure(expression, self)

                # Handle DISTINCT in aggregations
                if isinstance(expression.this, sqlglot.expressions.Distinct):
                    parsed_select['distinct'] = True

                # Extract legacy column name for backward compatibility
                col_name = self._extract_legacy_column_name(expr_struct)

                # For backward compatibility with old code that checks for aggregation string
                # Store both the column name and the expression structure
                # Old code: clause[1] checks for agg string
                # New code: can use full expr_struct
                if expr_struct and expr_struct.get("type") == EXPR_TYPE_AGGREGATION:
                    # Store as (col_name, agg_operation, expr_struct) tuple
                    # But for backward compat, we'll store (col_name, expr_struct)
                    # and let old code extract agg from expr_struct if needed
                    parsed_select['clauses'].append((col_name, expr_struct))
                else:
                    # Non-aggregation expression (arithmetic, etc.)
                    parsed_select['clauses'].append((col_name, expr_struct))

        return parsed_select

    def process_column_object(self, column_expression: Union[sqlglot.expressions.Column, sqlglot.expressions.Star], table_name: str = None) -> str:
        if isinstance(column_expression, sqlglot.expressions.Column):
            if table_name is not None:
                column_name = table_name + '_' + str(column_expression.this)
            elif column_expression.table != '':
                column_name = self.alias_to_table_dict[column_expression.table]['modified_table_name'] + '_' + str(column_expression.this)
            else:
                column_name = str(column_expression.this)
            return column_name
        elif isinstance(column_expression, sqlglot.expressions.Star):
            return ""
        else:
            raise Exception(f"Column object of type {type(column_expression)} could not be processed. ")

    def parse_transform(self, transform_clause):
        if isinstance(transform_clause, sqlglot.expressions.Substring):
            col_name = self.process_column_object(transform_clause.this)
            start = int(transform_clause.args['start'].this) - 1  # SQL values will be 1-indexed, convert to 0-index
            end = start + int(transform_clause.args['length'].this)
            value = (start, end)
            parsed_transform = (col_name, value, transform_dict[type(transform_clause)], True)
        elif isinstance(transform_clause, sqlglot.expressions.Anonymous):
            try:
                assert transform_clause.this.lower() == "strftime"
            except:
                raise Exception(f"Can't parse transform clause: {str(transform_clause)}")
            col_name = self.process_column_object(transform_clause.expressions[1])
            datetime_pattern = transform_clause.expressions[0].this
            parsed_transform = (col_name, datetime_pattern, transform_dict[type(transform_clause)], True)
        else:
            raise Exception(f"Can't parse transform of type {type(transform_clause)}")
        return parsed_transform

    def parse_single_where_clause(self, clause):
        parsed_clauses = []

        if isinstance(clause, sqlglot.expressions.Not):
            value = clause.expression
            if isinstance(value, sqlglot.expressions.Null):
                value = None
            clause_type = where_dict[type(clause)]
            clause = clause.this
        elif isinstance(clause, sqlglot.expressions.Between):
            # value isn't used for between because there are two values
            value = None  # clause.expression == None
            clause_type = where_dict[type(clause)]
        else:
            value = clause.expression.this
            clause_type = where_dict[type(clause)]
            
        if type(clause.this) in transform_dict.keys():
            transform_clause = self.parse_transform(clause.this)
            parsed_clauses.append(transform_clause)
            if isinstance(clause.this.this, sqlglot.expressions.Column):
                col_name = self.process_column_object(clause.this.this)
            else:
                col_name = transform_clause[0]
        elif isinstance(clause.this, sqlglot.expressions.Column):
            col_name = self.process_column_object(clause.this)
        else:
            raise Exception(f"Can't parse where clause nested with {type(clause.this)}")
        
        if clause_type == "between":
            # Between is implemented as two separate filters
            parsed_clauses.append((col_name, clause.args['low'].this, "greater_than", False))
            parsed_clauses.append((col_name, clause.args['high'].this, "less_than", False))
        else:
            if isinstance(value, sqlglot.expressions.Identifier):
                value = value.this
            # if value is not None and clause.expression.is_number:
            #     value = float(value)
            parsed_clauses.append((col_name, value, clause_type, False))
        return parsed_clauses

    @staticmethod
    def prune_where_tree(expr: sqlglot.Expression) -> bool:
        if isinstance(expr.parent, sqlglot.expressions.Paren):
            return True
        if type(expr) in single_where_clause_types:
            return True
        return False
        

    def parse_where_clause(self, ast: sqlglot.Expression) -> dict:

        # Clauses will be a list of lists, where the top-level lists are connected by ORs, and the inner level lists by ANDs
        parsed_where = [[]]

        for node in ast.walk(bfs=False, prune=self.prune_where_tree):
            if isinstance(node, sqlglot.expressions.Or):
                left_side = self.parse_where_clause(node.this)
                right_side = self.parse_where_clause(node.expression)
                parsed_where.extend(left_side)
                parsed_where.extend(right_side)
                break  # Can only handle a single OR for now
            elif isinstance(node, sqlglot.expressions.Paren):
                return(self.parse_where_clause(node.this.copy()))
            # Handle single condition
            elif type(node) in single_where_clause_types:
                parsed_clause = self.parse_single_where_clause(node)
                parsed_where[-1].extend(parsed_clause)
            elif type(node) in [sqlglot.expressions.And, sqlglot.expressions.Where]:
                continue
            else:
                raise Exception(f"Unknown where operator: {type(node)}")
        if parsed_where[0] == []:
            parsed_where = parsed_where[1:]
        return parsed_where


def identify_aggregation_expression(expression: sqlglot.Expression) -> Union[str, None]:
    """Legacy function for backward compatibility. Use identify_expression_structure instead."""
    agg = None
    if isinstance(expression, sqlglot.expressions.Count):
        agg = "count"
    elif isinstance(expression, sqlglot.expressions.Sum):
        agg = "sum"
    elif isinstance(expression, sqlglot.expressions.Min):
        agg = "min"
    elif isinstance(expression, sqlglot.expressions.ArgMin):
        agg = "argmin"
    elif isinstance(expression, sqlglot.expressions.Max):
        agg = "max"
    elif isinstance(expression, sqlglot.expressions.ArgMax):
        agg = "argmax"
    elif isinstance(expression, sqlglot.expressions.Avg):
        agg = "mean"
    elif isinstance(expression, sqlglot.expressions.Stddev):
        agg = "std"
    return agg


def identify_expression_structure(expression: sqlglot.Expression, sql_builder_instance) -> Union[dict, None]:
    """Parse SQL expression into structured dict representation.

    Returns dict with type and operands for aggregations, arithmetic, columns, and literals.
    Supports nested expressions like SUM(col1 / col2).

    Args:
        expression: SQLglot expression node to parse
        sql_builder_instance: SqlDatasetBuilder instance for column processing

    Returns:
        Dict representing expression structure, or None if expression cannot be parsed

    Structure examples:
        - Column: {"type": "column", "name": "table_column"}
        - Literal: {"type": "literal", "value": 2}
        - Aggregation: {"type": "aggregation", "operation": "sum", "operand": {...}}
        - Arithmetic: {"type": "arithmetic", "operation": "divide", "left": {...}, "right": {...}}
    """
    # Handle aggregation expressions
    if isinstance(expression, sqlglot.expressions.Count):
        # Handle COUNT(*) special case
        if isinstance(expression.this, sqlglot.expressions.Star):
            operand = {"type": EXPR_TYPE_COLUMN, "name": ""}
        elif isinstance(expression.this, sqlglot.expressions.Distinct):
            # COUNT(DISTINCT col) - unwrap distinct and process column
            operand = identify_expression_structure(expression.this.expressions[0], sql_builder_instance)
        else:
            operand = identify_expression_structure(expression.this, sql_builder_instance)
        return {"type": EXPR_TYPE_AGGREGATION, "operation": "count", "operand": operand}

    elif isinstance(expression, sqlglot.expressions.Sum):
        operand = identify_expression_structure(expression.this, sql_builder_instance)
        return {"type": EXPR_TYPE_AGGREGATION, "operation": "sum", "operand": operand}

    elif isinstance(expression, sqlglot.expressions.Min):
        operand = identify_expression_structure(expression.this, sql_builder_instance)
        return {"type": EXPR_TYPE_AGGREGATION, "operation": "min", "operand": operand}

    elif isinstance(expression, sqlglot.expressions.Max):
        operand = identify_expression_structure(expression.this, sql_builder_instance)
        return {"type": EXPR_TYPE_AGGREGATION, "operation": "max", "operand": operand}

    elif isinstance(expression, sqlglot.expressions.ArgMin):
        operand = identify_expression_structure(expression.this, sql_builder_instance)
        return {"type": EXPR_TYPE_AGGREGATION, "operation": "argmin", "operand": operand}

    elif isinstance(expression, sqlglot.expressions.ArgMax):
        operand = identify_expression_structure(expression.this, sql_builder_instance)
        return {"type": EXPR_TYPE_AGGREGATION, "operation": "argmax", "operand": operand}

    elif isinstance(expression, sqlglot.expressions.Avg):
        operand = identify_expression_structure(expression.this, sql_builder_instance)
        return {"type": EXPR_TYPE_AGGREGATION, "operation": "mean", "operand": operand}

    elif isinstance(expression, sqlglot.expressions.Stddev):
        operand = identify_expression_structure(expression.this, sql_builder_instance)
        return {"type": EXPR_TYPE_AGGREGATION, "operation": "std", "operand": operand}

    # Handle arithmetic expressions
    elif isinstance(expression, sqlglot.expressions.Div):
        left = identify_expression_structure(expression.this, sql_builder_instance)
        right = identify_expression_structure(expression.expression, sql_builder_instance)
        return {"type": EXPR_TYPE_ARITHMETIC, "operation": "divide", "left": left, "right": right}

    elif isinstance(expression, sqlglot.expressions.Mul):
        left = identify_expression_structure(expression.this, sql_builder_instance)
        right = identify_expression_structure(expression.expression, sql_builder_instance)
        return {"type": EXPR_TYPE_ARITHMETIC, "operation": "multiply", "left": left, "right": right}

    elif isinstance(expression, sqlglot.expressions.Add):
        left = identify_expression_structure(expression.this, sql_builder_instance)
        right = identify_expression_structure(expression.expression, sql_builder_instance)
        return {"type": EXPR_TYPE_ARITHMETIC, "operation": "add", "left": left, "right": right}

    elif isinstance(expression, sqlglot.expressions.Sub):
        left = identify_expression_structure(expression.this, sql_builder_instance)
        right = identify_expression_structure(expression.expression, sql_builder_instance)
        return {"type": EXPR_TYPE_ARITHMETIC, "operation": "subtract", "left": left, "right": right}

    # Handle column references
    elif isinstance(expression, sqlglot.expressions.Column):
        col_name = sql_builder_instance.process_column_object(expression)
        return {"type": EXPR_TYPE_COLUMN, "name": col_name}

    # Handle literal values
    elif isinstance(expression, sqlglot.expressions.Literal):
        value = expression.this
        # Convert to appropriate type
        if expression.is_number:
            try:
                value = int(value)
            except ValueError:
                value = float(value)
        return {"type": EXPR_TYPE_LITERAL, "value": value}

    # Handle Star (for COUNT(*))
    elif isinstance(expression, sqlglot.expressions.Star):
        return {"type": EXPR_TYPE_COLUMN, "name": ""}

    # Handle Distinct - unwrap and recursively process
    elif isinstance(expression, sqlglot.expressions.Distinct):
        return identify_expression_structure(expression.expressions[0], sql_builder_instance)

    # Handle Cast - unwrap and recursively process the inner expression
    elif isinstance(expression, sqlglot.expressions.Cast):
        return identify_expression_structure(expression.this, sql_builder_instance)

    # Handle Paren - unwrap and recursively process
    elif isinstance(expression, sqlglot.expressions.Paren):
        return identify_expression_structure(expression.this, sql_builder_instance)

    # Unsupported expression type
    return None

def identify_transformation_expression(expression: sqlglot.Expression) -> Union[str, None]:
    transform = None
    transform_args = {}
    if isinstance(expression, sqlglot.expressions.Abs):
        transform = "abs"
        transform_args = {}
    elif isinstance(expression, sqlglot.expressions.Substring):
        transform = "substring"
        transform_args = {}  # TODO: implement this
    return transform, transform_args


def create_structured_api_call(fcn: Callable, name: str, args: dict, output: str) -> dict:
    payload = {'fcn': fcn, 'name': name, 'arguments': args, 'label': output}
    return payload
