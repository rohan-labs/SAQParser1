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
    (each with a question lead and an ideal answer). The app uses OpenAI to parse the text into a nested JSON format
    and then uploads the data to two Supabase tables:
   
    - **saqParent**: Contains the parent question with columns: `id`, `parentQuestion` (text), and `categoryId` (integer).
    - **saqChild**: Contains sub-questions with columns: `id`, `questionLead` (text), `idealAnswer` (text), 'keyConcept' (text) and `parentQuestionId` (integer).
    """
)


uploaded_files = st.file_uploader(
    "Upload PDF, DOCX, or TXT files", type=["pdf", "docx", "txt"], accept_multiple_files=True
)


if uploaded_files:
    data_list = []  # This will store the parsed parent records (with nested child questions)
    any_errors = False


    for uploaded_file in uploaded_files:
        file_name = uploaded_file.name
        st.write(f"Processing **{file_name}**...")


        # --- Read file content based on type ---
        try:
            if uploaded_file.type == "application/pdf":
                with NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                    temp_pdf.write(uploaded_file.read())
                    temp_pdf.flush()
                    reader = PyPDF2.PdfReader(temp_pdf.name)
                    text_content = ""
                    for page in reader.pages:
                        text_content += page.extract_text() or ""
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


        # --- Use OpenAI API to parse the content into nested JSON ---
        max_retries = 3
        retry_delay = 5  # seconds


        for attempt in range(max_retries):
            try:
                full_text_content = text_content


                prompt = f"""
You will be provided with text containing SAQ (Short Answer Questions) data. Each question contains a parent question and one or more sub-questions. The parent question includes the main question stem and a category. Each sub-question includes a question lead and an ideal answer.


You must output the data in a nested JSON format where each key at the root is a unique identifier for a parent question. Each parent question object must include the following keys:
- **parentQuestion**: the main question stem (string)
- **categoryId**: an integer representing the category
- **childQuestions**: a list of objects, each with:
    - **questionLead**: the sub-question text (string)
    - **idealAnswer**: the ideal answer text (string)
    - **keyConcept**: the key concept of the sub-question (string)


Ensure that you include every detail from the text. Do not omit or summarize any information. Do not add any additional keys.


For example, if the text is:
"Parent Question: What is the capital of France? Category: 2.
Sub-question 1: What river runs through Paris? Ideal Answer: The Seine.
Sub-question 2: What famous tower is located in Paris? Ideal Answer: The Eiffel Tower."


The JSON should be:
{{
  "0": {{
    "parentQuestion": "What is the capital of France?",
    "categoryId": 2,
    "childQuestions": [
      {{
        "questionLead": "What river runs through Paris?",
        "idealAnswer": "The Seine",
        "keyConcept": "Paris"
      }},
      {{
        "questionLead": "What famous tower is located in Paris?",
        "idealAnswer": "The Eiffel Tower",
        "keyConcept": "Paris"
      }}
    ]
  }}
}}


Now, parse the following text and output the JSON accordingly:
{full_text_content}
                """


                response = openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant that extracts SAQ information and formats it as nested JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0,
                    max_tokens=None
                )


                json_response = response.choices[0].message.content.strip()
                # Remove potential code block markers
                json_response = json_response.replace("```json", "").replace("```", "").strip()


                parsed_data = json.loads(json_response)


                # Expecting a dictionary where each key is a unique parent record
                if isinstance(parsed_data, dict):
                    for key, parent_obj in parsed_data.items():
                        data_list.append(parent_obj)
                else:
                    data_list.append(parsed_data)


                st.success(f"Successfully parsed **{file_name}**.")
                break  # exit the retry loop if successful


            except json.JSONDecodeError as json_error:
                if attempt < max_retries - 1:
                    st.warning(f"Error parsing JSON for {file_name}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    st.error(f"Error parsing JSON for {file_name} after {max_retries} attempts: {json_error}")
                    st.error(f"Raw response: {json_response}")
                    any_errors = True
            except Exception as e:
                st.error(f"Error processing {file_name}: {e}")
                any_errors = True
                break


    if data_list:
        st.write("### Parsed Data:")
        st.json(data_list)


        # --- Confirm before uploading ---
        if st.button("Upload Data to Supabase"):
            st.write("Uploading data to Supabase...")
            upload_errors = False


            # Process each parsed parent record
            for parent in data_list:
                try:
                    # Prepare the parent record for insertion (saqParent)
                    parent_record = {
                        "parentQuestion": parent.get("parentQuestion"),
                        "categoryId": parent.get("categoryId")
                    }
                    # Insert the parent record and return the inserted record to get its ID.
                    parent_response = supabase.table("saqParent").insert(parent_record, returning="representation").execute()


                    if parent_response.data and len(parent_response.data) > 0:
                        parent_id = parent_response.data[0]["id"]
                        st.success(f"Inserted parent question: {parent_record['parentQuestion']}")


                        # Process each child question under this parent
                        child_questions = parent.get("childQuestions", [])
                        for child in child_questions:
                            child_record = {
                                "questionLead": child.get("questionLead"),
                                "idealAnswer": child.get("idealAnswer"),
                                "keyConcept": child.get("keyConcept"),
                                "parentQuestionId": parent_id
                            }
                            child_response = supabase.table("saqChild").insert(child_record, returning="representation").execute()
                            if child_response.data and len(child_response.data) > 0:
                                st.success(f"Inserted child question: {child_record['questionLead']}")
                            else:
                                st.error(f"Failed to insert child question: {child_record['questionLead']}")
                                upload_errors = True
                    else:
                        st.error(f"Failed to insert parent question: {parent_record['parentQuestion']}")
                        upload_errors = True
                except Exception as e:
                    st.error(f"Exception during upload for parent question '{parent.get('parentQuestion')}': {e}")
                    upload_errors = True


            if not upload_errors:
                st.success("All data processed successfully.")
            else:
                st.warning("Some data may have failed to upload. Please check the messages above.")
    else:
        if any_errors:
            st.warning("No data to upload due to errors in processing files.")
        else:
            st.warning("No data was parsed from the uploaded files.")
else:
    st.write("No files uploaded.")
