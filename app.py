import os
import json
import streamlit as st
import time
from io import StringIO
from tempfile import NamedTemporaryFile

import PyPDF2
import docx2txt
from openai import OpenAI
from supabase import create_client, Client


def get_env_variable(var_name):
    try:
        return st.secrets[var_name]
    except Exception:
        return os.getenv(var_name)


openai_api_key = get_env_variable("OPENAI_API_KEY")
supabase_url = get_env_variable("SUPABASE_URL")
supabase_key = get_env_variable("SUPABASE_KEY")


if not openai_api_key or not supabase_url or not supabase_key:
    st.error("API keys or credentials are not properly set.")
    st.stop()


# Initialize OpenAI and Supabase clients
openai_client = OpenAI(api_key=openai_api_key)
supabase: Client = create_client(supabase_url, supabase_key)


# --- Streamlit UI ---
st.title("SAQ Uploader and Parser")
st.write(
    """
    This app allows you to upload PDF, DOCX, or TXT files that contain SAQ (Short Answer Questions) data.

    The text should include a parent question (with a question stem and a category) and one or more sub-questions
    (each with a question lead, ideal answer, and key concept). The app uses OpenAI to parse the text into a nested JSON format
    and then uploads the data to two Supabase tables:

    - **saqParent**: Contains the parent question with columns: `id`, `parentQuestion` (text), and `categoryId` (integer).
    - **saqChild**: Contains sub-questions with columns: `id`, `questionLead` (text), `idealAnswer` (text), `keyConcept` (text), and `parentQuestionId` (integer).
    """
)


uploaded_files = st.file_uploader(
    "Upload PDF, DOCX, or TXT files", type=["pdf", "docx", "txt"], accept_multiple_files=True
)


if uploaded_files:
    data_list = []
    any_errors = False

    for uploaded_file in uploaded_files:
        file_name = uploaded_file.name
        st.write(f"Processing **{file_name}**...")

        try:
            if uploaded_file.type == "application/pdf":
                with NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                    temp_pdf.write(uploaded_file.read())
                    temp_pdf.flush()
                    reader = PyPDF2.PdfReader(temp_pdf.name)
                    text_content = "".join([page.extract_text() for page in reader.pages])

            elif uploaded_file.type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                with NamedTemporaryFile(delete=False, suffix=".docx") as temp_docx:
                    temp_docx.write(uploaded_file.read())
                    temp_docx.flush()
                    text_content = docx2txt.process(temp_docx.name)

            elif uploaded_file.type == "text/plain":
                stringio = StringIO(uploaded_file.getvalue().decode("utf-8"))
                text_content = stringio.read()
            else:
                st.error(f"Unsupported file type: {uploaded_file.type}")
                continue

        except Exception as e:
            st.error(f"Error reading {file_name}: {e}")
            any_errors = True
            continue

        prompt = f"""
        You will be provided with text containing SAQ data. Each question contains a parent question and one or more sub-questions. The parent question includes the main question stem and a category. Each sub-question includes a question lead, an ideal answer, and a key concept.

        Output the data in a nested JSON format where each parent question object includes:
        - parentQuestion: main question stem
        - categoryId: category identifier
        - childQuestions: a list of objects, each containing:
            - questionLead
            - idealAnswer
            - keyConcept

        Now, parse the following text and output the JSON accordingly:
        {text_content}
        """

        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that extracts SAQ information and formats it as nested JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0
            )

            json_response = response.choices[0].message.content.strip()
            json_response = json_response.replace("```json", "").replace("```", "").strip()
            parsed_data = json.loads(json_response)

            if isinstance(parsed_data, dict):
                data_list.extend(parsed_data.values())
            st.success(f"Successfully parsed {file_name}.")

        except Exception as e:
            st.error(f"Error processing {file_name}: {e}")
            any_errors = True

    if data_list:
        if st.button("Upload Data to Supabase"):
            for parent in data_list:
                try:
                    parent_record = {
                        "parentQuestion": parent.get("parentQuestion"),
                        "categoryId": parent.get("categoryId")
                    }
                    parent_response = supabase.table("saqParent").insert(parent_record).execute()
                    parent_id = parent_response.data[0]["id"]

                    for child in parent.get("childQuestions", []):
                        child_record = {
                            "questionLead": child.get("questionLead"),
                            "idealAnswer": child.get("idealAnswer"),
                            "keyConcept": child.get("keyConcept"),
                            "parentQuestionId": parent_id
                        }
                        supabase.table("saqChild").insert(child_record).execute()

                except Exception as e:
                    st.error(f"Error uploading data: {e}")

        st.success("Data uploaded successfully.")

else:
    st.write("No files uploaded.")
