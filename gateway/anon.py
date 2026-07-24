"""
Patient ID anonymisation utilities for the HERMES gateway.

Users submit anonymised patient IDs; this module converts them to real patient IDs
for internal use, and converts real IDs back to anonymised IDs for display.

Configuration (gateway .env):
    ANON_DB_HOST, ANON_DB_PORT, ANON_DB_NAME, ANON_DB_USER, ANON_DB_PASS

If ANON_DB_HOST is not set, is_configured() returns False and callers operate
in passthrough mode (no conversion). Set it in production to enforce anonymisation.
"""
import os
import csv as _csv
import io as _io
import logging
import psycopg2
from dotenv import load_dotenv
import xmltodict
from cryptography.fernet import Fernet

load_dotenv()
logger = logging.getLogger(__name__)

ANON_CONFIG = os.getenv("ANON_CONFIG")


# ── SQL queries ──────────────────
#
# anon_id : the anonymised patient identifier that external users submit
# real_id : the real patient ID stored in HERMES / Orthanc / StatusDB


_SQL_ANON_TO_REAL = """
    SELECT key_value
    FROM   key_value
    WHERE  patient_id = ANY(%s) AND key_type_id = 1
"""

_SQL_REAL_TO_ANON = """
    SELECT key_value
    FROM   key_value
    WHERE  patient_id = ANY(%s) AND key_type_id = 0
"""

# ─────────────────────────────────────────────────────────────────────────────
class AnonDatabase():
    """Class to read key database configuration file"""

    def __init__(self):
        """If config file is set, read the XML file to populate the data and decode the username and password"""
        self.filePath = ANON_CONFIG
        self.key = "RNLMk5u0H8Ns4Avewnmf2XzsuNmu0yhMmSgiCvtHy9o="

        ## Parse config file
        with open(self.filePath) as fd:
            doc = xmltodict.parse(fd.read())

        topLevel = list(doc.keys())[0]
        self.ServerName = doc[topLevel]['keyDataBase']['dataBaseServer']#.encode('utf-8')
        self.ServerIP = doc[topLevel]['keyDataBase']['dataBaseIP']#.encode('utf-8')
        self.DBName = doc[topLevel]['keyDataBase']['dataBaseName']#.encode('utf-8')

        self.Port = doc[topLevel]['keyDataBase']['dataBasePort']#.encode('utf-8')
        self.UserName = self.decodeString(doc[topLevel]['keyDataBase']['dataBaseUserName']).decode('utf-8')
        self.PassWd = self.decodeString(doc[topLevel]['keyDataBase']['dataBasePassword']).decode('utf-8')

    def decodeString(self,inStr):
        """Decode the input string using PyCrypto libraries"""
        decryption_suite = Fernet(self.key)
        plainText = decryption_suite.decrypt(inStr)
        return plainText

    def encodeString(self, inStr):
        """Encode the input string using PyCrypto libraries"""
        encryption_suite = Fernet(self.key)
        cipherText = encryption_suite.encrypt(inStr)
        return cipherText
    
    def _connect(self):
        """Connect to remote database"""
        self.dbConnectString = f"host='{self.ServerName}' dbname='{self.DBName}' user='{self.UserName}' password='{self.PassWd}'"
        print('PASSWORD', str(self.PassWd), flush=True)
        try:
            return psycopg2.connect(
                dbname=self.DBName,
                user=self.UserName,
                password=self.PassWd,
                host=self.ServerIP,
                port=self.Port)    #NOTE - needs link-local address entry in pg_hba.conf -- unsure if this is still the case
        except Exception as exc:
            raise ConnectionError(f"Cannot connect to anonymisation DB (keyDataBase) specified in {ANON_CONFIG} {exc}") from exc


class AnonLookupError(Exception):
    """Raised when an anonymised ID has no mapping in the database."""


def is_configured() -> bool:
    """Return True if the anonymisation DB is configured in the environment."""
    return bool(ANON_CONFIG)


# def _connect():
#     try:
#         return psycopg2.connect(
#             host=ANON_DB_HOST,
#             port=ANON_DB_PORT,
#             dbname=ANON_DB_NAME,
#             user=ANON_DB_USER,
#             password=ANON_DB_PASS,
#             connect_timeout=5,
#         )
#     except Exception as exc:
#         raise ConnectionError(f"Cannot connect to anonymisation DB at {ANON_DB_HOST}: {exc}") from exc


def lookup_real_ids(anon_ids: list[str]) -> dict[str, str]:
    """
    Batch convert anonymised patient IDs to real patient IDs.

    Returns: {anon_id: real_id, ...}
    Raises AnonLookupError if any of the provided IDs has no mapping.
    """
    if not anon_ids:
        return {}

    unique = list(dict.fromkeys(anon_ids))
    anonDB = AnonDatabase()
    conn = anonDB._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_ANON_TO_REAL, (unique,))
            rows = cur.fetchall()
    finally:
        conn.close()

    mapping = {row[0]: row[1] for row in rows}
    missing = [aid for aid in unique if aid not in mapping]
    if missing:
        raise AnonLookupError(
            f"Unknown anonymised patient ID{'s' if len(missing) > 1 else ''}: "
            + ", ".join(missing)
        )
    return mapping


def lookup_anon_ids(real_ids: list[str]) -> dict[str, str]:
    """
    Batch convert real patient IDs to anonymised patient IDs.

    Returns: {real_id: anon_id, ...}
    Real IDs with no mapping return the placeholder "[unknown]" so that
    a gap in the mapping never causes a real ID to be shown to the user.
    """
    if not real_ids:
        return {}

    unique = list(dict.fromkeys(real_ids))
    anonDB = AnonDatabase()
    conn = anonDB._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_REAL_TO_ANON, (unique,))
            rows = cur.fetchall()
    finally:
        conn.close()

    mapping = {row[0]: row[1] for row in rows}
    # Fill in a safe placeholder for any unmapped real IDs
    for rid in unique:
        if rid not in mapping:
            logger.warning("Real patient ID %r has no anonymised mapping — substituting [unknown]", rid)
            mapping[rid] = "[unknown]"
    return mapping


def rewrite_csv_patient_ids(csv_bytes: bytes, id_map: dict[str, str]) -> bytes:
    """
    Return CSV bytes with the `patient_id` column replaced using id_map.
    Rows whose patient_id is not in id_map are passed through unchanged.
    """
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = _csv.DictReader(text.splitlines())
    if not reader.fieldnames or "patient_id" not in reader.fieldnames:
        return csv_bytes

    rows = list(reader)
    out = _io.StringIO()
    writer = _csv.DictWriter(out, fieldnames=reader.fieldnames)
    writer.writeheader()
    for row in rows:
        pid = (row.get("patient_id") or "").strip()
        row["patient_id"] = id_map.get(pid, pid)
        writer.writerow(row)
    return out.getvalue().encode("utf-8")
