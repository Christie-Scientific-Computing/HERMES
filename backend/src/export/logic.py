"""
Export logic
"""
import os
import hashlib
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


def checksummed_series_manifest(client: Orthanc, series_list: list) -> dict:
    """
    Enumerate a list of already-found Series for the audit manifest --
    SeriesInstanceUIDs, instance count, and a checksum per instance (Orthanc's
    own stored MD5, confirmed present via get_instances_id_attachments_name_md5
    on the pinned pyorthanc==1.23.0).

    Shared by both DICOM-C-MOVE manifest builders (Exporter._build_manifest,
    for the plain patient-level export; export/endpoints.py's
    _build_uid_manifest, for the UID-based one) -- they arrive at their
    series_list differently (PatientID vs. StudyInstanceUID/SeriesInstanceUID
    query), but the per-series/per-instance checksum-collection loop itself
    is identical, so it lives here once rather than twice. Does not compute
    study_uids -- callers enumerate studies their own way (a PatientID query
    here, vs. a StudyInstanceUID query for the UID-based path) and merge that
    in themselves.
    """
    series_uids: list[str] = []
    checksums: dict[str, str] = {}
    instance_count = 0

    for series in series_list:
        series_uid = series.main_dicom_tags.get("SeriesInstanceUID") or series.identifier
        series_uids.append(series_uid)

        instances = client.get_series_id_instances(series.identifier)
        for instance in instances:
            instance_count += 1
            instance_id = instance.get("ID")
            sop_uid = instance.get("MainDicomTags", {}).get("SOPInstanceUID") or f"orthanc:{instance_id}"
            try:
                md5 = client.get_instances_id_attachments_name_md5(id_=instance_id, name="dicom")
                checksums[sop_uid] = md5.decode() if isinstance(md5, bytes) else str(md5)
            except Exception as exc:
                logger.warning("Could not fetch checksum for instance %s: %s", instance_id, exc)

    return {
        "series_count": len(series_list),
        "instance_count": instance_count,
        "series_uids": series_uids,
        "checksums": checksums,
    }


class Exporter():
    def __init__(self, destination: str):
        self.destination = destination # DICOM SCP or collection
        self.tmp_dir = Path('./tmp')
        self.tmp_dir.mkdir(exist_ok=True)

    def upload_to_proknow(self, patient_id: str):
        try:
            client = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER,
                    password=ORTHANC_PASS, verify=False,
                    timeout=14000.0,)
            logger.info("Connected to Orthanc")
        except Exception as exc:
            logger.error(f"Failed to connect to Orthanc: {exc}")
            raise

        series_dict, manifest = self.download_data(client, patient_id, self.tmp_dir)

        #TODO Upload to ProKnow
        study_status = {}
        for study_uid in series_dict.keys():
            input_dir = self.tmp_dir / study_uid
            try:
                self.upload_study_to_proknow(input_dir, self.destination)
            except ValueError as e:
                logger.error(f"Error occured during proknow upload: {e}")

            shutil.rmtree(input_dir)

        return {'status': 'Success', **manifest}

    def dicom_c_move(self, patient_id: str, message_id: int | None = None):
        """
        `message_id`, when given, is sent to Orthanc as `MoveOriginatorID` --
        Orthanc includes it as the Move Originator Message ID (DICOM tag
        0000,1031) on the outgoing C-STORE association, exactly as if this
        store had resulted from a real C-MOVE request bearing that Message
        ID. This is how a receiving anonymising node on the DMZ can be told
        which pseudonymisation table to apply to an otherwise ordinary
        export (e.g. a clinical-trial patient who needs a different
        PatientID mapping than routine pseudo-anonymisation) -- it's a
        signalling channel to the destination, not something HERMES
        interprets itself. Must fit DICOM's Message ID VR (US, unsigned
        16-bit): 0-65535.
        """
        if message_id is not None and not (0 <= message_id <= 65535):
            raise ValueError(f"message_id must be between 0 and 65535 (got {message_id})")

        try:
            client = Orthanc(url=ORTHANC_URL, username=ORTHANC_USER,
                    password=ORTHANC_PASS, verify=False,
                    timeout=14000.0,)
            logger.info("Connected to Orthanc")
        except Exception as exc:
            logger.error(f"Failed to connect to Orthanc: {exc}")
            raise ValueError("Could not connect to Orthanc")

        series_list = find_series(client=client, query={"PatientID": str(patient_id)})

        if not series_list:
            logger.error("No series found in Orthanc for patient.")
            raise ValueError("No series found in Orthanc.")

        # Manifest, built before the actual C-MOVE is triggered (see
        # docs/safety-plan.md SS D2). dicom_c_move only enumerates at the
        # series level today (series_list, above) -- instance counts and
        # per-instance checksums are net-new queries for this path. Unlike
        # the ProKnow path, the DICOM bytes never land locally here, so
        # checksums come from Orthanc's own stored MD5 rather than being
        # re-hashed from bytes HERMES doesn't have.
        manifest = self._build_manifest(client, patient_id, series_list)

        series_to_send = [x.identifier for x in series_list]
        store_json = {
            "Resources": series_to_send,
            "Synchronous": False,
        }
        if message_id is not None:
            store_json["MoveOriginatorID"] = message_id
        try:
            res = client.post_modalities_id_store(
                id_=self.destination,
                json=store_json,
            )
            logger.info("DICOM moved")
        except Exception as e:
            logging.error("Could not send to destination: %s", self.destination)
            raise ValueError(f"Could not send to destination: {self.destination}")

        return {'status': 'Success', **manifest}

    @staticmethod
    def _build_manifest(client: Orthanc, patient_id: str, series_list: list) -> dict:
        """
        Enumerate what's about to be C-MOVE'd, for the audit record --
        StudyInstanceUIDs, plus the SeriesInstanceUIDs/instance
        counts/checksums that checksummed_series_manifest (module-level,
        shared with export/endpoints.py's UID-based path) already collects
        from series_list.

        DICOM C-MOVE is fire-and-forget ("Synchronous": False in the caller)
        -- this manifest describes what was *asked to leave*, not
        confirmation that it arrived. Polling Orthanc's Job API for
        completion is an explicitly separate future increment, not built
        here (see docs/safety-plan.md SS D2).
        """
        studies = find_studies(client=client, query={"PatientID": str(patient_id)})
        study_uids = [s.main_dicom_tags.get("StudyInstanceUID") for s in studies]
        study_uids = [uid for uid in study_uids if uid]

        manifest = checksummed_series_manifest(client, series_list)
        manifest["study_uids"] = study_uids
        return manifest

    def upload_study_to_proknow(self, path: Path, collection: str = None):
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

        
        if collection is not None:
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
    def download_data(client: Orthanc, patient_id: str, local_dir: Path) -> tuple[dict, dict]:
        """
        Download data from orthanc to a local directory.

        Returns (series_dict, manifest). series_dict is unchanged from
        before -- {study_uid: [Series, ...]}, used by the caller to know
        which per-study directories to upload/clean up. manifest is new
        (docs/safety-plan.md SS D2): study/series UIDs, series/instance
        counts, and a sha256 checksum per instance, computed directly over
        the bytes already pulled to disk here -- genuinely close to free,
        since this path (unlike DICOM C-MOVE) already has every instance's
        bytes in hand for the ProKnow upload, no extra Orthanc round-trip
        needed to hash them.
        """
        query = {
            "Level": "Study",
            "Query": {"PatientID": patient_id}
        }
        studies = find_studies(client=client, query=query['Query'])

        series_dict = {}
        manifest = {
            "study_uids": [],
            "series_uids": [],
            "series_count": 0,
            "instance_count": 0,
            "checksums": {},
        }
        for study in studies:
            study_uid = study.main_dicom_tags["StudyInstanceUID"]
            manifest["study_uids"].append(study_uid)
            series_query = {
                'Level': 'Series',
                'Query': {
                    'StudyInstanceUID': study_uid,
                    'Modality': ''
                }
            }
            series_dict[study_uid] = find_series(client, series_query['Query'])

        def download_instances(client: Orthanc, instances: list, output_dir: Path, manifest: dict):
            # Download a single dicom instance
            for slice_index, instance in enumerate(instances, start=1):
                dicom_bytes = client.get_instances_id_file(instance['ID'])
                filename = output_dir / f"Slice_{instance['ID']}.dcm"
                with open(filename, "wb") as f:
                    f.write(dicom_bytes)

                sop_uid = instance.get("MainDicomTags", {}).get("SOPInstanceUID") or f"orthanc:{instance['ID']}"
                manifest["checksums"][sop_uid] = hashlib.sha256(dicom_bytes).hexdigest()
                manifest["instance_count"] += 1


        for i, (study_uid, series_list) in enumerate(series_dict.items()):
            study_path = local_dir / study_uid
            logger.debug("Writing to %s", study_path)
            study_path.mkdir(parents=True, exist_ok=True)

            for series in series_list:
                series_uid = series.main_dicom_tags.get("SeriesInstanceUID") or series.identifier
                manifest["series_uids"].append(series_uid)
                manifest["series_count"] += 1

                instances = client.get_series_id_instances(series.identifier)
                logger.debug("Downloading %s slice(s)", len(instances))
                download_instances(client, instances, study_path, manifest)

        return series_dict, manifest
