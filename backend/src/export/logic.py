"""
Export logic
"""
import os
import logging
import shutil
from pathlib import Path
import polars as pl
from proknow import ProKnow
from pyorthanc import Orthanc, find_series, find_studies
from dotenv import load_dotenv

ORTHANC_URL = os.getenv('ORTHANC_URL')
ORTHANC_USER = os.getenv('ORTHANC_USER')
ORTHANC_PASS = os.getenv('ORTHANC_PASS')

PROKNOW_URL = 'https://nhs.proknow.com' #Hard-coded on purpose
PROKNOW_WORKSPACE = 'RBV - Christie'

logger = logging.getLogger(__name__)

class Exporter():
    def __init__(self, destination: str):
        self.destination = destination # DICOM SCP
        self.tmp_dir = Path('./tmp')

    def upload_to_proknow(self, patient_id: str):
        try:
            client = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER,
                    password=ORTHANC_PASS, verify=False,
                    timeout=14000.0,)
            logger.info("Connected to Orthanc")
        except Exception as exc:
            logger.error(f"Failed to connect to Orthanc: {exc}")
            raise

        series_dict = self.download_data(client, patient_id, self.tmp_dir)

        #TODO Upload to ProKnow
        study_status = {}
        for study_uid in series_dict.keys():
            input_dir = self.tmp_dir / study_uid

            self.upload_study_to_proknow(input_dir, self.destination)
            #shutil.rmtree(input_dir)

        return {'status': 'Success'}

    def dicom_c_move(self, patient_id: str):
        try:
            client = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER,
                    password=ORTHANC_PASS, verify=False,
                    timeout=14000.0,)
            logger.info("Connected to Orthanc")
        except Exception as exc:
            logger.error(f"Failed to connect to Orthanc: {exc}")
            raise
        
        series_list = find_series(client=client, query={"PatientID": str(patient_id)})

        if not series_list:
            logger.error(f"No series found for patient ({mrn}).")
            raise ValueError

        series_to_send = [x.identifier for x in series_list]
        try:
            res = client.post_modalities_id_store(
                id_=self.destination, 
                json={
                    "Resources": series_to_send,
                    "Synchronous": False
                }
            )
            logger.info("DICOM moved")
        except Exception as e:
            logging.error("Could not send patient (%s) to dest: %s", patient_id, self.destination)
            raise
        
        return {'status': 'Success'}

    def upload_study_to_proknow(self, path: Path, collection: str):
        logger.info(f"UPLOADING: {path}")
        try:
            pk = ProKnow(PROKNOW_URL, './credentials.json')#
        except Exception as exc:
            logger.error("Could not connect to ProKnow: %s", exc)
            raise ValueError("Could not connect to ProKnow: %s", exc)

        try:
            logger.info("Uploading %s to ProKnow", str(path))
            batch = pk.uploads.upload(PROKNOW_WORKSPACE, str(path), wait=True)
            #raise ValueError
        except Exception as exc:
            logger.error("Upload failed. %s", exc)
            raise ValueError("Upload failed. %s", exc)

        # If empty upload result
        if batch is None:
            logger.error("Data already on ProKnow, skipping")
            raise ValueError("Data already on ProKnow, skipping")
        
        if not batch.patients:
            logger.warning("Uploads need attention!")
            raise ValueError("Uploads need attention!")

        assert len(batch.patients) == 1 
        #NOTE The batch object keeps track of uploads to proknow
        # If only RTSTRUCT uploaded, means the data is already on PK but PK changed the structUID 

        patient = batch.patients[0]

        # Check not all RTSTRUCT in uploads, if true Remove them.
        if all([x.data['type'] == 'structure_set' for x in patient.entities]):
            logger.warning("Only uploaded RTSTRUCT to ProKnow, will remove them.")
            #TODO remove them
            for ent in patient.entities:
                entity = ent.get()
                entity.delete()

            raise ValueError("Patient already on ProKnow. Additional RTSTRUCTs will be removed.")            

        # Move to collection
        dose_id = [{"patient": patient.id, "entity": entity.id} for entity in patient.entities if entity.data['type'] == 'dose']
        if not dose_id:
            raise ValueError("Dose expected but not found on ProKnow")

        

        collection = pk.collections.find(workspace=PROKNOW_WORKSPACE, name=collection).get()
        collection.patients.add(PROKNOW_WORKSPACE, dose_id)

        return {'status': 'Success'}

    @staticmethod
    def read_input_file(csv_path: Path) -> list[dict]:
        """
        Function to read input args from CSV and parses into a list of dicts
        """
        accepted_args = {
            'patient_id': str,
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
    def download_data(client: Orthanc, patient_id: str, local_dir: Path) -> list[Path]:
        """
        Download data from orthanc to a local directory
        """
        query = {
            "Level": "Study",
            "Query": {"PatientID": patient_id}
        }
        studies = find_studies(client=client, query=query['Query'])
        
        series_dict = {}
        for study in studies:
            series_query = {
                'Level': 'Series',
                'Query': {
                    'StudyInstanceUID': study.main_dicom_tags["StudyInstanceUID"],
                    'Modality': ''
                }
            }
            series_dict[study.main_dicom_tags["StudyInstanceUID"]] = find_series(client, series_query['Query'])

        def download_instances(client: Orthanc, instances: list, output_dir: Path):
            # Download a single dicom instance
            for slice_index, instance in enumerate(instances, start=1):
                dicom_bytes = client.get_instances_id_file(instance['ID'])
                filename = output_dir / f"Slice_{instance['ID']}.dcm"
                with open(filename, "wb") as f:
                    f.write(dicom_bytes)


        for i, (study_uid, series_list) in enumerate(series_dict.items()):
            study_path = local_dir / study_uid
            logger.debug("Writing to %s", study_path)
            study_path.mkdir(parents=True, exist_ok=True)

            for series in series_list:
                instances = client.get_series_id_instances(series.identifier)
                logger.debug("Downloading %s slice(s)", len(instances))
                download_instances(client, instances, study_path)

        return series_dict
        