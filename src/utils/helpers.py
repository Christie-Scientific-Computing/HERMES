from pathlib import Path
import tomllib
import logging
import csv

def init_config_file() -> dict:
    # Checks config file setup & converts args to expected dtype
    # Raises errors if missing required args 
    target_dtypes: dict = {
        'log-dir': Path,
        'log-to-file': bool,
        'log-level': str,
        'patients-file': Path,
        'pinnacle-db': Path
    }

    with open('./config.toml', 'rb') as f:
        config = tomllib.load(f)
    
    # Convert args to expected dtype
    for key, val in config.items():
        try:
            config[key] = target_dtypes[key](val)
        except (ValueError, TypeError):
            # Skip or handle conversion errors
            print(f"Warning: Could not convert key '{key}' with value '{value}' to {target_dtype[key].__name__}")
    return config

def init_logger(config: dict) -> None:
    """
    Init. logger based on config file
    """
    if config['log-to-file']:
        config['log-dir'].mkdir(exist_ok=True)
        log_filename = config['log-dir'] / datetime.now().strftime("logfile_%Y-%m-%d_%H-%M-%S.log")

    log_level = config['log-level']
    level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(
        filename=log_filename if config["log-to-file"] else None,
        level=level,
        format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
        )
    logging.getLogger('httpx').setLevel(logging.WARNING)
    return logging.getLogger(__name__)

def read_patients_file(patient_list: Path) -> list[dict]:
    """
    Function to read input args from CSV and parses into a list of dicts
    """
    accepted_args = {
        'patient_id': str,
    }
    reader = csv.DictReader(open(patient_list))
    all_data = []
    ids = []
    for row in reader:  
        patient_dict = {}
        for col, val in row.items():            
            if col == 'patient_id':
                if val.startswith('#'):
                    continue
                if val in ids: # Skip if already in requests
                    continue
                ids.append(val)

            patient_dict[col] = accepted_args[col](val)

        if patient_dict:
            all_data.append(patient_dict)
    return all_data
