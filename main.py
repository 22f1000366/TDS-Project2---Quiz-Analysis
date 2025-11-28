import os
import time
import json
import traceback
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup
import asyncio, platform

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict
from playwright.async_api import async_playwright
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
STUDENT_EMAIL = os.getenv("STUDENT_EMAIL")
STUDENT_SECRET = os.getenv("STUDENT_SECRET")

print("KEY:", GEMINI_API_KEY[:20] + "..." if GEMINI_API_KEY else None)
print("EMAIL:", STUDENT_EMAIL)
print("SECRET:", STUDENT_SECRET)

if not GEMINI_API_KEY or not STUDENT_EMAIL or not STUDENT_SECRET:
    raise RuntimeError("Environment variables not set.")

genai.configure(api_key=GEMINI_API_KEY)
llm_model = genai.GenerativeModel("gemini-2.5-flash")

# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------
app = FastAPI()

class QuizRequest(BaseModel):
    email: str
    secret: str
    url: str
    model_config = ConfigDict(extra="ignore")

# ---------------------------------------------------------------------------
# IMPROVED FETCH WITH BETTER CONTENT EXTRACTION
# ---------------------------------------------------------------------------
async def fetch_quiz_page(url: str) -> str:
    """Fetch page with better error handling"""
    print(f"🌐 Fetching quiz page: {url}")

    if platform.system() == "Windows":
        print("🪟 Windows detected → using httpx")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=headers, timeout=30)
                print(f"✅ Page fetched via httpx ({len(resp.text)} chars)")
                return resp.text
            except Exception as e:
                print(f"❌ httpx failed: {str(e)}")
                raise

    # Linux - try Playwright first
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=60000)
            await page.wait_for_load_state("networkidle")
            content = await page.content()
            print(f"✅ Page fetched via Playwright ({len(content)} chars)")
            await browser.close()
            return content
    except Exception as e:
        print(f"⚠️ Playwright failed: {e}, falling back to httpx")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = httpx.get(url, headers=headers, timeout=30)
        return resp.text

# ---------------------------------------------------------------------------
# IMPROVED HTML PARSING - EXTRACT ALL TEXT CONTENT
# ---------------------------------------------------------------------------
def extract_all_text_from_html(html: str) -> str:
    """
    Extract ALL visible text from HTML, including:
    - Text in spans, divs, paragraphs
    - Audio file names and descriptions
    - CSV file links
    - All structured content
    """
    print("\n📝 Extracting all text from HTML...")
    
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove script and style elements
    for script in soup(["script", "style"]):
        script.decompose()
    
    # Get all text
    text = soup.get_text(separator="\n", strip=True)
    
    print(f"   ✅ Extracted {len(text)} chars of text")
    return text

# ---------------------------------------------------------------------------
# IMPROVED PARSING WITH BETTER CONTEXT
# ---------------------------------------------------------------------------
def parse_quiz_with_llm(html_content: str, page_url: str,curr_page_url:str) -> dict:
    """
    Enhanced parser that:
    1. Extracts ALL text content (not just question div)
    2. Includes audio/file descriptions
    3. Finds all URLs and data sources
    """
    
    print("\n📄 Parsing quiz with enhanced extraction...")
    
    # Extract all text from HTML
    all_text = extract_all_text_from_html(html_content)
    
    # Also get raw HTML for reference
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Find all audio, files, linhks
    audio_files = []
    data_files = []
    
    for audio in soup.find_all('audio'):
        src = audio.get('src')
        if src:
            audio_files.append(src)
            print(f"   🎵 Found audio: {src}")
    
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('http') or href.endswith(('.csv', '.pdf', '.json')):
            data_files.append(href)
            print(f"   📄 Found file: {href}")
    
    # Build prompt with ALL available information
    prompt = f"""
You are an expert quiz parser. Extract structured information from the complete quiz page content.

COMPLETE PAGE CONTENT:
{all_text}

AUDIO FILES DETECTED:
{chr(10).join(audio_files) if audio_files else "None"}

DATA FILES DETECTED:
{chr(10).join(data_files) if data_files else "None"}

RAW HTML (for reference):
{html_content}

EXTRACTION RULES:
- Extract the ACTUAL quiz question (even if it's split across multiple spans/elements)
- Include context from audio descriptions or file names if they are part of the question
- Find the submit URL (look for "POST to", "submit to", or similar)
- Extract ALL data source URLs (audio files, CSV files, API endpoints, etc.) take full URLs only
- Replace {{origin}} with: {page_url.split('/')[0]}//{page_url.split('/')[2]}
- Replace $EMAIL with: {STUDENT_EMAIL}
- submit_url MUST be a fully-qualified URL (http/https).
- data_sources MUST NOT include submit_url.
❗ IMPORTANT RULE ABOUT EXAMPLE JSON:
Many quiz pages include an example like:
  {{
    "email": "your email",
    "secret": "your secret",
    "url": "https://some-link-or-text",
    "answer": ...
  }}
This block is ONLY an example to show the response format:
- Do NOT include the "url" from this example in data_sources.
- BUT extract this example "url" separately as answer_url_json.
- If the example says: "url": "this page's URL"
  → replace it with the actual current page URL: {curr_page_url}
- If the example contains a full URL starting with http/https
  → return that exact URL as answer_url_json.

Return ONLY valid JSON (no markdown):
{{
  "question": "The complete quiz question text (including any audio/file context)",
  "submit_url": "https://...",
  "data_sources": ["url1", "url2"],
  "answer_url_json": "https://... or {curr_page_url}",
  "question_type": "text/audio/mixed"
}}


"""

    try:
        response = llm_model.generate_content(prompt)
        raw = response.text.strip()
        parsed = json.loads(raw)
        return parsed
    except json.JSONDecodeError:
        try:
            obj = json.loads(raw[raw.index("{"): raw.rindex("}")+1])
            return obj
        except:
            raise RuntimeError(f"Failed to parse: {raw[:200]}")

# ---------------------------------------------------------------------------
# ENHANCED SOLVING - INCLUDES AUDIO CONTEXT
# ---------------------------------------------------------------------------
async def transcribe_audio(audio_url: str) -> str:
    """
    Transcribe audio file using Gemini's vision API
    Supports: .mp3, .opus, .wav, .flac, .ogg, .m4a
    """
    print(f"\n   🎵 Transcribing audio: {audio_url[:60]}...")
    
    try:
        async with httpx.AsyncClient() as client:
            # Download audio file
            resp = await client.get(audio_url, timeout=30)
            audio_bytes = resp.content
            
            print(f"       ✅ Downloaded audio ({len(audio_bytes)} bytes)")
            
            # Get file extension to determine MIME type
            ext = audio_url.split('.')[-1].lower()
            mime_types = {
                'mp3': 'audio/mpeg',
                'opus': 'audio/opus',
                'wav': 'audio/wav',
                'flac': 'audio/flac',
                'ogg': 'audio/ogg',
                'm4a': 'audio/mp4'
            }
            mime_type = mime_types.get(ext, 'audio/mpeg')
            
            # Upload to Gemini Files API for processing
            import base64
            audio_base64 = base64.standard_b64encode(audio_bytes).decode()
            
            # Create a prompt to transcribe the audio
            prompt = """
Please transcribe the audio content. Return ONLY the transcribed text, word for word.
Do not add any explanations or summaries - just the exact words spoken in the audio.
"""
            
            # Use Gemini to process audio
            message = {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": audio_base64
                        }
                    },
                    {
                        "text": prompt
                    }
                ]
            }
            
            response = llm_model.generate_content(message.get("parts", []) if isinstance(message, dict) else prompt)
            transcription = response.text.strip()
            
            print(f"       ✅ Transcribed: {transcription}...")
            return transcription
            
    except Exception as e:
        print(f"       ⚠️  Transcription failed: {str(e)}")
        return f"[Audio file could not be transcribed: {str(e)}]"


async def fetch_data_source(source: str) -> tuple[str, str]:
    """
    Intelligently fetch any type of data source:
    - Regular webpages (HTML) → Use Playwright (handles JavaScript)
    - Audio files (.mp3, .opus, etc.) → Transcribe with Gemini
    - CSV files → Fetch and return
    - JSON files → Fetch and return
    - PDF files → Fetch metadata
    
    Returns: (file_type, content)
    """
    
    if not source or not source.startswith("http"):
        return "unknown", f"Invalid source: {source}"
    
    print(f"\n   📥 Fetching: {source[:70]}...")
    
    try:
        # 1. Check if it's an audio file
        if source.endswith(('.mp3', '.opus', '.wav', '.flac', '.ogg', '.m4a')):
            print(f"       🎵 Detected AUDIO file...")
            transcription = await transcribe_audio(source)
            return "AUDIO (Transcribed)", transcription
        
        # 2. Check if it's a data file (CSV, JSON, PDF)
        elif source.endswith(('.csv', '.json', '.pdf', '.txt')):
            file_ext = source.split('.')[-1].upper()
            # print(f"       📄 Detected {file_ext} file...")
            
            async with httpx.AsyncClient() as client:
                resp = await client.get(source, timeout=15)
                data = resp.text
                
                if file_ext == "PDF":
                    return f"{file_ext} (Preview)", data[:500]
                else:
                    return file_ext, data
        
        # 3. Otherwise, treat as a webpage (use Playwright for JS rendering)
        else:
            data = await fetch_quiz_page(source)
            all_text = extract_all_text_from_html(data)
           
            return "WEBPAGE (JavaScript Rendered)", all_text
               
    except Exception as e:
        print(f"       ❌ Error fetching: {str(e)}")
        return "ERROR", f"Failed to fetch: {str(e)}"


async def solve_quiz_with_llm(question: str, html_content: str, data_sources: list) -> str:
    """
    Enhanced solver that:
    1. Intelligently fetches ALL data source types
    2. Handles webpages with JavaScript
    3. Transcribes audio files
    4. Processes data files (CSV, JSON)
    5. Provides complete context to Gemini
    """
    
    print(f"\n🧠 Solving quiz with complete context...")

    fetched_data = ""
    
    if data_sources:
        print(f"\n   📂 Fetching {len(data_sources)} source(s)...")
        
        for  source in data_sources:
            try:
                # Intelligently fetch any data source type
                file_type, data = await fetch_data_source(source)
                # data = await fetch_quiz_page(source)
                # all_text = extract_all_text_from_html(data)
           
                # file_type="WEBPAGE (JavaScript Rendered)"
                fetched_data += f"\n\n--- {file_type} from {source} ---\n{data}\n"
                # print('fetched_data updated',fetched_data)
                    
            except Exception as e:
                print(f"       ⚠️  Failed: {str(e)}")
                fetched_data += f"\n--- ERROR fetching {source}: {str(e)} ---\n"
    else:
        print(f"   📂 No external data sources")

    # Build comprehensive prompt
    prompt = f"""
You are an expert problem solver. Solve this quiz using ALL available context.

QUESTION:
{question}

ADDITIONAL CONTEXT FROM PAGE:
{html_content[:1500]}

FETCHED DATA (CSV, Audio descriptions, Files, etc.):
{fetched_data if fetched_data else "No external data"}

YOUR TASK:
 DO NOT hallucinate — extract and do operations only that are asked and in case of any calculation in csv/excel file apply proper filters and calculate right answer.
1. Understand what the question is asking (including audio context if any)
2. Analyze ALL provided data
3. Calculate or deduce the CORRECT answer
4. Return ONLY the final answer value
5. If csv/excel data is provided, analyze it thoroughly to find the answer ,go through all rows and columns carefully and calculate answer correctly.

ANSWER FORMAT:
- If number: return just the number (e.g., 12345)
- If text: return the exact text (e.g., "hello world")
- If boolean: return true or false
- If multiple values: return as JSON array [1, 2, 3]
- NO explanations, NO working, NO extra text

FINAL ANSWER:
"""



    try:
        print(f"   🤖 Sending to Gemini...")
        response = llm_model.generate_content(prompt)
        answer = response.text.strip()
        
        print(f"   ✅ Gemini answer: {answer}")
        return answer
        
    except Exception as e:
        print(f"   ❌ Error: {str(e)}")
        return "ERROR"

# ---------------------------------------------------------------------------
# REST OF CODE UNCHANGED
# ---------------------------------------------------------------------------
async def submit_answer(submit_url: str, quiz_url: str, answer):
    payload = {
        "email": STUDENT_EMAIL,
        "secret": STUDENT_SECRET,
        "url": quiz_url,
        "answer": answer
    }
    async with httpx.AsyncClient(timeout=20) as client:
        print(f"   📨 Submitting to {submit_url} with payload: {payload}")
        resp = await client.post(submit_url, json=payload)
        return resp.json()

def format_url(url_string: str, base_url: str) -> str:
    if "{origin}" not in url_string:
        return url_string
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return url_string.replace("{origin}", origin)

def get_origin(url):
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"

async def solve_quiz_chain(initial_url: str):
    start_time = time.time()
    current_url = initial_url
    quiz_count = 0

    print("\n🧵 Worker started solving chain...\n")

    while quiz_count < 100:
        if time.time() - start_time > 170:
            print("⏳ TIMEOUT: 3-minute limit exceeded.")
            return

        quiz_count += 1
        print(f"\n📌 Quiz #{quiz_count}")

        try:
            
            # Fetch page
            html = await fetch_quiz_page(current_url)
            
            # Parse with improved extraction
            origin = get_origin(current_url)
            parsed = parse_quiz_with_llm(html, origin,current_url)
            
            question = parsed.get("question", "")
            submit_url = parsed.get("submit_url", "")
            data_sources = parsed.get("data_sources", [])
            answer_url_json=parsed.get("answer_url_json","")
            
            if not submit_url or not question:
                print("❌ Missing question or submit URL")
                return

            print(f"✅ Question: {parsed}...")
            print('parsed',parsed)
            
            
            # Solve
            answer = await solve_quiz_with_llm(question, html, data_sources)
            print(f"✅ Answer: {answer}")
            
            # Submit
            print("   [5/5] Submitting answer...")
            print("🟦 Submitting answer:", answer)
            print("🟦 Submit URL:", submit_url)
            print("🟦 Current URL:", current_url)
            response = await submit_answer(submit_url, answer_url_json, answer)

            print(f"✅ Submission response: {response}")
            if response.get("correct") is True:
                next_url = response.get("url")
                if not next_url:
                    print("🏁 Quiz chain finished!")
                    return
                current_url = next_url
                continue
            else:
                print("🔁 Retrying wrong attempt...")
                continue

        except Exception as e:
            print(f"❌ Error: {traceback.format_exc()}")
            return

@app.post("/")
async def handle_quiz(task: QuizRequest, bg: BackgroundTasks):
    print(f"\n📩 Incoming request: {task.url}")
    if task.secret != STUDENT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    bg.add_task(solve_quiz_chain, task.url)
    return {"status": "accepted", "message": "Quiz solving started"}

@app.get("/")
def home():
    return {"status": "Server is running"}