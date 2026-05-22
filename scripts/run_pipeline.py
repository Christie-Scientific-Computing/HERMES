"""
Script for automatically centralising data and exporting to destination
(Combination of retrieve and export).
Users can't modify data from this script - should we implement this?
"""
import os
import logging
import requests
import polars as pl
from datetime import datetime
import uuid
import json
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
        filename=f'/config/Hermes/logs/{datetime.now().strftime("logfile_%Y-%m-%d_%H-%M-%S.log")}', #TODO Update when log to file
        level="INFO",
        format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
        )
logging.getLogger('httpx').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


BACKEND_URI = os.getenv('BACKEND_URI')
BACKEND_PORT = os.getenv('BACKEND_PORT')

import_level = 'Planning data'
file_path = '/config/Hermes/data/inputs/MaleBreast/MaleBreastTest.csv'
job_id = str(uuid.uuid4())
proknow_collection = 'TEST_MaleBreast_Audit'

def main():
    import_url = f"http://{BACKEND_URI}:{BACKEND_PORT}/import/single_import"
    export_url = f"http://{BACKEND_URI}:{BACKEND_PORT}/export/proknow_upload_patient"
    ## Submit import requests
    df = pl.read_csv(file_path, infer_schema=None)
    logger.info(f"Exporting {df.shape[0]} patients")
    for row in df.iter_rows(named=True):
        logger.info(f"----- Processing {row['patient_id']} -------")
        payload = {"job_id": job_id, "mrn": row['patient_id'], "import_level": import_level}
        res = requests.post(import_url, json=payload, timeout=(10, None))
        
        if res.status_code == 200:
            content = res.json()
            data = json.loads(content.lstrip('data:'))
        else:
            logger.error(f"Request failed. Status: {res.status_code}")
            continue

        if data['type'] == 'success':
            logger.debug("Import success")
            # Submit export request
            payload = {"job_id": job_id, "mrn": row['patient_id'], "collection": proknow_collection}
            res = requests.post(export_url, json=payload, timeout=(10, None))
            if res.status_code == 200:
                logger.info("Exported successfully")
                content = res.json()
                data = json.loads(content.lstrip('data:'))
            else:
                logger.error(f"Request failed: {res.status_code}")
                continue

            if data['type'] == 'success':
                logger.info("Exported successfully")
            
            else:
                logger.error(f"Export failed: {data}")
            
        else:
            logger.error(f"Import failed: {data}")
    
    
if __name__ == '__main__':
    main()