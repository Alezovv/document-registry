STATUSES = [
    "Новый",
    "В работе",
    "Исполнен",
    "Отклонен",
]

SEARCH_FIELDS = [
    "Дата поступления",
    "Срок исполнения",
    "Отдел",
    "Должность",
    "От кого",
    "На кого",
    "Суть запроса",
    "Статус",
]

# Calm, high-contrast light theme.  The palette intentionally stays simple:
# no gradients, blur or animation, so it remains fast on office PCs.
BG_COLOR = "#F4F7FB"
SIDEBAR_COLOR = "#0F172A"
SIDEBAR_HOVER = "#1E293B"
SIDEBAR_ACTIVE = "#2563EB"
CARD_COLOR = "#FFFFFF"
BORDER_COLOR = "#E2E8F0"
TEXT_COLOR = "#0F172A"
SECONDARY_TEXT = "#64748B"
MUTED_TEXT = "#94A3B8"
PRIMARY_COLOR = "#2563EB"
PRIMARY_HOVER = "#1D4ED8"
SUCCESS_COLOR = "#16A34A"
SUCCESS_BG = "#DCFCE7"
WARNING_COLOR = "#D97706"
WARNING_BG = "#FEF3C7"
INFO_COLOR = "#2563EB"
INFO_BG = "#DBEAFE"
DANGER_COLOR = "#DC2626"
DANGER_BG = "#FEE2E2"

APP_STYLE = f"""
QMainWindow {{ background-color: {BG_COLOR}; }}
QWidget {{ font-family: "Segoe UI", Arial, sans-serif; color: {TEXT_COLOR}; }}
QLabel {{ background: transparent; }}

QPushButton {{
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 8px 14px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton:hover {{ outline: none; }}
QPushButton:disabled {{ color: #94A3B8; background-color: #F1F5F9; border-color: #E2E8F0; }}

QLineEdit, QComboBox, QDateEdit, QTextEdit {{
    background-color: #FFFFFF;
    border: 1px solid #CBD5E1;
    border-radius: 9px;
    padding: 8px 11px;
    font-size: 13px;
    color: #0F172A;
    selection-background-color: #DBEAFE;
    selection-color: #0F172A;
}}
QLineEdit:hover, QComboBox:hover, QDateEdit:hover, QTextEdit:hover {{ border-color: #94A3B8; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QTextEdit:focus {{
    border: 1px solid {PRIMARY_COLOR};
    background-color: #FFFFFF;
}}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QTextEdit:disabled {{
    background-color: #F8FAFC;
    color: #94A3B8;
    border-color: #E2E8F0;
}}
QComboBox {{ padding-right: 30px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: none; }}
QComboBox QAbstractItemView {{
    background-color: #FFFFFF;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: #EFF6FF;
    selection-color: #1D4ED8;
}}

QTableWidget {{
    background-color: #FFFFFF;
    alternate-background-color: #FBFDFF;
    border: none;
    gridline-color: transparent;
    font-size: 13px;
    outline: none;
    selection-background-color: #EAF2FF;
    selection-color: #0F172A;
}}
QTableWidget::item {{ padding: 9px 11px; border-bottom: 1px solid #EEF2F6; }}
QTableWidget::item:selected {{ background-color: #EAF2FF; color: #0F172A; }}
QTableWidget::item:hover {{ background-color: #F4F8FE; }}
QHeaderView {{ background-color: #F8FAFC; border: none; }}
QHeaderView::section {{
    background-color: #F8FAFC;
    color: #475569;
    border: none;
    border-bottom: 1px solid #E2E8F0;
    padding: 10px 11px;
    font-size: 11px;
    font-weight: 700;
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 5px 2px 5px 0; }}
QScrollBar::handle:vertical {{ background: #CBD5E1; min-height: 42px; border-radius: 5px; }}
QScrollBar::handle:vertical:hover {{ background: #94A3B8; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 5px 2px 5px; }}
QScrollBar::handle:horizontal {{ background: #CBD5E1; min-width: 42px; border-radius: 5px; }}
QScrollBar::handle:horizontal:hover {{ background: #94A3B8; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

QMenu {{
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 9px;
    padding: 6px;
    color: #1E293B;
    font-size: 12px;
}}
QMenu::item {{ padding: 7px 24px 7px 10px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: #EFF6FF; color: #1D4ED8; }}
QMenu::separator {{ height: 1px; background: #E2E8F0; margin: 5px 8px; }}

QToolTip {{
    background-color: #0F172A;
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 5px 7px;
    font-size: 11px;
}}
QCheckBox {{ spacing: 8px; font-size: 13px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border: 1px solid #CBD5E1;
    border-radius: 5px;
    background: #FFFFFF;
}}
QCheckBox::indicator:hover {{ border-color: #94A3B8; }}
QCheckBox::indicator:checked {{ background-color: {PRIMARY_COLOR}; border-color: {PRIMARY_COLOR}; }}
QFrame {{ border-radius: 10px; }}
QScrollArea {{ background: transparent; border: none; }}
QProgressBar {{
    background: #E2E8F0;
    border: none;
    border-radius: 5px;
    min-height: 8px;
    max-height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {PRIMARY_COLOR}; border-radius: 5px; }}

QCalendarWidget QWidget {{ background: #FFFFFF; color: #0F172A; }}
QCalendarWidget QToolButton {{
    color: #0F172A;
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 6px 9px;
    font-weight: 700;
}}
QCalendarWidget QToolButton:hover {{ background: #EFF6FF; color: #1D4ED8; }}
QCalendarWidget QAbstractItemView {{
    background: #FFFFFF;
    color: #334155;
    selection-background-color: {PRIMARY_COLOR};
    selection-color: #FFFFFF;
    outline: none;
}}
QCalendarWidget QSpinBox {{
    background: #FFFFFF;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    border-radius: 7px;
    padding: 4px 8px;
}}

QMessageBox {{ background: #FFFFFF; }}
QMessageBox QLabel {{ color: #1E293B; font-size: 13px; min-width: 260px; }}
QMessageBox QPushButton {{
    min-height: 34px;
    min-width: 88px;
    background: #FFFFFF;
    color: #334155;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
    padding: 0 14px;
    font-weight: 650;
}}
QMessageBox QPushButton:hover {{ background: #F8FAFC; border-color: #94A3B8; }}
"""

DIALOG_STYLE = """
QDialog { background-color: #F4F7FB; }
#dialogTitle { font-size: 24px; font-weight: 750; color: #0F172A; }
#dialogSubtitle { color: #64748B; font-size: 13px; }
#card { background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 13px; }
#sectionTitle { color: #0F172A; font-size: 14px; font-weight: 700; }
#fieldLabel { color: #475569; font-size: 11px; font-weight: 700; }
QLabel#fieldLabel { text-transform: uppercase; letter-spacing: 0.35px; }
QLineEdit, QTextEdit, QDateEdit, QComboBox {
    background-color: #FFFFFF;
    border: 1px solid #CBD5E1;
    border-radius: 9px;
    padding: 8px 11px;
    font-size: 13px;
    color: #0F172A;
}
QLineEdit:hover, QTextEdit:hover, QDateEdit:hover, QComboBox:hover { border-color: #94A3B8; }
QLineEdit:focus, QTextEdit:focus, QDateEdit:focus, QComboBox:focus { border: 1px solid #2563EB; }
QComboBox::drop-down { border: none; }
QComboBox::down-arrow { image: none; }
#btnAttach { background-color: #EFF6FF; color: #1D4ED8; border: 1px solid #BFDBFE; border-radius: 8px; font-weight: 650; }
#btnAttach:hover { background-color: #DBEAFE; border-color: #93C5FD; }
#btnDetach { background-color: #FFF7F6; color: #DC2626; border: 1px solid #FECACA; border-radius: 8px; font-weight: 650; }
#btnDetach:hover { background-color: #FEE2E2; }
#btnView { background-color: #FFFFFF; color: #334155; border: 1px solid #CBD5E1; border-radius: 8px; font-weight: 650; }
#btnView:hover { background-color: #F8FAFC; border-color: #94A3B8; }
#dialogHeader { background-color: #FFFFFF; border-bottom: 1px solid #E2E8F0; }
#dialogFooter { background-color: #FFFFFF; border-top: 1px solid #E2E8F0; }
#btnPrimary { background-color: #2563EB; color: white; font-weight: 700; border: 1px solid #2563EB; border-radius: 9px; min-width: 140px; }
#btnPrimary:hover { background-color: #1D4ED8; border-color: #1D4ED8; }
#btnPrimary:pressed { background-color: #1E40AF; border-color: #1E40AF; }
#btnSecondary { background-color: #FFFFFF; color: #334155; border: 1px solid #CBD5E1; border-radius: 9px; min-width: 110px; }
#btnSecondary:hover { background-color: #F8FAFC; border-color: #94A3B8; }
#btnDanger { background-color: #FFF7F6; color: #B42318; border: 1px solid #FECDCA; border-radius: 9px; font-weight: 650; }
#btnDanger:hover { background-color: #FEE4E2; border-color: #FDA29B; }
QCheckBox { color: #334155; font-size: 13px; font-weight: 600; spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px; border: 1px solid #CBD5E1; background: #FFFFFF; }
QCheckBox::indicator:checked { background-color: #2563EB; border-color: #2563EB; }
"""
