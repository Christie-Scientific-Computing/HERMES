"""
Logic for the import page, endpoints call these methods
"""
import os
import re
import shutil
import logging
import sqlite3
import polars as pl
import requests
from proknow import ProKnow
from collections import defaultdict
from pyorthanc import Orthanc, Modality, find_series, upload
from dotenv import load_dotenv
from pathlib import Path

from backend.src.retrieve.PinnacleExport.entrypoint import entry as pinn_entry
from backend.src.retrieve.PinnacleExport.src.database import ExportRequest


logger = logging.getLogger(__name__)

load_dotenv()

# Configuration from .env file
ORTHANC_URL = os.getenv('ORTHANC_URL')
ORTHANC_USER = os.getenv('ORTHANC_USER')
ORTHANC_PASS = os.getenv('ORTHANC_PASS')
PULL_MODALITY_AET_ONE = os.getenv('PULL_MODALITY_AET_ONE')
PULL_MODALITY_AET_TWO = os.getenv('PULL_MODALITY_AET_TWO')
PATH_TO_CERT = os.getenv('PATH_TO_CERT')
PATH_TO_KEY = os.getenv('PATH_TO_KEY')

PINN_DB = os.getenv('PINN_DB')
STATUS_DB = os.getenv('STATUS_DB')

#Proknow setup
PROKNOW_URL = 'https://nhs.proknow.com'
PROKNOW_WORKSPACE = 'RBV - Christie'


class Importer():

    def __init__(self, import_level:str | None = None):
        self.pk: ProKnow = None # Defining here for clarity
        self.ot: Orthanc = None 
        self.pinn_db: sqlite3.Connection = None 
        self._init_connections() # This populates above
        self.dicom_sources = [PULL_MODALITY_AET_ONE, PULL_MODALITY_AET_TWO]

        self.tmp_dir = Path('./tmp/proknow/')
        self.tmp_dir.mkdir(exist_ok=True)

        # Define import level
        self._set_import_level(import_level)
        
        # Station name in RTPLAN/RTSTRUCT when uploaded to mosaiq. Will delete these ones if too many files found on orthanc
        self.pinnacle_station_name = (r'cht-pinnapp\d+', r'pinncm\d+', 'cht-vsim', 'Christie-acqsim')

    def _set_import_level(self, import_level: str) -> None:
        if import_level is None or import_level == 'Planning data':
            self.import_level = 'Planning'
            self.accepted_modalities = ("CT", "RTSTRUCT", "RTPLAN", "RTDOSE")

        elif import_level == 'Images only':
            logger.info("Setting import level to: Images")
            self.import_level = 'Images'
            self.accepted_modalities = ("CT", "MR", "REG")
            #raise NotImplementedError("Image only import not yet implemented")
        
        elif import_level == 'Everything':
            self.import_level = 'Everything'
            self.accepted_modalities = ("CT", "MR", "REG", "RTSTRUCT", "RTPLAN", "RTDOSE")
            #raise NotImplementedError("Importing everything not yet implemented")

        else:
            raise ValueError("Unknown import level.")
        logger.info(f"Import level set to: {self.import_level}. Accepted modalities: {self.accepted_modalities} ")


    def _init_connections(self) -> None:
        try:
            self.pk = ProKnow(PROKNOW_URL, 'credentials.json')
            logger.debug("Connected to Proknow")
        except Exception as exc:
            logger.error(f"Failed to connect to ProKnow: {exc}")
            raise

        try:
            self.ot = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER,
                password=ORTHANC_PASS,
                verify=False, timeout=14000.0,)
            logger.debug("Connected to Orthanc")
        except Exception as exc:
            logger.error(f"Failed to connect to Orthanc: {exc}")
            raise


    def handle_patient(self, mrn: int) -> None:
        """
        Handles a single patient:
        1. Searches locations
        2. Imports from known locations

        """
        locations: dict[str, bool] = self.find_patient(mrn)
        self.import_patient(mrn, locations)
        return {'status': 'I did it', **locations}


    def find_patient(self, mrn: int) -> dict[str, bool]:
        in_mosaiq: bool = self.search_mosaiq(mrn)
        in_pinnacle: bool = self.search_pinnacle_db(mrn)
        #TODO
        #in_raystation: bool | None = self.search_raystation(mrn)
        in_proknow: bool = self.search_proknow(mrn)

        logger.info(
            "Patient found in Mosaiq (%s), Pinnacle (%s), ProKnow (%s)",
            in_mosaiq, in_pinnacle, in_proknow
        )
        return {
            'in_mosaiq': in_mosaiq,
            'in_pinnacle': in_pinnacle,
            'in_proknow': in_proknow
        }

    def import_patient(self, mrn: int, locations: dict[str, bool]) -> None:
        
        # if locations['in_proknow']:
        #     self.import_from_proknow(mrn)
        
        if locations['in_mosaiq']:
            self.import_from_mosaiq(mrn)
        
        if locations['in_pinnacle']:
            self.import_from_pinnacle(mrn)

        #TODO Add raystation
        

        ## Clean orthanc after importing all data.
        self._cleanup_orthanc(mrn)


    ## ============= Methods for importing ===========================
    def _cleanup_orthanc(self, mrn):
        #TODO Structs and plans can be duplicated by mosaiq and pinnacle import.
        ## Since they have different UIDs and metadata in headers.
        ## Cleanup by looking at referenceSOPInstanceUID (for struct). Will link to CT, which has the same series UID across platforms. 

        study_query = {
            'Level': 'Study',
            'Query': {
                'PatientID': str(mrn),
                }
            }
        # Get series info (all data local at this point)
        series_list = find_series(client=self.ot, query=study_query["Query"])
        if not series_list:
            logger.error(f"No series found for MRN {mrn} locally after C-MOVE")
            return

        # Group by study
        studies: dict[str, list] = defaultdict(list)
        for series in series_list:
            study_uid = series.parent_study.main_dicom_tags.get("StudyInstanceUID")
            studies[study_uid].append(series)

        for study_uid, series_in_study in studies.items():
            modalities_in_study = {s.main_dicom_tags.get("Modality") for s in series_in_study}
            # Delete entire study if no RTDOSE (incomplete planning data)
            if self.import_level == 'Planning' and 'RTDOSE' not in modalities_in_study:
                logger.info(
                    "Study (%s) has no RTDOSE — deleting. Found modalities: %s",
                    study_uid, modalities_in_study
                )
                study_orthanc_id = series_in_study[0].parent_study.identifier
                self.orthanc_delete(f"/studies/{study_orthanc_id}")
                continue

            series_per_modality = {} #key=modality;val=#of series per modality
            for mod in modalities_in_study:
                series_per_modality[mod] = len([s for s in series_in_study if s.main_dicom_tags.get("Modality") == mod])

            #Get station names for RTDOSE file, will only keep structs and plans with same station
            dose_station_names = {x.main_dicom_tags.get("StationName") for x in series_in_study if x.main_dicom_tags.get("Modality") == 'RTDOSE'}
            for series in series_in_study:
                info = series.main_dicom_tags

                manufacturer = info.get("Manufacturer", "")
                modality_tag = info.get("Modality", "")  
                station_name = info.get("StationName", "")
                logger.info("MANUFACTURER: %s", manufacturer)
                # Handle additional structs from proknow, as it generates a new SOPInstanceUID when downloading from pk
                if modality_tag == 'RTSTRUCT':
                    instances = [x for x in self.ot.get_series_id_instances(series.identifier)]
                    #instances = [(x['ID'], x['Manufacturer']) for x in self.ot.get_series_id_instances(series.identifier)]
                    logger.info("INSTANCE: %s", instances)
                    if len(instances) > 1: 
                        logger.warning("Found duplicate RTSTRUCTs: (%s). Deleting those generated by ProKnow", len(instances))       
                        #TODO Delete proknow structs
                        # for (id_, manufacturer) in instances: #Delete all ProKnow structs
                        #     if manufacturer == 'ProKnow':
                        #         self.orthanc_delete(f"/instances/{id_}")

                ## Delete data we don't want
                if modality_tag not in self.accepted_modalities:
                    logger.debug("Deleting %s. Modality (%s) not accepted.", info, modality_tag)
                    self.orthanc_delete(f"/series/{series.identifier}")
                    continue
                
                if self.import_level == 'Planning' and modality_tag == 'CT' and manufacturer == 'ELEKTA':
                    # Catch CBCTs when importing planning data 
                    logger.debug("Deleting %s. CBCTs not accepted in planning mode.", info)
                    self.orthanc_delete(f"/series/{series.identifier}")
                    continue

                ## If more plans/structs than RTDOSE files
                ## Delete based on stationName = cht-pinnapp0. 
                #TODO Rely on my conversion since this is where the dose comes from? 


                ## Remove duplicate struct 
                if modality_tag == 'RTSTRUCT' and series_per_modality['RTSTRUCT'] > series_per_modality['RTDOSE'] and station_name not in dose_station_names:

                    logger.debug("Deleting %s. RTSTRUCT from mosaiq", info)
                    self.orthanc_delete(f"/series/{series.identifier}")
                    continue
                    
                if modality_tag == 'RTPLAN' and series_per_modality['RTPLAN'] > series_per_modality['RTDOSE'] and station_name not in dose_station_names:

                    logger.debug("Deleting %s. RTPLAN from mosaiq", info)
                    self.orthanc_delete(f"/series/{series.identifier}")
                    continue
                


    def import_from_mosaiq(self, mrn: int):
        #TODO Import all data into central orthanc
        # FIlter based on condition
        # Delete those not meeting condition
        logger.info("Importing data from mosaiq")

        study_query = {
            'Level': 'Study',
            'Query': {
                'PatientID': str(mrn),
                }
            }
        
        # Fetch all data 
        for src in self.dicom_sources:
            modality = Modality(self.ot, src)
            try:
                response = modality.find(study_query)
            except Exception as exc: # This can happen, usually if study not in this source
                logger.debug('Study not found in source (%s)', src)
                continue 
            
            # Centralise
            try: 
                res = modality.move(response['ID'])
            except Exception as exc: # This shouldn't happen, and user needs to be alerted
                logger.error('Failed to C-MOVE from %s', src)
                raise 


    def import_from_pinnacle(self, mrn: int):

        export_requests = self.get_pinn_export_requests(mrn)

        payload = {
            'remote_IP': '192.168.117.5',
            'remote_port': 32806,
            'remote_AE_title': 'old_mosaiq_router',
            'requests': export_requests
        }
        pinn_entry(payload)


    def import_from_proknow(self, mrn):

        # Download locally
        patients = self.pk.patients.lookup(PROKNOW_WORKSPACE, [str(mrn)])
        assert len(patients) == 1
        patient = patients[0].get()
        entities = [x.get() for x in patient.find_entities(lambda x: True)]
        for entity in entities:
            logger.info("Downloading entity (%s) from proknow", entity.data['type'])
            dl_path = entity.download(str(self.tmp_dir))
            if os.path.isdir(dl_path):
                logger.info("Downloaded %s files to %s", len(os.listdir(dl_path)), dl_path)
                # Images are downloaded without dcm extension, so orthanc directory upload doesn't work.
                # Upload files individually instead.
                for file in os.listdir(dl_path):
                    upload(self.ot, os.path.join(dl_path, file), check_before_upload=True)

            else:
                logger.info("Downloaded %s", dl_path)
                upload(self.ot, dl_path, check_before_upload=True)
        shutil.rmtree(self.tmp_dir)
        

    ## ============= Methods to search for a single patient across locations =================== 
    def search_mosaiq(self, mrn: int) -> bool:
        study_query = {
            "Level": "Study",
            "Query": {"PatientID": str(mrn)}
        }

        # Get studies with associated RTDOSE i.e.(Hopefully) complete planning data 
        for src in self.dicom_sources:
            modality = Modality(self.ot, src)
            try:
                studies = modality.find(study_query)
                return True
            except Exception as e:
                logger.debug(f'Could not find {mrn} in {src}')
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
                for s in series['answers']:
                    if self.import_level in ('Planning', 'Everything') and s['Modality'] == 'RTDOSE': 
                        #If at least one RTDOSE (i.e. complete planning data)
                        return True

                    if self.import_level in ('Images', 'Everything') and s['Modality'] in self.accepted_modalities:
                        return True

        return False # If no RTDOSE found or import_level=Images and no images 

    def search_pinnacle_db(self, mrn: int) -> bool:
        try:
            conn = sqlite3.connect(PINN_DB)
            conn.row_factory = sqlite3.Row
        except Exception as exc:
            logger.error(f"Failed to connect to Pinnacle database ({PINN_DB}): {exc}")
            raise

        cursor = conn.cursor()
        entries = cursor.execute(f"SELECT * FROM entries WHERE MedicalRecordNumber = {mrn}").fetchall()
        if entries:
            return True
        else:
            logger.debug("Patient (%s) not found in Pinnacle DB", mrn)
            return False
        conn.close()

    def search_raystation(self, mrn: int) -> bool:
        raise NotImplementedError("Raystation search not implemented")

    def search_proknow(self, mrn: int) -> bool:
        try:
            patient = self.pk.patients.find(workspace = PROKNOW_WORKSPACE, mrn=str(mrn)).get()
            return True
        except Exception as exc:
            logger.debug("Patient isn't on ProKnow. Error: %s", exc)
            return False
    
    #### ========================= HELPERS ================================
    @staticmethod
    def get_pinn_export_requests(mrn: int) -> list[ExportRequest]:
        """
        Given MRN, will return a list of export requests (for all paths & Pinnacle IDs found)
        """
        try:
            conn = sqlite3.connect(PINN_DB)
            conn.row_factory = sqlite3.Row
        except Exception as exc:
            logger.error(f"Failed to connect to Pinnacle database (./db/pinn_db.sqlite): {exc}")
            raise
        cursor = conn.cursor()
        entries = cursor.execute(f"SELECT * from entries WHERE MedicalRecordNumber = {mrn}").fetchall()
        
        requests = []
        for entry in entries:
            request = ExportRequest(mrn=mrn, patient_id=entry['PinnacleID'], path=Path(entry['Path']))
            requests.append(request)
        conn.close()
        return requests

    @staticmethod
    def read_input_file(csv_path: Path) -> list[dict]:
        """
        Function to read input args from CSV and parses into a list of dicts
        """
        accepted_args = {
            'patient_id': int,
        }
        df = pl.read_csv(csv_path, infer_schema=False)
        all_data = []
        ids = []
        for row in df.rows(named=True):
            assert 'patient_id' in row, "Patient ID not provided!"
            
            patient_dict = {}
            for key, val in row.items():            
                if key == 'patient_id':
                    if val.startswith('#'):
                        continue
                    if val in ids: # Skip if already in requests
                        continue
                    ids.append(val)

                patient_dict[key] = accepted_args[key](val)

            if patient_dict:
                all_data.append(patient_dict)
        return all_data
    
    @staticmethod
    def orthanc_delete(resource_url):
        url = f"{ORTHANC_URL}{resource_url}"
        resp = requests.delete(url, auth=(ORTHANC_USER, ORTHANC_PASS), verify=PATH_TO_CERT)
        resp.raise_for_status()
