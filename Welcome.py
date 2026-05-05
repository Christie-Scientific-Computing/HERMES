import streamlit as st
import logging

logging.basicConfig(
        filename=None, #TODO Update when log to file
        level="INFO",
        format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
        )
logging.getLogger('httpx').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


st.set_page_config(
    page_title="Apollo",
    page_icon="🚀",
)

st.write("# Welcome to APOLLO 🚀")
st.write("**:red[APOLLO]**: :red[A]utomated :red[P]lan :red[O]rchestration for :red[L]ibrary :red[L]ookup and :red[O]utput (*thanks Claude!*)")

st.markdown(
    """
    ### Quick start
    1. Find plans
    2. Centralise & clean
    3. Export
    ### Reporting issues
    - Please open issues here: 
"""
)