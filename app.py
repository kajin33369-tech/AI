"""평가보고서 점검 챗봇.

guidelines 폴더에 넣어둔 평가지침 PDF를 기준으로,
업로드한 평가보고서 PDF가 지침에 맞게 작성됐는지 Claude가 분석합니다.
"""

import base64
import os
from pathlib import Path

import anthropic
import streamlit as st
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
GUIDELINE_DIR = BASE_DIR / "guidelines"
MODEL = "claude-opus-5-5"
MAX_REQUEST_MB = 30  # Claude API 한 번 요청 한도(32MB)보다 조금 여유 있게

SYSTEM_PROMPT = """당신은 평가보고서 검토 전문가입니다.
'평가지침' 문서를 기준으로 '평가보고서'가 지침에 맞게 작성됐는지 꼼꼼히 점검합니다.

원칙:
- 판단은 반드시 제공된 두 문서의 내용에만 근거합니다. 추측하지 않습니다.
- 근거를 제시할 때는 지침과 보고서의 쪽수(p.)를 함께 적습니다.
- 지침이 모호해서 판단하기 어려운 항목은 '판단 보류'로 표시하고 이유를 씁니다.
- 한국어로, 실무자가 바로 고칠 수 있게 구체적으로 씁니다.

첫 분석 결과는 아래 형식을 따릅니다.

## 1. 종합 의견
지침 준수 수준을 3~5문장으로 요약합니다.

## 2. 지침 항목별 점검표
| 지침 항목 | 판정 | 근거 (지침 p. / 보고서 p.) | 개선 제안 |
판정은 ✅ 충족 / ⚠️ 일부 미흡 / ❌ 누락 / ❓ 판단 보류 중 하나로 씁니다.
지침의 요구사항을 빠짐없이 항목으로 나눠 모두 점검합니다.

## 3. 우선 수정이 필요한 사항
중요한 것부터 번호를 매겨 정리합니다.

이후 사용자의 추가 질문에는 같은 원칙으로 답합니다."""

ANALYSIS_REQUEST = "위 평가보고서가 평가지침에 맞게 작성됐는지 분석해 주세요."


def pdf_block(data: bytes, title: str) -> dict:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
        "title": title,
    }


@st.cache_data(show_spinner=False)
def load_guidelines() -> list[tuple[str, bytes]]:
    return [(p.name, p.read_bytes()) for p in sorted(GUIDELINE_DIR.glob("*.pdf"))]


def first_message(guidelines: list[tuple[str, bytes]], report_name: str, report: bytes) -> dict:
    content = [pdf_block(data, f"평가지침 - {name}") for name, data in guidelines]
    # 지침 부분까지 캐시해 두면, 다른 보고서를 분석할 때 비용과 시간이 줄어듭니다.
    content[-1]["cache_control"] = {"type": "ephemeral"}
    content.append(pdf_block(report, f"평가보고서 - {report_name}"))
    content.append({"type": "text", "text": ANALYSIS_REQUEST})
    return {"role": "user", "content": content}


def fail(messages: list) -> bool:
    """실패한 질문은 기록에서 지워서, 다시 시도할 수 있게 합니다."""
    messages.pop()
    st.session_state.display.pop()
    return False


def ask_claude(client: anthropic.Anthropic, messages: list) -> bool:
    """Claude 답변을 화면에 실시간으로 보여주고, 대화 기록에 저장합니다."""
    with st.chat_message("assistant"):
        try:
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=64000,
                system=SYSTEM_PROMPT,
                messages=messages,
                output_config={"effort": "high"},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                st.write_stream(stream.text_stream)
                final = stream.get_final_message()
        except anthropic.AuthenticationError:
            st.error("API 키가 올바르지 않아요. `.env` 파일의 키를 다시 확인해 주세요.")
            return fail(messages)
        except anthropic.RateLimitError:
            st.error("요청이 너무 많아요. 1분쯤 뒤에 다시 시도해 주세요.")
            return fail(messages)
        except anthropic.APIStatusError as e:
            if e.status_code == 413:
                st.error("파일이 너무 커요. PDF 용량을 줄여서 다시 올려 주세요.")
            elif "credit balance" in str(e.message).lower():
                st.error("API 크레딧이 부족해요. console.anthropic.com 의 Billing에서 충전해 주세요.")
            else:
                st.error(f"Claude 서버에서 오류가 났어요 ({e.status_code}): {e.message}")
            return fail(messages)
        except anthropic.APIConnectionError:
            st.error("인터넷 연결을 확인해 주세요.")
            return fail(messages)

        if final.stop_reason == "refusal":
            st.warning("Claude가 이 요청에 답하지 못했어요. 질문을 바꿔서 다시 시도해 주세요.")
            return fail(messages)
        if final.stop_reason == "max_tokens":
            st.warning("답변이 너무 길어서 중간에 끊겼어요. '이어서 써줘'라고 입력해 보세요.")

    messages.append({"role": "assistant", "content": final.content})
    text = "".join(b.text for b in final.content if b.type == "text")
    st.session_state.display.append(("assistant", text))
    return True


def main() -> None:
    st.set_page_config(page_title="평가보고서 점검 챗봇", page_icon="📋", layout="wide")
    st.title("📋 평가보고서 점검 챗봇")
    st.caption("평가지침에 맞게 보고서가 작성됐는지 AI가 항목별로 점검해 드려요.")

    load_dotenv(BASE_DIR / ".env")
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key or api_key.startswith("여기에"):
        st.error("API 키가 아직 설정되지 않았어요. `.env` 파일에 API 키를 넣고 다시 실행해 주세요.")
        st.stop()
    client = anthropic.Anthropic(api_key=api_key)

    GUIDELINE_DIR.mkdir(exist_ok=True)
    guidelines = load_guidelines()

    st.session_state.setdefault("messages", [])  # Claude에게 보내는 대화 기록
    st.session_state.setdefault("display", [])  # 화면에 보여줄 대화 기록

    with st.sidebar:
        st.header("📚 적용 중인 평가지침")
        if guidelines:
            for name, _ in guidelines:
                st.write(f"- {name}")
        else:
            st.warning("`guidelines` 폴더에 평가지침 PDF를 넣어 주세요.")
        if st.button("🔄 지침 다시 불러오기"):
            load_guidelines.clear()
            st.rerun()
        st.divider()
        if st.button("🗑️ 새 보고서로 다시 시작", type="primary"):
            st.session_state.messages = []
            st.session_state.display = []
            st.rerun()

    if not guidelines:
        st.info("먼저 `guidelines` 폴더에 평가지침 PDF를 넣고, 왼쪽의 '지침 다시 불러오기'를 눌러 주세요.")
        st.stop()

    if not st.session_state.messages:
        report = st.file_uploader("평가보고서 PDF를 올려 주세요", type=["pdf"])
        if report and st.button("🔍 분석 시작", type="primary"):
            data = report.getvalue()
            total_mb = (len(data) + sum(len(g) for _, g in guidelines)) * 4 / 3 / 1024 / 1024
            if total_mb > MAX_REQUEST_MB:
                st.error(f"지침과 보고서를 합친 용량이 너무 커요 (약 {total_mb:.0f}MB). PDF 용량을 줄여 주세요.")
                st.stop()
            st.session_state.display.append(("user", f"📎 **{report.name}** 분석 요청"))
            st.session_state.messages.append(first_message(guidelines, report.name, data))
            with st.chat_message("user"):
                st.markdown(st.session_state.display[-1][1])
            if ask_claude(client, st.session_state.messages):
                st.rerun()
        return

    for role, text in st.session_state.display:
        with st.chat_message(role):
            st.markdown(text)

    if question := st.chat_input("분석 결과에 대해 더 궁금한 점을 물어보세요"):
        with st.chat_message("user"):
            st.markdown(question)
        st.session_state.display.append(("user", question))
        st.session_state.messages.append({"role": "user", "content": question})
        ask_claude(client, st.session_state.messages)


if __name__ == "__main__":
    main()
