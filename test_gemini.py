import os
from google import genai

api_key = os.environ["GEMINI_API_KEY"]
model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

print("[TEST] Gemini 연결 테스트 시작")
print(f"[TEST] model={model}")

client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model=model,
    contents="테스트입니다. 정상 연결되었다면 'Gemini 정상'이라고만 답해주세요."
)

print("[TEST] Gemini 응답:")
print(response.text)
print("[TEST] Gemini 연결 정상")
