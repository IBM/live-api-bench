
import argparse
import json
import os
from pathlib import Path
from typing import List

from .bird_database_loader import BirdDatabaseLoader
from .sql_selection_dataset_builder import SqlSelectionDatasetBuilder
from .sql_slot_filling_dataset_builder import SqlSlotFillingDatasetBuilder
from .main_fcn import main


ALL_BIRD_TRAIN = [
    'app_store', 'law_episode', 'citeseer', 'retail_world',
    'college_completion', 'coinmarketcap', 'human_resources',
    'beer_factory', 'food_inspection_2', 'authors', 'cars',
    'book_publishing_company', 'codebase_comments', 'synthea',
    'european_football_1', 'movielens', 'world_development_indicators',
    'craftbeer', 'address', 'bike_share_1', 'mental_health_survey',
    'food_inspection', 'mondial_geo', 'legislator', 'cookbook',
    'olympics', 'soccer_2016', 'public_review_platform',
    'movies_4', 'airline', 'video_games', 'university',
    'movie_platform', 'sales', 'genes', 'software_company',
    'hockey', 'menu', 'retail_complains', 'restaurant',
    'car_retails', 'donor', 'talkingdata', 'cs_semester',
    'language_corpus', 'ice_hockey_draft', 'world', 'regional_sales',
    'retails', 'shakespeare', 'superstore', 'sales_in_weather',
    'works_cycles', 'movie_3', 'social_media', 'chicago_crime',
    'disney', 'books', 'image_and_language', 'professional_basketball',
    'simpson_episodes', 'music_platform_2', 'student_loan',
    'computer_student', 'shooting', 'music_tracker', 'trains',
    'shipping', 'movie'
]

ALL_BIRD_DEV = [
    'superhero', 'student_club', 'thrombosis_prediction',
    'debit_card_specializing', 'formula_1', 'california_schools',
    'toxicology', 'financial', 'european_football_2', 'card_games',
    'codebase_community'
]


def get_datasets(mode: str, size: str, dataset_arg: str = None) -> tuple[List[str], str]:
    """
    Determine which datasets to process based on mode, size, and dataset arguments.

    Returns:
        tuple: (list of dataset names, mode string)
    """
    if dataset_arg:
        if dataset_arg in ALL_BIRD_TRAIN:
            return [dataset_arg], "train"
        elif dataset_arg in ALL_BIRD_DEV:
            return [dataset_arg], "dev"
        else:
            raise ValueError(f"Dataset '{dataset_arg}' not found in train or dev sets")

    if mode == 'train' and size == 'large':
        return ALL_BIRD_TRAIN, mode
    elif mode == 'dev' and size == 'large':
        return ALL_BIRD_DEV, mode
    elif mode == 'train' and size == 'small':
        return ["disney"], mode
    elif mode == 'dev' and size == 'small':
        return ['california_schools'], mode

    raise ValueError(f"Invalid mode/size combination: {mode}/{size}")


def load_query_data(dataset: str, mode: str, db_path: str) -> tuple[List[str], List[str]]:
    """
    Load query data for a specific dataset and mode.

    Returns:
        tuple: (list of questions, list of SQL queries)
    """
    if mode == 'train':
        query_dir = 'train_queries'
        query_file = f"train_{dataset}.json"
        backup_file = "train.json"
    elif mode == 'dev':
        query_dir = 'dev_queries'
        query_file = f"dev_{dataset}.json"
        backup_file = "dev.json"
    else:
        raise ValueError(f"Invalid mode: {mode}")

    query_path = os.path.join(db_path, query_dir, query_file)
    backup_query_path = os.path.join(db_path, backup_file)

    # Try to load prefiltered queries
    try:
        with open(query_path) as f:
            query_data = json.load(f)
        print(f"Loaded {len(query_data)} prefiltered queries from {dataset} ({mode} mode)")
    except FileNotFoundError:
        # Fall back to global queries file and filter
        try:
            with open(backup_query_path) as f:
                all_queries = json.load(f)

            query_data = [q for q in all_queries if q['db_id'] == dataset]
            print(f"Loaded {len(all_queries)} queries from {backup_file}, "
                  f"filtered to {len(query_data)} for {dataset}")

            # Save filtered queries for future use
            os.makedirs(os.path.dirname(query_path), exist_ok=True)
            with open(query_path, 'w') as f:
                json.dump(query_data, f, indent=2)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Could not find query data for {dataset} in {mode} mode. "
                f"Checked {query_path} and {backup_query_path}"
            )

    questions = [q['question'] for q in query_data]
    queries = [q['SQL'] for q in query_data]

    return questions, queries


def main_script():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Load source nl2sql data and generate API-sequence data"
    )
    parser.add_argument(
        '-m', '--mode',
        type=str,
        choices=['train', 'dev'],
        default='dev',
        help='Run on the train or dev set'
    )
    parser.add_argument(
        '-s', '--size',
        type=str,
        choices=['small', 'large'],
        default='small',
        help='Run all databases or a subset'
    )
    parser.add_argument(
        '-d', '--dataset',
        type=str,
        help='Process a specific dataset (overrides mode/size)'
    )
    parser.add_argument(
        '-api', '--api_style',
        type=str,
        choices=["slot", "sel"],
        default="slot",
        help='API style: slot filling or selection'
    )
    parser.add_argument(
        '--db-path',
        type=str,
        default=None,
        help='Path to the BIRD database directory (overrides BIRD_DB_PATH env var)'
    )
    args = parser.parse_args()

    # Setup paths: CLI arg > BIRD_DB_PATH env var > default relative location
    if args.db_path:
        db_path = Path(args.db_path)
    elif os.environ.get('BIRD_DB_PATH'):
        db_path = Path(os.environ['BIRD_DB_PATH'])
    else:
        db_path = Path(__file__).parent.parent.parent / "db"
    cache_path = db_path / 'cache' / args.api_style

    # Create output directories
    for api_type in ["slot", "sel"]:
        output_dir = Path("output") / api_type
        output_dir.mkdir(parents=True, exist_ok=True)

    # Determine which datasets to process
    datasets, mode = get_datasets(args.mode, args.size, args.dataset)

    if args.dataset:
        print(f"Processing single dataset: {args.dataset} ({mode} mode)")
    else:
        print(f"Processing {len(datasets)} dataset(s) in {mode} mode ({args.size} size)")

    # Process each dataset
    for dataset in datasets:
        print(f"\n{'='*60}")
        print(f"Processing: {dataset}")
        print(f"{'='*60}")

        try:
            # Load query data
            questions, queries = load_query_data(dataset, mode, str(db_path))

            # Determine database path
            database_subdir = "train_databases" if mode == 'train' else "dev_databases"
            db_path_full = str(db_path / database_subdir)

            # Initialize database loader
            loader = BirdDatabaseLoader(
                dataset,
                db_path_full,
                database_cache_location=str(cache_path)
            )

            # Generate slot-filling dataset
            if args.api_style == "slot":
                output_file_slot = f"output/{args.api_style}/{dataset}.json"
                ds_builder_slot = SqlSlotFillingDatasetBuilder(loader)
                main(dataset, output_file_slot, queries, questions, ds_builder_slot)
                print(f"✓ Generated slot-filling dataset: {output_file_slot}")

            # Generate selection dataset
            if args.api_style == "sel":
                output_file_sel = f"output/{args.api_style}/{dataset}.json"
                ds_builder_sel = SqlSelectionDatasetBuilder(loader)
                main(dataset, output_file_sel, queries, questions, ds_builder_sel)
                print(f"✓ Generated selection dataset: {output_file_sel}")

        except Exception as e:
            print(f"✗ Failed to process {dataset}: {e}")
            raise

    print(f"\n{'='*60}")
    print(f"Successfully processed {len(datasets)} dataset(s)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main_script()
    
