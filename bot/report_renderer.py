"""
bot/report_renderer.py — рендер медицинского отчёта в PDF.

Принцип: LLM выдаёт структурированный JSON по заданной схеме (см. SCHEMA_DOC).
Этот модуль превращает JSON → HTML (Jinja2) → PDF (WeasyPrint).
Дизайн один раз настраивается в CSS_STYLES, дальше не трогается.

Палитра: тёплое «бумажное» письмо, не клинический документ. Палитра намеренно
приглушённая, без алярм-RGB — чтобы пожилому пациенту было приятно и спокойно
читать; цвета-флаги работают как эмоциональные якоря, не как сигнал тревоги.
"""
from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from jinja2 import Template
from weasyprint import HTML, CSS

log = logging.getLogger(__name__)


SCHEMA_DOC = """
JSON-схема, которую LLM должен вернуть для рендера отчёта:

{
  "title": "Заголовок отчёта",                       // обязательно
  "subtitle": "Подзаголовок одной строкой",          // необязательно
  "patient_line": "ФИО · возраст · дата",            // обязательно

  "opening": "Тёплое обращение-открытие, 1-2 предложения. Задаёт интонацию письма.",  // обязательно

  "headline": {                                      // главная новость отчёта; необязательно
    "tone": "good" | "warning" | "critical",
    "icon": "🎉",
    "headline": "Короткая кричалка результата",
    "body": "1-2 предложения объяснения"
  },

  "highlights": [                                    // ключевые блоки, 2-4 штуки, прозой
    { "tone": "good|warning|critical",
      "icon": "🌟",
      "title": "Заголовок блока",
      "body": "Полный абзац объяснения, 3-5 предложений" }
  ],

  "before_lab_table": "Связующий абзац перед таблицей. 1-2 предложения.",  // необязательно

  "lab_tables": [                                    // таблицы анализов; 0..N
    {
      "title": "Все результаты — по порядку",
      "rows": [
        { "flag": "good|warning|critical", "label": "Гемоглобин",
          "value": "136 г/л", "norm": "120–155", "comment": "Норма." }
      ]
    }
  ],

  "after_lab_summary": "Прозаический абзац-комментарий после таблицы: общая картина, динамика март→апрель словами, что главное запомнить. 3-5 предложений.",  // необязательно

  "lifestyle_text": {                                // образ жизни прозой; необязательно
    "intro": "Абзац: про образ жизни, 2-3 предложения. Не сухой список.",
    "concrete": ["Конкретный пункт 1", "Пункт 2", "Пункт 3"]   // 0-4 коротких буллета
  },

  "before_action_plan": "Связующий абзац перед списком действий. 1 предложение.",  // необязательно

  "action_plan": [                                   // план действий; необязательно
    { "priority": "critical|warning|good", "action": "Что сделать", "deadline": "Когда" }
  ],

  "personal_closing": "Расширенное тёплое закрытие: 2-3 абзаца. Подытожить, поддержать, обозначить главное."  // обязательно
}
"""


_HTML_TEMPLATE = Template("""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{{ report.title }}</title>
</head>
<body>

  <header class="page-header">
    <h1>{{ report.title }}</h1>
    {% if report.subtitle %}<p class="subtitle">{{ report.subtitle }}</p>{% endif %}
    <p class="patient">{{ report.patient_line }}</p>
  </header>

  {% if report.opening %}
  <section class="opening">
    <p>{{ report.opening }}</p>
  </section>
  {% endif %}

  {% if report.headline %}
  <section class="headline tone-{{ report.headline.tone }}">
    <div class="headline-icon">{{ report.headline.icon }}</div>
    <div class="headline-text">
      <p class="headline-line">{{ report.headline.headline }}</p>
      <p class="headline-body">{{ report.headline.body }}</p>
    </div>
  </section>
  {% endif %}

  {% if report.highlights %}
  <section class="highlights">
    {% for h in report.highlights %}
    <div class="highlight accent-{{ h.tone }}">
      <p class="hl-title"><span class="hl-icon">{{ h.icon }}</span>{{ h.title }}</p>
      <p class="hl-body">{{ h.body }}</p>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  {% if report.before_lab_table %}
  <section class="prose prose-bridge">
    <p>{{ report.before_lab_table }}</p>
  </section>
  {% endif %}

  {% if report.lab_tables %}
    {% for t in report.lab_tables %}
    <section class="lab-table">
      {% if t.title %}<h3>{{ t.title }}</h3>{% endif %}
      <table>
        <thead>
          <tr>
            <th class="col-flag"></th>
            <th class="col-label">Показатель</th>
            <th class="col-value">Результат</th>
            <th class="col-norm">Норма</th>
            <th class="col-comment">Что это значит</th>
          </tr>
        </thead>
        <tbody>
          {% for r in t.rows %}
          <tr>
            <td class="col-flag"><span class="dot dot-{{ r.flag }}"></span></td>
            <td class="col-label">{{ r.label }}</td>
            <td class="col-value">{{ r.value }}</td>
            <td class="col-norm">{{ r.norm }}</td>
            <td class="col-comment">{{ r.comment }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </section>
    {% endfor %}
  {% endif %}

  {% if report.after_lab_summary %}
  <section class="prose prose-summary">
    <p>{{ report.after_lab_summary }}</p>
  </section>
  {% endif %}

  {% if report.lifestyle_text %}
  <section class="lifestyle">
    <h3>Образ жизни — что подкрутить</h3>
    {% if report.lifestyle_text.intro %}<p class="lifestyle-intro">{{ report.lifestyle_text.intro }}</p>{% endif %}
    {% if report.lifestyle_text.concrete %}
    <ul class="lifestyle-bullets">
      {% for item in report.lifestyle_text.concrete %}<li>{{ item }}</li>{% endfor %}
    </ul>
    {% endif %}
  </section>
  {% endif %}

  {% if report.before_action_plan %}
  <section class="prose prose-bridge">
    <p>{{ report.before_action_plan }}</p>
  </section>
  {% endif %}

  {% if report.action_plan %}
  <section class="action-plan">
    <h3>Что сделать — конкретно</h3>
    <table>
      <tbody>
        {% for a in report.action_plan %}
        <tr>
          <td class="col-priority"><span class="dot dot-{{ a.priority }}"></span></td>
          <td class="col-action">{{ a.action }}</td>
          <td class="col-deadline">{{ a.deadline }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </section>
  {% endif %}

  {% if report.personal_closing %}
  <section class="closing">
    {% for para in report.personal_closing.split('\n\n') %}<p>{{ para }}</p>{% endfor %}
    <p class="signature">Берегите себя, Elisoncha</p>
  </section>
  {% endif %}

</body>
</html>
""")


_CSS_STYLES = """
@page {
  size: A4;
  margin: 2cm 2cm 2cm 2cm;
  @bottom-center {
    content: counter(page) " / " counter(pages);
    font-family: "Avenir Next", Avenir, "PT Sans", "Helvetica Neue", system-ui, sans-serif;
    font-size: 9pt;
    color: #B0A99A;
  }
}

* { box-sizing: border-box; }

html, body {
  background: #FFFFFF;
  color: #1F2937;
  font-family: "PT Serif", Georgia, "Times New Roman", serif;
  font-size: 11.5pt;
  line-height: 1.7;
  margin: 0;
  padding: 0;
}

p { margin: 0 0 10pt 0; }

/* ============ TYPOGRAPHY ============ */
h1, h2, h3, h4 {
  font-family: "PT Serif", Georgia, "Times New Roman", serif;
  color: #111827;
  line-height: 1.25;
  margin: 0 0 8pt 0;
}
h1 { font-size: 28pt; font-weight: 700; letter-spacing: -0.5px; }
h2 { font-size: 14pt; font-weight: 700; }
h3 { font-size: 15pt; font-weight: 700; margin-top: 22pt; margin-bottom: 8pt; }
h4 { font-size: 11pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; color: #5B7553; }

/* ============ HEADER ============ */
.page-header {
  border-bottom: 1px solid #E5DFD2;
  padding-bottom: 14pt;
  margin-bottom: 22pt;
}
.page-header .subtitle {
  font-size: 12pt;
  color: #6B7280;
  font-style: italic;
  margin-top: 2pt;
}
.page-header .patient {
  font-size: 10pt;
  color: #5B7553;
  margin-top: 8pt;
  letter-spacing: 0.3px;
}

/* ============ OPENING (тёплое обращение) ============ */
.opening {
  margin-bottom: 18pt;
}
.opening p {
  font-family: "PT Serif", Georgia, serif;
  font-size: 13pt;
  line-height: 1.6;
  color: #1F2937;
  font-style: italic;
}

/* ============ HEADLINE CARD — отдельная палитра, чтобы выделялся среди highlights ============ */
.headline {
  display: flex;
  align-items: flex-start;
  gap: 14pt;
  padding: 18pt 22pt;
  border-radius: 6px;
  margin-bottom: 22pt;
  border-left: 4px solid #3F5F7A;
  background: #ECF1F5;
}
/* tone-классы оставлены, но фон/рамка не меняются —
   tone выражает себя через эмодзи-icon, цвет один. */

.headline-icon {
  font-size: 22pt;
  line-height: 1;
  flex-shrink: 0;
}
.headline-line {
  font-family: "PT Serif", Georgia, serif;
  font-size: 14.5pt;
  font-weight: 700;
  margin-bottom: 4pt;
  color: #1F3447;
}
.headline-body {
  font-size: 11.5pt;
  color: #1F3447;
  margin: 0;
  line-height: 1.6;
}

/* ============ HIGHLIGHTS — карточки с tone-фоном ============ */
.highlights {
  margin-bottom: 18pt;
}
.highlight {
  margin-bottom: 12pt;
  padding: 14pt 18pt;
  border-radius: 6px;
  border-left: 4px solid;
}
.highlight.accent-good     { border-color: #6B8E4E; background: #F0F4ED; }
.highlight.accent-warning  { border-color: #B8862E; background: #F8EFDB; }
.highlight.accent-critical { border-color: #B85C3C; background: #F4E5D9; }

.hl-title {
  font-family: "PT Serif", Georgia, serif;
  font-size: 12.5pt;
  font-weight: 700;
  color: #111827;
  margin-bottom: 4pt;
  display: flex;
  align-items: baseline;
  gap: 6pt;
}
.hl-icon {
  font-size: 14pt;
  flex-shrink: 0;
}
.hl-body {
  font-size: 11pt;
  color: #1F2937;
  margin: 0;
  line-height: 1.6;
}

/* ============ PROSE BRIDGES ============ */
.prose {
  margin: 14pt 0;
}
.prose p {
  font-size: 11pt;
  color: #1F2937;
  line-height: 1.65;
  margin: 0;
}
.prose-bridge p { color: #4B5563; }

/* ============ TABLES ============ */
.lab-table, .action-plan {
  background: #FBF9F4;
  border-radius: 8px;
  padding: 14pt 16pt 8pt 16pt;
  margin-top: 18pt;
}
.lab-table h3, .action-plan h3 {
  margin-top: 0;
  margin-bottom: 10pt;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 10.5pt;
  margin-top: 6pt;
}
thead th {
  text-align: left;
  font-family: "Avenir Next", Avenir, "PT Sans", "Helvetica Neue", sans-serif;
  font-size: 9pt;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: #6B7280;
  padding: 6pt 8pt;
  border-bottom: 1.5px solid #D6CFBE;
  background: transparent;
}
tbody tr { border-bottom: 1px solid #ECE6D7; }
tbody tr:nth-child(even) { background: rgba(232, 224, 207, 0.22); }
tbody td {
  padding: 8pt 8pt;
  vertical-align: top;
}

.dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  vertical-align: middle;
}
.dot-good     { background: #6B8E4E; }
.dot-warning  { background: #B8862E; }
.dot-critical { background: #B85C3C; }

.lab-table .col-flag, .action-plan .col-priority { width: 18px; padding-right: 4pt; }
.lab-table .col-label { width: 26%; font-weight: 600; color: #111827; }
.lab-table .col-value { width: 17%; font-variant-numeric: tabular-nums; color: #111827; }
.lab-table .col-norm { width: 13%; color: #6B7280; font-variant-numeric: tabular-nums; }
.lab-table .col-comment { font-size: 10pt; color: #1F2937; line-height: 1.5; }

.action-plan .col-action { font-weight: 500; color: #111827; }
.action-plan .col-deadline { width: 28%; color: #5B7553; font-size: 10pt; }

/* ============ LIFESTYLE — проза + 0..N буллетов ============ */
.lifestyle {
  margin-top: 22pt;
}
.lifestyle h3 { margin-top: 0; }
.lifestyle-intro {
  font-size: 11pt;
  color: #1F2937;
  line-height: 1.65;
  margin-bottom: 8pt;
}
.lifestyle-bullets {
  margin: 4pt 0 0 0;
  padding-left: 14pt;
  font-size: 11pt;
  line-height: 1.55;
}
.lifestyle-bullets li { margin-bottom: 4pt; }

/* ============ CLOSING — расширенное письмо ============ */
.closing {
  margin-top: 28pt;
  padding-top: 16pt;
  border-top: 1px solid #E5DFD2;
}
.closing p {
  font-family: "PT Serif", Georgia, serif;
  font-size: 12pt;
  line-height: 1.65;
  color: #1F2937;
  font-style: italic;
  margin-bottom: 10pt;
}
.closing .signature {
  margin-top: 14pt;
  font-style: normal;
  font-family: "Avenir Next", Avenir, "PT Sans", "Helvetica Neue", sans-serif;
  font-size: 10pt;
  color: #5B7553;
  letter-spacing: 0.6px;
}

/* ============ PRINT FLOW ============ */
/* По умолчанию контент течёт страницами без принудительного «не разрывать»,
   иначе блоки прыгают на новую страницу целиком и остаются полупустые листы.
   «Не рвать» оставляем только маленьким заголовкам и таблицам. */
.headline,
.highlight,
.opening,
tr { page-break-inside: avoid; }
h1, h2, h3, h4 { page-break-after: avoid; }
table { page-break-inside: auto; }
"""


def render_html(report: dict) -> str:
    """Превращает JSON-данные отчёта в полный HTML-документ (без CSS — стили инлайнятся в render_pdf)."""
    return _HTML_TEMPLATE.render(report=report)


def render_pdf(report: dict) -> bytes:
    """JSON-данные отчёта → готовый PDF в виде bytes."""
    html_str = render_html(report)
    pdf_io = BytesIO()
    HTML(string=html_str).write_pdf(pdf_io, stylesheets=[CSS(string=_CSS_STYLES)])
    return pdf_io.getvalue()


def render_pdf_to_file(report: dict, path: Path) -> Path:
    """Удобный wrapper: рендерит и пишет в файл, возвращает путь."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf_bytes = render_pdf(report)
    path.write_bytes(pdf_bytes)
    return path


def render_html_to_file(report: dict, path: Path) -> Path:
    """Для отладки дизайна — сохранить HTML, открыть в браузере, итерировать CSS быстрее."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    html_str = render_html(report)
    full_html = html_str.replace("</head>", f"<style>{_CSS_STYLES}</style></head>")
    path.write_text(full_html, encoding="utf-8")
    return path
