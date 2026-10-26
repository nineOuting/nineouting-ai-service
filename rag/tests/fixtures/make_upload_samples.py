"""업로드 문서 시험용 합성 샘플을 만든다. 내용은 모두 지어낸 것이다. 만든 파일은 저장소에 함께 올린다
(다른 환경은 이 스크립트 없이 시험만 돌리면 된다).

  .venv/bin/python rag/tests/fixtures/make_upload_samples.py [글꼴.otf]

필요: fpdf2·pypdf (rag/requirements-dev.txt), 한글 글꼴. 기본은 Noto Sans CJK KR (OFL 라이선스, PDF 에는 쓴 글자만 들어간다).
참고: fpdf2 가 이 글꼴로 만든 PDF 는 글자 폭 정보가 어긋나 pypdf 로 읽으면 글자마다 띄어진다 (pdftotext·PDFium 은 정상).
그래서 RAG 는 PDFium(pypdfium2)으로 읽고, 이 샘플은 "표준에서 조금 벗어난 PDF" 시험도 겸한다.
  upload_report.pdf   4쪽 보고서. 1쪽 제목·문단, 2쪽 예산 표, 3쪽 빈 쪽(머리글도 없음), 4쪽 긴 문단.
                      1·2·4쪽에 같은 머리글·바닥글(쪽 번호) → 정제 시험(SCRUM-110)에도 쓴다
  upload_scan.pdf     도형만 있고 글자는 없는 1쪽 (스캔한 PDF 대신)
  upload_locked.pdf   upload_report.pdf 에 암호를 건 것
"""
import sys
from pathlib import Path

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

HERE = Path(__file__).parent
FONT_CANDIDATES = [Path.home() / "Library/Fonts/NotoSansCJKkr-Regular.otf", Path("/Library/Fonts/NotoSansCJKkr-Regular.otf"),
                   Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
HEADER = "나인아웃팅 합성 자료 · 내부 검토용"

LONG = ("행사 당일 운영은 접수, 진행, 안전 세 조로 나눈다. 접수 조는 오전 9시까지 등록 데스크를 준비하고 "
        "참가자 명단을 출력해 둔다. 진행 조는 발표 순서와 시간을 확인하고 발표자에게 10분 전에 알린다. "
        "안전 조는 비상구 위치와 소화기 상태를 점검하고 결과를 운영 대화방에 남긴다. ")


class Report(FPDF):
    def __init__(self, font):
        super().__init__()
        self.add_font("noto", "", str(font))
        self.add_font("noto", "B", str(font))   # 표 머리 행은 굵게 그리므로 같은 글꼴을 굵게로도 등록
        self.set_font("noto", size=11)
        self.plain_pages = set()                # 머리글·바닥글을 그리지 않을 쪽 번호 (빈 쪽)

    def header(self):
        if self.page_no() not in self.plain_pages:
            self.set_font("noto", size=8)
            self.cell(0, 6, HEADER, align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_font("noto", size=11)

    def footer(self):
        if self.page_no() not in self.plain_pages:      # 바닥글은 다음 쪽을 넣을 때 그려지므로 쪽 번호로 판단
            self.set_y(-15)
            self.set_font("noto", size=8)
            self.cell(0, 6, f"- {self.page_no()} -", align="C")
            self.set_font("noto", size=11)

    def heading(self, text):
        self.set_font("noto", size=14)
        self.multi_cell(0, 9, text, new_x="LMARGIN", new_y="NEXT")
        self.set_font("noto", size=11)

    def para(self, text):
        self.multi_cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)


def make_report(font, path):
    pdf = Report(font)
    pdf.add_page()
    pdf.heading("2026년 4분기 고객 초청 행사 운영 계획 (합성 자료)")
    pdf.heading("1. 개요")
    pdf.para("고객 초청 행사는 11월 20일(목)부터 이틀 동안 강남 행사장에서 연다. 참가 예정 인원은 150명이다. "
             "행사 목적은 신규 기능 소개와 고객 의견 수집이다.")
    pdf.add_page()
    pdf.heading("2. 예산 계획")
    pdf.para("총 예산은 2,470만 원이며 항목별 금액은 아래 표와 같다.")
    with pdf.table(col_widths=(40, 40, 80)) as table:
        for row in (("항목", "금액", "비고"), ("마케팅", "12,000,000원", "온라인 광고 포함"),
                    ("장소 대관", "8,500,000원", "2일"), ("식음료", "4,200,000원", "참가자 150명 기준")):
            r = table.row()
            for cell in row:
                r.cell(cell)
    pdf.plain_pages.add(3)
    pdf.add_page()                               # 3쪽: 아무것도 없는 쪽
    pdf.add_page()
    pdf.heading("3. 운영 조와 할 일")
    pdf.para(LONG * 8)
    pdf.output(str(path))


def make_scan(path):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_fill_color(220, 220, 220)
    for i in range(6):                           # 글자 대신 회색 막대: 스캔 이미지처럼 글을 꺼낼 수 없는 쪽
        pdf.rect(20, 30 + i * 25, 170, 12, style="F")
    pdf.output(str(path))


def make_locked(src, path):
    writer = PdfWriter(clone_from=PdfReader(str(src)))
    writer.encrypt(user_password="1234", algorithm="RC4-128")
    with open(path, "wb") as f:
        writer.write(f)


def main():
    font = Path(sys.argv[1]) if len(sys.argv) > 1 else next((p for p in FONT_CANDIDATES if p.exists()), None)
    if font is None or not font.exists():
        sys.exit("한글 글꼴이 없음. Noto Sans CJK KR 등의 경로를 인자로 준다")
    make_report(font, HERE / "upload_report.pdf")
    make_scan(HERE / "upload_scan.pdf")
    make_locked(HERE / "upload_report.pdf", HERE / "upload_locked.pdf")
    for name in ("upload_report.pdf", "upload_scan.pdf", "upload_locked.pdf"):
        print(f"{name}: {(HERE / name).stat().st_size:,} 바이트")


if __name__ == "__main__":
    main()
