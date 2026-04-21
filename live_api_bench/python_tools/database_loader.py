
from collections import defaultdict
import os
import pandas as pd

from .sql_query_components import (
    database_get_connection,
    database_close_connection,
    database_lookup_tables,
    database_get_table,
    safe_name_columns
    )
from environment.m3.python_tools.tools.dtype_utils import preserve_dtypes_in_dict

class DatabaseLoader:

    def __init__(self, database_name: str, database_location: str, database_cache_location: str = "."):

        # Find source database
        self.name = database_name
        self.database_location = database_location
        self.database_source_file = os.path.join(self.database_location, self.name, self.name+".sqlite")
        assert os.path.isdir(self.database_location), f"{self.database_location} is not a directory. "
        assert os.path.isfile(self.database_source_file), f"{self.database_source_file} is not a sqlite database. "

        # Set cache
        self.database_cache_location = database_cache_location
        self.cache_file = os.path.join(self.database_cache_location, database_name + ".sqlite")

        # Set up containers for database schema components
        self.column_list = []
        self.table_descriptions = {}
        self.table_data = {}
        self.primary_keys = defaultdict(list)
        self.foreign_keys = []

    def cleanup_temp_files(self, keep_db=False):
        for directory, subdirs, files in os.walk(self.database_cache_location):
            for f in files:
                if ((not keep_db) and f == self.name+".sqlite") or (f.startswith("temp_") and f.endswith(".csv")):
                    found_temp_file = os.path.join(self.database_cache_location, f)
                    os.remove(found_temp_file)
    
    def get_table_column_descriptions(self):
        table_column_descriptions = {}
        for table, cols in self.table_descriptions.items():
            table_column_descriptions[table] = {}
            for row in cols:
                table_column_descriptions[table][row['column_name']] = {'column_description': row['column_description'], 'column_dtype': row['column_dtype']}
        return table_column_descriptions

    def _load_keys(self, key_data: dict):
        raise NotImplementedError()


    def load(self, load_in_memory: bool = False):

        self.load_lazy()
        if load_in_memory:
            # Convert tables in database into pandas dataframes
            for table_name in self.table_descriptions.keys():
                table_data = safe_name_columns(self.get_table_as_dataframe(table_name))
                self.table_data[table_name] = table_data
        
        elif self.database_cache_location is not None:
            if not os.path.isdir(self.database_cache_location):
                os.makedirs(self.database_cache_location, exist_ok=True)
                print(f"Creating database_cache_location = {self.database_cache_location} directory. ")
            
            connection = database_get_connection(self.cache_file)
            for table_name in self.table_descriptions.keys():
                loaded_table = self.get_table_as_dataframe(table_name, use_original_not_cache=True)
                safe_table = safe_name_columns(loaded_table)
                safe_table.to_sql(table_name, connection, if_exists='replace', index=False)
            database_tables = database_lookup_tables(connection)
            assert set(database_tables) == set(self.table_descriptions.keys()), f"Database tables: {set(database_tables)} not equal to self.table_descriptions: {set(self.table_descriptions.keys())}"
            database_close_connection(connection)


    def load_lazy(self):
        raise NotImplementedError()


    def get_table_as_dataframe(self, table_name: str, use_original_not_cache: bool = False, safe: bool = True) -> pd.DataFrame:
        if use_original_not_cache:
            assert self.database_path is not None, "Can't call get_table without calling lazy_loading first"
            path_to_database = self.database_path
        else:
            path_to_database = self.cache_file

        connection = database_get_connection(path_to_database)
        table = database_get_table(connection, table_name)

        # Close the database
        database_close_connection(connection)

        if safe:
            table = safe_name_columns(table)
        return table
    
    def get_table_as_dict(self, table_name: str, use_original_not_cache: bool = False, safe: bool = True) -> dict:
        table = self.get_table_as_dataframe(table_name, use_original_not_cache=use_original_not_cache, safe=safe)
        return preserve_dtypes_in_dict(table)

