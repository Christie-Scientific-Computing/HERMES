"""
Script to query different databases, returns a csv of patient IDs and where they are.
"""
import os
import sqlite3
import logging
import polars as pl
from pathlib import Path
from proknow import ProKnow
from dotenv import load_dotenv
from pyorthanc import Orthanc, Modality, find_series
from src.utils.helpers import init_config_file, init_logger, read_patients_file

load_dotenv()

# Configuration from .env file
ORTHANC_URL = os.getenv('ORTHANC_URL')
ORTHANC_USER = os.getenv('ORTHANC_USER')
ORTHANC_PASS = os.getenv('ORTHANC_PASS')
PULL_MODALITY_AET_ONE = os.getenv('PULL_MODALITY_AET_ONE')
PULL_MODALITY_AET_TWO = os.getenv('PULL_MODALITY_AET_TWO')
PATH_TO_CERT = os.getenv('PATH_TO_CERT')
PATH_TO_KEY = os.getenv('PATH_TO_KEY')

#Proknow setup
PROKNOW_URL = 'https://nhs.proknow.com' #Hard-coded on purpose
PROKNOW_WORKSPACE = 'RBV - Christie'

logger = logging.getLogger(__name__)


def query_pinnacle_db(patient_id: str) -> bool:
    conn = sqlite3.connect(config['pinnacle-db'])
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    entries = cursor.execute(f"SELECT * FROM entries WHERE MedicalRecordNumber = {patient_id}").fetchall()
    if entries:
        return True
    else:
        logger.warning("Patient (%s) not found in Pinnacle DB", patient_id)
        return False

def query_mosaiq_datadirector(client, patient_id: str) -> bool:
    
    study_query = {
        "Level": "Study",
        "Query": {"PatientID": patient_id}
    }

    results = []
    for src in [PULL_MODALITY_AET_ONE, PULL_MODALITY_AET_TWO]:
        modality = Modality(client, src)
        try:
            studies = modality.find(study_query)
        except Exception as e:
            logger.error(f'Could not find {patient_id} in {src}')
            continue
        study_uids = [s['StudyInstanceUID'] for s in studies['answers'] if 'StudyInstanceUID' in s]
        
        
        for study_uid in study_uids:
            series_query = {
                'Level': 'Series',
                'Query': {
                    'StudyInstanceUID': study_uid,
                    'Modality': ''
                }
            }
            series = modality.find(series_query)
            for ser in series['answers']:
                out = {
                    'SeriesInstanceUID': ser.get("SeriesInstanceUID"),
                    'Modality': ser.get("Modality")
                }
                if out['Modality'] == 'RTDOSE': #If at least one RTDOSE
                    return True

    return False # Only triggers if no RTDOSE found

def query_raystation(patient_id: str) -> bool:
    #TODO Implement with RS query/retrieve?
    ...

def query_proknow(client, patient_id: str) -> bool:
    try:
        patient = client.patients.find(workspace = PROKNOW_WORKSPACE, mrn=patient_id).get()
        return True
    except Exception as exc:
        logger.warning("Patient isn't on ProKnow. Error: %s", exc)
        return False
    

def main(data=None, stop_event=None):
    global logger, config
    config = init_config_file()
    #logger = init_logger(config)

    if data is None:
        patients = read_patients_file(config['patients-file'])
    else:
        patients = read_patients_file(data['patients-file'])
    
    logger.info("Found %s patients in file.", len(patients))

    # Connect to Proknow
    try:
        pk = ProKnow(PROKNOW_URL, './credentials.json')
        logger.info("Connected to Proknow")#
    except Exception as exc:
        logger.error(f"Failed to connect to ProKnow: {exc}")
        raise


    # Connect to Orthanc
    try:
        client = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER, password=ORTHANC_PASS, verify=False, timeout=14000.0,)
        logger.info("Connected to Orthanc")
    except Exception as exc:
        logger.error(f"Failed to connect to ProKnow: {exc}")
        raise

    all_data = []
    for patient in patients:
        if stop_event and stop_event.is_set():
            logger.info("Stop requested - aborting.")
            break
        patient_id = patient['patient_id']
        logger.info("### Processing %s ###", patient_id)
        # Query mosaiq
        in_mosaiq = query_mosaiq_datadirector(client, patient_id)

        # Query Pinnacle db
        in_pinnacle = query_pinnacle_db(patient_id)

        # Query RS
        #in_raystation = query_raystation(patient_id)

        # Query Proknow
        in_proknow = query_proknow(pk, patient_id)


        data = {
            "patient_id": patient_id,
            "mosaiq": in_mosaiq,
            "pinnacle": in_pinnacle,
            "proknow": in_proknow
        }
        logger.info(data)
        all_data.append(data)

        #break
    
    df = pl.from_dicts(all_data)
    
    df.write_csv("./data/outputs/test.csv")