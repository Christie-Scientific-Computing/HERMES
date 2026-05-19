"""
Streamlit page for cleaning data once in Orthanc.
Use Orthanc API calls to update DICOM headers.

Should provide list of current fields (which users are allowed to edit (e.g. patient name))
Should probably flag issues (e.g. different patient names per patient ID)

Author: Donal McSweeney
Date: 05/05/2025
Version: 0.01

Copyright (C) 2026 The Christie NHS
Foundation Trust
"""
import streamlit as st



def main():
    """
    Streamlit frontend for interacting with app.

    TODO: 
        - Report errors/plans that need reviewing
    """
    st.set_page_config(
        page_title="Hermes",
        page_icon="🪽",
        layout="wide"
    )
    st.title("Modify")
    st.markdown(f"""
        Modify metadata once data has been centralised. 
        
        **TODO** 
        
            - Need to fetch metadata from Orthanc
            - Enable edits somehow 
         
    """)
    
    if 'submitted' not in st.session_state:
        st.session_state.submitted = False


    
    st.divider()

    # Add sidebar with helpful info
    with st.sidebar:
        st.header("ℹ️ Information")

        st.info("""
        **How to use:**
        """)
        
        st.success("Logs will be saved to ./logs")


if __name__ == '__main__':
    main()