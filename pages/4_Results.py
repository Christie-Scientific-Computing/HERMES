"""
Streamlit page for displaying export results. 
Should show where patients were lost & where, how many series per patient, etc...

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
    st.title("Results")
    st.markdown(f"""
        Show where patients were lost and why 

         
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