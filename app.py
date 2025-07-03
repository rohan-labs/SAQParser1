import os
import json
import streamlit as st
from openai import OpenAI
from supabase import create_client, Client
from io import StringIO, BytesIO
from tempfile import NamedTemporaryFile
import time
import PyPDF2
import docx2txt
from dotenv import load_dotenv
import base64
from PIL import Image
import uuid
import fitz
import zipfile
from xml.etree import ElementTree

# Client / credential set-up
load_dotenv()

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

client = OpenAI(api_key=openai_api_key)
supabase: Client = create_client(supabase_url, supabase_key)

# Helper Functions for Image Processing
def create_supabase_bucket_if_not_exists():
    """Ensure the mcq-images bucket exists in Supabase Storage"""
    try:
        supabase.storage.get_bucket('mcq-images')
    except:
        try:
            supabase.storage.create_bucket('mcq-images', {'public': True})
            st.info("Created mcq-images storage bucket")
        except Exception as e:
            st.warning(f"Could not create storage bucket: {e}")

def upload_image_to_supabase_storage(image_data, original_filename, scenario_index=0):
    """Upload image to Supabase Storage and return public URL"""
    try:
        create_supabase_bucket_if_not_exists()
        
        # Generate unique filename with scenario context
        file_extension = original_filename.split('.')[-1] if '.' in original_filename else 'png'
        unique_filename = f"saq_scenario_{scenario_index}_{uuid.uuid4()}.{file_extension}"
        
        # Convert to bytes if it's a PIL Image
        if isinstance(image_data, Image.Image):
            img_byte_arr = BytesIO()
            image_data.save(img_byte_arr, format='PNG')
            image_data = img_byte_arr.getvalue()
        
        # Upload to Supabase Storage
        response = supabase.storage.from_("mcq-images").upload(
            path=unique_filename,
            file=image_data,
            file_options={"content-type": f"image/{file_extension}"}
        )
        
        # Get public URL
        public_url_response = supabase.storage.from_("mcq-images").get_public_url(unique_filename)
        public_url = public_url_response.get('publicUrl') if hasattr(public_url_response, 'get') else str(public_url_response)
        
        st.success(f"Uploaded SAQ image: {unique_filename}")
        return public_url
        
    except Exception as e:
        st.error(f"Error uploading image to Supabase Storage: {e}")
        return None

def extract_images_from_pdf_advanced(pdf_path):
    """Extract images from PDF with position information using PyMuPDF"""
    images = []
    try:
        doc = fitz.open(pdf_path)
        
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            
            # Get text blocks to understand content structure
            text_blocks = page.get_text("dict")
            
            # Get images on this page
            image_list = page.get_images(full=True)
            
            for img_index, img in enumerate(image_list):
                try:
                    xref = img[0]
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    image_ext = base_image["ext"]
                    
                    # Get image rectangle (position on page)
                    img_rect = page.get_image_rects(img)[0] if page.get_image_rects(img) else None
                    
                    # Convert to PIL Image for processing
                    pil_image = Image.open(BytesIO(image_bytes))
                    
                    image_info = {
                        'data': image_bytes,
                        'pil_image': pil_image,
                        'page': page_num + 1,
                        'index': img_index,
                        'filename': f'page_{page_num + 1}_img_{img_index}.{image_ext}',
                        'extension': image_ext,
                        'rect': img_rect,
                        'size': pil_image.size if pil_image else None
                    }
                    
                    images.append(image_info)
                    
                except Exception as img_error:
                    st.warning(f"Could not extract image {img_index} from page {page_num + 1}: {img_error}")
                    continue
        
        doc.close()
        return images
        
    except Exception as e:
        st.error(f"Error extracting images from PDF: {e}")
        return []

def extract_images_from_docx_advanced(docx_path):
    """Extract images from DOCX file with better handling"""
    images = []
    try:
        with zipfile.ZipFile(docx_path, 'r') as docx_zip:
            # Get all image files from the media folder
            image_files = [f for f in docx_zip.namelist() if f.startswith('word/media/') and any(f.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp'])]
            
            for i, img_file in enumerate(image_files):
                try:
                    img_data = docx_zip.read(img_file)
                    filename = img_file.split('/')[-1]
                    
                    # Convert to PIL Image
                    pil_image = Image.open(BytesIO(img_data))
                    
                    image_info = {
                        'data': img_data,
                        'pil_image': pil_image,
                        'index': i,
                        'filename': filename,
                        'extension': filename.split('.')[-1] if '.' in filename else 'png',
                        'size': pil_image.size
                    }
                    
                    images.append(image_info)
                    
                except Exception as img_error:
                    st.warning(f"Could not process image {img_file}: {img_error}")
                    continue
                    
        return images
        
    except Exception as e:
        st.error(f"Error extracting images from DOCX: {e}")
        return []

def match_images_to_scenarios(parsed_scenarios, extracted_images, file_name):
    """Match extracted images to parsed scenarios and upload to Supabase"""
    updated_scenarios = []
    
    for i, scenario in enumerate(parsed_scenarios):
        scenario_copy = scenario.copy()
        
        # Check if this scenario should have an image
        has_image = scenario.get('hasImage', False)
        image_position = scenario.get('imagePosition', i)  # Default to scenario index if not specified
        
        if has_image and image_position < len(extracted_images):
            try:
                # Get the corresponding image
                image_info = extracted_images[image_position]
                
                # Upload image to Supabase Storage
                image_url = upload_image_to_supabase_storage(
                    image_info['data'], 
                    image_info['filename'],
                    scenario_index=i
                )
                
                if image_url:
                    scenario_copy['image'] = image_url
                    st.success(f"Linked image to scenario {i + 1} from {file_name}")
                else:
                    st.warning(f"Failed to upload image for scenario {i + 1}")
                    
            except Exception as e:
                st.error(f"Error processing image for scenario {i + 1}: {e}")
        
        # Clean up processing fields
        scenario_copy.pop('hasImage', None)
        scenario_copy.pop('imagePosition', None)
        scenario_copy.pop('source_file', None)
        
        updated_scenarios.append(scenario_copy)
    
    return updated_scenarios

def process_file_with_enhanced_extraction(uploaded_file):
    """Process file and extract both text and images with better coordination"""
    file_name = uploaded_file.name
    text_content = ""
    extracted_images = []
    
    try:
        if uploaded_file.type == "application/pdf":
            # For PDFs
            with NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                temp_pdf.write(uploaded_file.read())
                temp_pdf.flush()
                
                # Extract text using PyPDF2
                reader = PyPDF2.PdfReader(temp_pdf.name)
                for page in reader.pages:
                    text_content += page.extract_text() + "\n"
                
                # Extract images using PyMuPDF (more advanced)
                extracted_images = extract_images_from_pdf_advanced(temp_pdf.name)
                
                os.unlink(temp_pdf.name)  # Clean up temp file
                
        elif uploaded_file.type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            # For DOCX files
            with NamedTemporaryFile(delete=False, suffix=".docx") as temp_docx:
                temp_docx.write(uploaded_file.read())
                temp_docx.flush()
                
                # Extract text
                text_content = docx2txt.process(temp_docx.name)
                
                # Extract images
                extracted_images = extract_images_from_docx_advanced(temp_docx.name)
                
                os.unlink(temp_docx.name)  # Clean up temp file
                
        elif uploaded_file.type == "text/plain":
            # For TXT files (no images)
            stringio = StringIO(uploaded_file.getvalue().decode("utf-8"))
            text_content = stringio.read()
            
        else:
            st.error(f"Unsupported file type: {uploaded_file.type}")
            return None, None
            
    except Exception as e:
        st.error(f"Error processing {file_name}: {e}")
        return None, None
    
    return text_content, extracted_images

def upsert_saq_data_to_supabase(parsed_data):
    """Insert SAQ data to both parent and child tables with duplicate checking"""
    try:
        upload_summary = {
            'parent_success': 0,
            'parent_errors': 0,
            'child_success': 0,
            'child_errors': 0,
            'total_scenarios': len(parsed_data)
        }
        
        progress_bar = st.progress(0)
        
        for i, scenario_data in enumerate(parsed_data):
            try:
                # Prepare parent data - ONLY the fields we want, NO ID
                parent_data = {
                    'parentQuestion': str(scenario_data['parentQuestion']).strip(),
                    'moduleId': int(scenario_data['moduleId'])
                }
                
                # Add image if present
                if scenario_data.get('image'):
                    parent_data['image'] = str(scenario_data['image']).strip()
                
                st.info(f"🔍 Checking for existing parent scenario...")
                
                # Check if parent already exists - exact match on parentQuestion
                existing_parent = supabase.table("saqParent").select("id").eq(
                    'parentQuestion', scenario_data['parentQuestion'].strip()
                ).execute()
                
                if existing_parent.data and len(existing_parent.data) > 0:
                    # Use existing parent
                    parent_id = existing_parent.data[0]['id']
                    st.info(f"📋 Using existing parent scenario ID: {parent_id}")
                    upload_summary['parent_success'] += 1
                else:
                    # Insert new parent record - let database auto-generate ID
                    st.info(f"🔄 Inserting new parent record...")
                    
                    parent_response = supabase.table("saqParent").insert(parent_data).execute()
                    
                    # Check for errors
                    if hasattr(parent_response, 'error') and parent_response.error:
                        st.error(f"❌ Parent table error for scenario {i + 1}: {parent_response.error}")
                        upload_summary['parent_errors'] += 1
                        continue
                    
                    # Get the generated ID
                    if parent_response.data and len(parent_response.data) > 0:
                        parent_id = parent_response.data[0]['id']
                        upload_summary['parent_success'] += 1
                        st.success(f"✅ Created new parent scenario with ID: {parent_id}")
                    else:
                        st.error(f"❌ Could not retrieve parent ID for scenario {i + 1}")
                        st.error(f"Response: {parent_response}")
                        upload_summary['parent_errors'] += 1
                        continue
                
                # Process child questions
                child_questions = scenario_data.get('childQuestions', [])
                st.info(f"📝 Processing {len(child_questions)} child questions for parent {parent_id}")
                
                for j, child_question in enumerate(child_questions):
                    try:
                        # Prepare child data - ONLY the fields we want, NO ID
                        child_data = {
                            'questionLead': str(child_question['questionLead']).strip(),
                            'idealAnswer': str(child_question['idealAnswer']).strip(),
                            'parentQuestionId': int(parent_id),
                            'keyConcept': str(child_question['keyConcept']).strip()
                        }
                        
                        # Check if child question already exists
                        existing_child = supabase.table("saqChild").select("id").eq(
                            'questionLead', child_question['questionLead'].strip()
                        ).eq('parentQuestionId', parent_id).execute()
                        
                        if existing_child.data and len(existing_child.data) > 0:
                            st.info(f"📝 Child question {j + 1} already exists, skipping...")
                            upload_summary['child_success'] += 1
                        else:
                            # Insert child record - let database auto-generate ID
                            child_response = supabase.table("saqChild").insert(child_data).execute()
                            
                            if hasattr(child_response, 'error') and child_response.error:
                                st.error(f"❌ Child table error for scenario {i + 1}, question {j + 1}: {child_response.error}")
                                upload_summary['child_errors'] += 1
                            else:
                                child_id = child_response.data[0]['id'] if child_response.data else 'unknown'
                                upload_summary['child_success'] += 1
                                st.success(f"✅ Created child question {j + 1} with ID: {child_id}")
                                
                    except Exception as child_e:
                        st.error(f"❌ Child question {j + 1} upload exception: {child_e}")
                        upload_summary['child_errors'] += 1
                
            except Exception as parent_e:
                st.error(f"❌ Parent scenario {i + 1} upload exception: {parent_e}")
                upload_summary['parent_errors'] += 1
            
            # Update progress
            progress_bar.progress((i + 1) / len(parsed_data))
        
        progress_bar.empty()
        return upload_summary
        
    except Exception as e:
        st.error(f"❌ General upload error: {e}")
        return None

# Main File Processing Section
st.title("📋 SAQ Parser with Automatic Image Extraction")

st.write("""
This app processes PDF, DOCX, or TXT files containing SAQ (Short Answer Questions) scenarios with embedded images.
It automatically:
1. Extracts text content and parses scenarios with their questions
2. Extracts embedded images from the files
3. Matches images to their corresponding scenarios
4. Uploads images to Supabase Storage and stores data in both parent and child tables
""")

uploaded_files = st.file_uploader(
    "Upload PDF, DOCX, or TXT files with embedded images", 
    type=["pdf", "docx", "txt"], 
    accept_multiple_files=True
)

if uploaded_files:
    data_list = []
    any_errors = False

    for uploaded_file in uploaded_files:
        file_name = uploaded_file.name
        st.write(f"🔄 Processing **{file_name}**...")

        # Process file and extract text and images
        text_content, extracted_images = process_file_with_enhanced_extraction(uploaded_file)
        
        if text_content is None:
            any_errors = True
            continue
        
        if extracted_images:
            st.success(f"Extracted {len(extracted_images)} images from **{file_name}**")
            
            # Display extracted images in an expandable section
            with st.expander(f"Preview images from {file_name}"):
                cols = st.columns(min(3, len(extracted_images)))
                for idx, img in enumerate(extracted_images):
                    with cols[idx % 3]:
                        if img.get('pil_image'):
                            st.image(img['pil_image'], caption=f"Image {idx + 1}: {img['filename']}", width=200)
                        st.caption(f"Size: {img.get('size', 'Unknown')}")
        else:
            st.info(f"ℹ️ No images found in **{file_name}**")

        # Use OpenAI API to parse the content with enhanced image awareness
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                # Create enhanced prompt for SAQ parsing
                image_context = f"\n\nIMPORTANT: This document contains {len(extracted_images)} extracted images. " if extracted_images else "\n\nNote: No images were found in this document. "
                
                prompt = f"""
                You will be provided with SAQ (Short Answer Questions) data. You must output them in a JSON format representing clinical scenarios with their associated questions.
                
                Structure your response as an array of scenario objects, each with the following keys:
                
                - parentQuestion (string): The main clinical scenario/case description
                - moduleId (integer): The module/category ID for this scenario
                - hasImage (boolean): True if this scenario has an associated image
                - imagePosition (integer): If hasImage is true, indicate which image corresponds to this scenario (starting from 0)
                - childQuestions (array): Array of individual questions for this scenario, each containing:
                  - questionLead (string): The specific question being asked
                  - idealAnswer (string): The expected/ideal answer
                  - keyConcept (string): The main concept being tested
                
                {image_context}
                When parsing scenarios, look for any references to images, figures, diagrams, ECGs, X-rays, or visual elements.
                If you detect that a scenario refers to or requires an image (like "based on the ECG above", "the X-ray shows", "refer to the image", etc.), set hasImage to true.
                For imagePosition, use the order in which images appear in the document (0 for first image, 1 for second, etc.).
                
                You will categorise each scenario via the module they come under (the ID number for each scenario will be provided).
                
                Example output format:
                [
                  {{
                    "parentQuestion": "A 65-year-old man with a history of diabetes mellitus presents to the emergency department with crushing chest pain that started 2 hours ago. The pain radiates to his left arm and jaw. He appears diaphoretic and anxious. His blood pressure is 90/60 mmHg, heart rate is 110 bpm, and oxygen saturation is 94% on room air. An ECG shows ST-elevation in leads II, III, and aVF.",
                    "moduleId": 2,
                    "hasImage": true,
                    "imagePosition": 0,
                    "childQuestions": [
                      {{
                        "questionLead": "What is the most likely diagnosis based on the clinical presentation and ECG findings?",
                        "idealAnswer": "Inferior ST-elevation myocardial infarction (STEMI). The patient presents with typical chest pain, ECG changes showing ST-elevation in the inferior leads (II, III, aVF), and hemodynamic compromise.",
                        "keyConcept": "STEMI diagnosis and ECG interpretation"
                      }},
                      {{
                        "questionLead": "What immediate management steps should be taken?",
                        "idealAnswer": "1. Activate cardiac catheterization lab for primary PCI, 2. Administer dual antiplatelet therapy (aspirin + P2Y12 inhibitor), 3. Anticoagulation with heparin, 4. Oxygen if SpO2 <90%, 5. IV access and continuous monitoring, 6. Pain relief with morphine if needed.",
                        "keyConcept": "STEMI emergency management"
                      }},
                      {{
                        "questionLead": "Which coronary artery is most likely occluded based on the ECG pattern?",
                        "idealAnswer": "Right coronary artery (RCA). Inferior STEMI with ST-elevation in leads II, III, and aVF typically indicates RCA occlusion, as the RCA usually supplies the inferior wall of the left ventricle.",
                        "keyConcept": "Coronary anatomy and ECG correlation"
                      }}
                    ]
                  }}
                ]
                
                CRITICAL INSTRUCTIONS:
                - YOU MUST parse ALL scenarios in the text, not just the first one
                - Each scenario should be a complete clinical case with multiple related questions
                - INCLUDE ALL answer details - never summarize
                - RETAIN EVERY WORD from the ideal answers in the document
                - Make sure moduleId is always an integer
                - Pay attention to any image references in the text and set hasImage/imagePosition accordingly
                - Group related questions under the same parent scenario
                
                Text to parse:
                {text_content}
                """

                response = client.chat.completions.create(
                    model="gpt-4.1", 
                    messages=[
                        {"role": "system", "content": "You are a precise JSON parser that extracts SAQ data while preserving all content and identifying image associations. You structure clinical scenarios with their associated questions."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0,
                    max_tokens=None 
                )
                
                # Parse the JSON output from OpenAI
                json_response = response.choices[0].message.content.strip()
                json_response = json_response.replace("```json", "").replace("```", "").strip()
                
                # Parse JSON and prepare for image matching
                parsed_data = json.loads(json_response)
                
                # Ensure parsed_data is a list
                if isinstance(parsed_data, dict):
                    parsed_data = [parsed_data]
                
                # Add source file info
                for scenario in parsed_data:
                    scenario['source_file'] = file_name
                
                # Match images to scenarios and upload them
                if extracted_images:
                    st.info(f"🔗 Matching {len(extracted_images)} images to {len(parsed_data)} scenarios...")
                    final_scenarios = match_images_to_scenarios(parsed_data, extracted_images, file_name)
                else:
                    # No images to process, just clean up fields
                    final_scenarios = []
                    for scenario in parsed_data:
                        scenario.pop('hasImage', None)
                        scenario.pop('imagePosition', None) 
                        scenario.pop('source_file', None)
                        final_scenarios.append(scenario)
                
                data_list.extend(final_scenarios)
                
                # Count total child questions
                total_child_questions = sum(len(scenario.get('childQuestions', [])) for scenario in final_scenarios)
                st.success(f"Successfully processed **{file_name}** with {len(final_scenarios)} scenarios and {total_child_questions} questions")
                break
            
            except json.JSONDecodeError as json_error:
                if attempt < max_retries - 1:
                    st.warning(f"JSON parsing error for {file_name}. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                else:
                    st.error(f"Failed to parse JSON for {file_name} after {max_retries} attempts")
                    st.error(f"JSON Error: {json_error}")
                    with st.expander("View raw response"):
                        st.text(json_response)
                    any_errors = True
            
            except Exception as e:
                st.error(f"Error processing {file_name}: {e}")
                any_errors = True
                break

    # Display results and upload option
    if data_list:
        st.write("### 📊 Parsed SAQ Data Preview:")
        
        # Show summary
        total_scenarios = len(data_list)
        scenarios_with_images = len([s for s in data_list if s.get('image')])
        total_child_questions = sum(len(scenario.get('childQuestions', [])) for scenario in data_list)
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Scenarios", total_scenarios)
        with col2:
            st.metric("Scenarios with Images", scenarios_with_images)
        with col3:
            st.metric("Total Questions", total_child_questions)
        with col4:
            st.metric("Avg Questions/Scenario", f"{total_child_questions/total_scenarios:.1f}" if total_scenarios > 0 else "0")
        
        # Show expandable JSON preview
        with st.expander("📋 View Full JSON Data"):
            st.json(data_list)

        # Upload confirmation
        if st.button("🚀 Upload All Data to Supabase (Parent & Child Tables)", type="primary"):
            st.write("📤 Uploading scenarios and questions to Supabase...")
            
            upload_result = upsert_saq_data_to_supabase(data_list)
            
            if upload_result:
                st.write("### Upload Summary:")
                
                col1, col2 = st.columns(2)
                with col1:
                    st.metric("Parent Records Success", upload_result['parent_success'])
                    st.metric("Parent Records Errors", upload_result['parent_errors'])
                
                with col2:
                    st.metric("Child Records Success", upload_result['child_success'])
                    st.metric("Child Records Errors", upload_result['child_errors'])
                
                if upload_result['parent_errors'] == 0 and upload_result['child_errors'] == 0:
                    st.success("🎉 All data uploaded successfully to both tables!")
                    st.balloons()
                elif upload_result['parent_success'] > 0 or upload_result['child_success'] > 0:
                    st.warning("⚠️ Partial upload completed. Check error messages above.")
                else:
                    st.error("❌ Upload failed. Check error messages above.")
            else:
                st.error("❌ Upload process failed completely.")
                
    else:
        if any_errors:
            st.error("No data to upload due to processing errors.")
        else:
            st.warning("No SAQ scenarios were extracted from the uploaded files.")
            
else:
    st.info("Please upload PDF, DOCX, or TXT files to get started.")
