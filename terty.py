import os
import time
import json
import traceback
from urllib.parse import urlparse
import httpx

import asyncio, platform
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict

from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright
import google.generativeai as genai

from dotenv import load_dotenv
load_dotenv()

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
import platform


# os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/render/project/.playwright"

# ---------------------------------------------------------------------------
# ENVIRONMENT VARIABLES (NO HARDCODED SECRETS)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
STUDENT_EMAIL = os.getenv("STUDENT_EMAIL")
STUDENT_SECRET = os.getenv("STUDENT_SECRET")


print("KEY:", GEMINI_API_KEY)
print("EMAIL:", STUDENT_EMAIL)
print("SECRET:", STUDENT_SECRET)

if not GEMINI_API_KEY or not STUDENT_EMAIL or not STUDENT_SECRET:
    raise RuntimeError("Environment variables not set. Check GEMINI_API_KEY, STUDENT_EMAIL, STUDENT_SECRET.")

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
# JS RENDERED PAGE SCRAPER (Playwright)
# ---------------------------------------------------------------------------



async def fetch_quiz_page(url: str) -> str:
    print(f"🌐 Fetching quiz page: {url}")

    # ✅ Windows cannot run Playwright reliably with FastAPI background tasks
    if platform.system() == "Windows":
        print("🪟 Windows detected → using requests instead of Playwright")
        # resp = requests.get(url, timeout=30)
        # resp.raise_for_status()
        # return resp.text
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=30)
            print(f"✅ Page fetched via requests ({len(resp.text)} chars)")
            return resp.text

    # ✅ Linux server (deployment) → use Playwright
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
        print("⚠️ Playwright failed — falling back to requests:", e)
        # Use sync request as a fallback (keep it simple) but prefer httpx
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        print(f"✅ Fallback worked ({len(resp.text)} chars)")
        return resp.text
    


# ---------------------------------------------------------------------------
# EXTRACT SUBMISSION URL + QUESTION USING LLM (safer approach)
# ---------------------------------------------------------------------------
def parse_quiz_with_llm(html_content: str,page_url:str) -> dict:
    prompt = f"""
You are an expert quiz parser. Your job is to extract structured information from HTML.

RULES:
- DO NOT rewrite, summarize, or generalize the question.
- DO NOT hallucinate — extract only what appears in the HTML.
- DO NOT include placeholder HTML such as <span class="origin"></span>
- REMOVE all HTML tags.
- KEEP the exact phrasing of the quiz's question.
- Extract ALL URLs (file URLs, API endpoints, submit URLs)
- Return ONLY valid JSON, no markdown or extra text
- If a URL contains <span class="origin"></span>, [origin], or similar placeholder,
  REPLACE it with the origin of this page: {page_url}
  (origin = scheme + "://" + domain of the page URL)
- submit_url and data_sources urls MUST be a fully-qualified URL starting with http or https.
- If any URL contains "$EMAIL", REPLACE it with the student's actual email: {STUDENT_EMAIL}
- data_sources MUST NOT include the submit_url.

Extract:
1. question: The exact quiz question text
2. submit_url: The URL where answer must be POSTed
3. data_sources: List of any file URLs, API endpoints, or data URLs mentioned dont include the submit_url in this list.

Return JSON:
{{
  "question": "...",
  "submit_url": "...",
  "data_sources": ["url1", "url2"]
}}

HTML Content:
{html_content}
"""

    response = llm_model.generate_content(prompt)
    raw = response.text.strip()

    try:
        parsed = json.loads(raw)
        print('✅ Parsed quiz metadata:', parsed)
        return parsed
    except:
        # fallback: extract JSON inside text
        try:
            obj = json.loads(raw[raw.index("{"): raw.rindex("}")+1])
            return obj
        except:
            raise RuntimeError("Failed to parse quiz metadata with LLM")


 
async def solve_quiz_with_llm(question: str, html_content: str, data_sources: list) -> str:
    """
    This is a placeholder. Real quizzes vary widely, so this function
    attempts to solve common patterns (sum tables, read files, parse PDFs, etc.)
    """

    # Example fallback: If no specific instructions, answer "OK"

    """
    Use LLM to actually solve the quiz
    """
    print(f"\n🧠 Solving quiz with Gemini...")

    fetched_data = ""
    
    if data_sources:
        print(f"\n   📂 Fetching data from {len(data_sources)} source(s)...")
        
        for source in data_sources:
            # print(f"   📥 Processing source: {source}")
            if not source:
                continue
            
            # Only fetch URLs (skip empty or invalid sources)
            if source.startswith("http"):
                data = await fetch_quiz_page(source)
                # print('   ✅ Fetched data from source',data)
                fetched_data += f"\n\n--- Data from {source} ---\n{data}\n"
            else:
                print(f"   ⚠️  Skipping non-URL source: {source}")
    else:
        print(f"   📂 No external data sources to fetch")
    print("fetched_data:", fetched_data)
    prompt = f"""
            You are an expert problem solver. Your job is to solve this quiz question.

            QUESTION:
            {question}

            EXTERNAL DATA (fetched from provided URLs):
            {fetched_data if fetched_data else "No external data provided"}

            INSTRUCTIONS:
            1. Read the question carefully
            2. If external data is provided above, ANALYZE IT to find the answer else USE ONLY the question to give the answer
            3. Look for numbers, tables, lists, or information needed to answer the question
            4. Calculate or deduce the correct answer based on the data
            5. Return ONLY the final answer value - nothing else
            6. NO explanations, NO working, NO extra text
            7. If answer is a number: return just the number (e.g., 12345)
            8. If answer is text: return the text exactly (e.g., "hello world")
            9. If answer is boolean: return true or false
            10. If answer is multiple values: return as JSON array (e.g., [1, 2, 3])

            ANSWER:
      
            """
    try:
        # Step 3: Send to Gemini for solving
        print(f"\n   🤖 Sending to Gemini...")
        response = llm_model.generate_content(prompt)
        answer = response.text.strip()
        
        print(f"   ✅ Gemini answer: {answer}")
        return answer
        
    except Exception as e:
        print(f"   ❌ Error solving with Gemini: {str(e)}")
        return "ERROR"



def  fetch_data_from_sources(data_sources: list) -> dict:
    """
    Fetch data from provided URLs/APIs
    """
    data = {}
    
    for source in data_sources:
        if not source:
            continue
            
        print(f"📥 Fetching data from: {source}")
        
        try:
            # Try to fetch from URL
            if source.startswith("http"):
                response = httpx.get(source, timeout=10)
                
                if source.endswith(".pdf"):
                    print(f"   📄 PDF file fetched")
                    data[source] = f"PDF file ({len(response.content)} bytes)"
                elif source.endswith(".csv"):
                    print(f"   📊 CSV file fetched")
                    data[source] = response.text
                elif source.endswith(".json"):
                    print(f"   📋 JSON file fetched")
                    data[source] = response.json()
                else:
                    print(f"   📰 Content fetched ({len(response.text)} chars)")
                    data[source] = response.text[:1000]  # First 1000 chars
                    
            else:
                data[source] = "Not a URL"
                
        except Exception as e:
            print(f"   ❌ Error fetching: {str(e)}")
            data[source] = f"Error: {str(e)}"
    
    return data
# ---------------------------------------------------------------------------
# SUBMIT ANSWER
# ---------------------------------------------------------------------------
async def submit_answer(submit_url: str, quiz_url: str, answer):
    payload = {
        "email": STUDENT_EMAIL,
        "secret": STUDENT_SECRET,
        "url": quiz_url,
        "answer": answer
    }

    async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(submit_url, json=payload)
            return resp.json()

def format_url(url_string: str, base_url: str) -> str:
    """Replace {origin} placeholder ONLY if it exists"""
    if "{origin}" not in url_string:
        return url_string
    
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return url_string.replace("{origin}", origin)

def get_origin(url):
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"
# ---------------------------------------------------------------------------
# MAIN QUIZ WORKER (runs in background)
# ---------------------------------------------------------------------------
async def solve_quiz_chain(initial_url: str):
    start_time = time.time()
    current_url = initial_url

    print("\n🧵 Worker started solving chain...\n")

    while True:
        if time.time() - start_time > 170:
            print("⏳ TIMEOUT: 3-minute limit exceeded.")
            return

        try:
            html = await fetch_quiz_page(current_url)
            print("🟦 Fetched quiz page, parsing...",html)
            origin = get_origin(current_url)
            parsed = parse_quiz_with_llm(html,origin)
            print("🟦 Parsed quiz, solving...", parsed)
            question = parsed.get("question", "")
            submit_url = parsed.get("submit_url", "")
            data_sources = parsed.get("data_sources", [])
            # return
            # submit_url = format_url(submit_url, current_url)
            # data_sources = [format_url(src, current_url) for src in data_sources]
            # print("🟦 Solving quiz question...",submit_url,data_sources)

            fetched_data = {}
            if data_sources:
                print(f"   [3/5] Fetching data from {len(data_sources)} source(s)...")
                fetched_data = fetch_data_from_sources(data_sources)
                # print("   [6/5] Fetched data:", fetched_data)
            else:
                print("   [3/5] No external data sources")
            
            # Step 4: Solve quiz
            print("   [4/5] Solving quiz with Gemini...")
            answer = await solve_quiz_with_llm(question, html, data_sources)
            print("   [5/5] Submitting answer...")
            print("🟦 Submitting answer:", answer)
            print("🟦 Submit URL:", submit_url)
            print("🟦 Current URL:", current_url)
            response =await submit_answer(submit_url, current_url, answer)

            


            print("🟦 Server Response:", response)

            if response.get("correct") is True:
                next_url = response.get("url")
                if not next_url:
                    print("🏁 Quiz chain finished.")
                    return
                print(f"➡️ Next quiz URL: {next_url}")
                current_url = next_url
                continue

            else:
                # retry wrong attempt
                print("🔁 Retrying wrong attempt...")
                continue

        except Exception as e:
            print("❌ Worker error:", traceback.format_exc())
            return


# ---------------------------------------------------------------------------
# API ENDPOINT — RETURNS 200 IMMEDIATELY (RULE REQUIREMENT)
# ---------------------------------------------------------------------------
@app.post("/")
async def handle_quiz(task: QuizRequest, bg: BackgroundTasks):

    print(f"\n📩 Incoming request: {task.url}")

    # Validate secret
    if task.secret != STUDENT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    # Start background solving
    bg.add_task(solve_quiz_chain, task.url)

    # Immediate response to grader (very important)
    return {"status": "accepted", "message": "Quiz solving started"}


# ---------------------------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------------------------
@app.get("/")
def home():
    return {"status": "Server is running"}
