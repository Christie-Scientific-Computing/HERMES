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
    page_title="Hermes",
    page_icon="🪽",
    layout="wide"
)

st.write("# 🪽 HERMES 🪽")
st.write(":red[H]andles :red[E]verything: :red[R]etrieve, :red[M]odify and :red[E]xport :red[S]tuff")

st.markdown(
    """
    ### Quick start
    1. Import plans
    2. Clean metadata / Anonymise
    3. Export
    ### Reporting issues
    - Please open issues here: 
"""
)