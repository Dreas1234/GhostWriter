import sys
import time
import random
import math
import json
import os
import re
import urllib.request
import urllib.error
import pyautogui
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLabel, QSlider, QPushButton, QFrame, QProgressBar,
    QCheckBox, QScrollArea, QStackedWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont

SETTINGS_FILE = "typer_settings.json"


# ─────────────────────────────────────────────── text helpers
def detect_text_type(text):
    code_indicators = 0
    lines = text.split('\n')
    total_lines = max(len(lines), 1)
    for line in lines:
        stripped = line.strip()
        if re.match(r'^(import |from .+ import |def |class |#!|//|/\*|\*/|package |using )', stripped):
            code_indicators += 2
        elif re.match(r'^(if |elif |else:|for |while |return |try:|except |finally:|with )', stripped):
            code_indicators += 1
        elif stripped.endswith(('{', '}', ');', '};', ':')) and not stripped.endswith('.:'):
            code_indicators += 1
        elif re.match(r'^\s', line) and stripped and not stripped[0].isupper():
            code_indicators += 0.5
    return "code" if (code_indicators / total_lines) > 0.15 else "prose"


def split_into_blocks(text):
    text_type = detect_text_type(text)
    if text_type == "prose":
        raw_blocks = re.split(r'\n\s*\n', text)
        blocks = []
        for i, block in enumerate(raw_blocks):
            if block.strip():
                blocks.append(block)
                if i < len(raw_blocks) - 1:
                    blocks[-1] += '\n\n'
        return blocks if blocks else [text]

    lines = text.split('\n')
    blocks, current_block, i = [], [], 0
    while i < len(lines):
        line, stripped = lines[i], lines[i].strip()
        is_boundary = False
        if current_block:
            if re.match(r'^(def |class |async def )', stripped):
                is_boundary = True
            elif re.match(r'^(import |from .+ import )', stripped):
                prev = current_block[-1].strip()
                if prev and not re.match(r'^(import |from .+ import |#)', prev):
                    is_boundary = True
            elif stripped == '' and current_block and current_block[-1].strip() == '':
                while i < len(lines) and lines[i].strip() == '':
                    current_block.append(lines[i]); i += 1
                blocks.append('\n'.join(current_block)); current_block = []; continue
        if is_boundary and current_block:
            blocks.append('\n'.join(current_block)); current_block = []
        current_block.append(line); i += 1
    if current_block:
        blocks.append('\n'.join(current_block))
    return blocks if blocks else [text]


OLLAMA_BASE_URL = "http://localhost:11434"


# ─────────────────────────────────────────────── workers
class OllamaCheckWorker(QObject):
    result = pyqtSignal(bool, str)
    finished = pyqtSignal()

    def run(self):
        try:
            req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    models = [m["name"] for m in data.get("models", [])]
                    if models:
                        self.result.emit(True, f"Connected — {len(models)} model(s)")
                    else:
                        self.result.emit(False, "Ollama running but no models installed")
                else:
                    self.result.emit(False, "Ollama returned unexpected status")
        except (urllib.error.URLError, OSError):
            self.result.emit(False, "Ollama not detected")
        except Exception as e:
            self.result.emit(False, f"Error: {str(e)}")
        finally:
            self.finished.emit()


class TypingWorker(QObject):
    text_updated     = pyqtSignal(str)
    progress_updated = pyqtSignal(int)
    countdown_updated= pyqtSignal(int)
    status_message   = pyqtSignal(str)
    phase_changed    = pyqtSignal(str)
    block_updated    = pyqtSignal(int, int)
    stats_updated    = pyqtSignal(dict)
    finished         = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.source_text = ""
        self.wpm = 65
        self.error_rate = 3.0
        self.variability = 40
        self.burstiness = 50
        self.start_delay = 5
        self.block_pause = 8
        self.smart_pausing = True
        self.false_starts_enabled = False
        self.false_start_count = 3
        self.mistake_discovery_enabled = True
        self.edit_frequency = 3
        self._stop_requested = False
        self._pause_requested = False
        self._thinking_messages = [
            "Thinking...", "Collecting thoughts...", "Composing...",
            "Considering phrasing...", "Reviewing...", "Reflecting...",
        ]
        self.keyboard_neighbors = {
            'a':'qwsz','b':'vghn','c':'xdfv','d':'serfcx','e':'wrsdf',
            'f':'drtgvc','g':'ftyhbv','h':'gyujnb','i':'ujko','j':'hukmn',
            'k':'ijlm','l':'opk','m':'njk','n':'bhjm','o':'iklp','p':'ol',
            'q':'wa','r':'edft','s':'awedzx','t':'rfgy','u':'yhjkio',
            'v':'cfgb','w':'qase','x':'zsdc','y':'tghu','z':'asx',' ':'cvbnm',
        }

    def stop(self): self._stop_requested = True
    def pause(self): self._pause_requested = True
    def resume(self): self._pause_requested = False

    def get_delay(self, index):
        base = 60 / (self.wpm * 5)
        jitter = (random.random() - 0.5) * 2 * (self.variability / 100) * base
        burst  = math.sin(index / 5) * (self.burstiness / 100) * base
        return max(0.005, base + jitter + burst)

    def _responsive_sleep(self, duration):
        end = time.time() + duration
        while time.time() < end and not self._stop_requested:
            if self._pause_requested:
                time.sleep(0.1)
                end += 0.1  # extend deadline while paused
            else:
                time.sleep(0.2)

    def _generate_false_start(self, context):
        prompt = (
            "Given this writing context, write 1 sentence fragment (8-20 words) "
            "that someone might START typing but then delete and rephrase. "
            "Match the writing style. Output only the fragment, nothing else.\n\n"
            f"Context: {context}"
        )
        for model in ("llama3.2:3b", "phi3"):
            try:
                payload = json.dumps({
                    "model": model, "prompt": prompt, "stream": False,
                    "options": {"temperature": 0.9, "num_predict": 35},
                }).encode()
                req = urllib.request.Request(
                    f"{OLLAMA_BASE_URL}/api/generate", data=payload,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    fragment = json.loads(resp.read()).get("response","").strip()
                    fragment = fragment.split('\n')[0].strip().strip('"\'')
                    if fragment: return fragment
            except Exception:
                continue
        return None

    def _perform_false_start(self, typed_content, bcd):
        ctx = typed_content[-random.randint(150,250):] if len(typed_content)>150 else typed_content
        fragment = self._generate_false_start(ctx)
        if not fragment or self._stop_requested: return typed_content
        self.status_message.emit("Reconsidering...")
        for ch in fragment:
            if self._stop_requested: return typed_content
            pyautogui.write(ch); typed_content += ch
            self.text_updated.emit(typed_content)
            time.sleep(self.get_delay(0) * random.uniform(0.8, 1.2))
        time.sleep(random.uniform(0.7, 2.0))
        for _ in range(len(fragment)):
            if self._stop_requested: return typed_content
            pyautogui.press('backspace'); typed_content = typed_content[:-1]
            self.text_updated.emit(typed_content)
            time.sleep(bcd * random.uniform(0.3, 0.7))
        time.sleep(random.uniform(0.5, 1.2))
        return typed_content

    _edit_replacements = [
        ("very","quite"),("good","solid"),("bad","poor"),("big","large"),
        ("small","minor"),("nice","pleasant"),("hard","difficult"),("easy","simple"),
        ("fast","quick"),("show","demonstrate"),("help","assist"),("use","utilize"),
        ("get","obtain"),("make","create"),("think","believe"),("want","desire"),
        ("need","require"),("try","attempt"),("start","begin"),("also","additionally"),
        ("but","however"),("so","therefore"),("just","simply"),("really","truly"),
        ("thing","aspect"),("stuff","material"),("kind","type"),("like","such as"),
        ("important","essential"),("different","distinct"),
        ("probelm","problem"),("teh","the"),("wiht","with"),("becuase","because"),
        ("recieve","receive"),("acheive","achieve"),("occured","occurred"),("seperate","separate"),
    ]

    def _find_editable_word(self, typed_content):
        # Search directly in the last ~500 chars of typed_content so distance
        # calculations are always accurate (no sentence-reconstruction mismatch).
        lookback = 500
        region_start = max(0, len(typed_content) - lookback)
        region = typed_content[region_start:]
        if len(region) < 20:
            return None
        pairs = list(self._edit_replacements); random.shuffle(pairs)
        for old, new in pairs:
            matches = list(re.finditer(r'\b' + re.escape(old) + r'\b', region, re.I))
            if matches:
                m = random.choice(matches)
                # distance from the start of the matched word to end of typed_content
                abs_pos = region_start + m.start()
                dist_from_end = len(typed_content) - abs_pos
                rep = (new[0].upper() + new[1:]) if m.group()[0].isupper() else new
                return (dist_from_end, m.group(), rep)
        return None

    def _perform_mistake_discovery(self, typed_content, bcd):
        result = self._find_editable_word(typed_content)
        if not result or self._stop_requested: return typed_content
        dist, old_word, new_word = result
        self.status_message.emit("Fixing mistake...")
        time.sleep(random.uniform(1.0, 2.5))
        if self._stop_requested: return typed_content
        for _ in range(dist):
            if self._stop_requested: return typed_content
            pyautogui.press('left'); time.sleep(bcd * random.uniform(0.15, 0.35))
        for _ in range(len(old_word)):
            if self._stop_requested: return typed_content
            pyautogui.hotkey('shift','right'); time.sleep(bcd * random.uniform(0.15, 0.35))
        pyautogui.press('delete'); time.sleep(bcd * random.uniform(0.5, 1.0))
        for ch in new_word:
            if self._stop_requested: return typed_content
            pyautogui.write(ch); time.sleep(self.get_delay(0) * random.uniform(0.8, 1.2))
        ep = len(typed_content) - dist
        typed_content = typed_content[:ep] + new_word + typed_content[ep+len(old_word):]
        self.text_updated.emit(typed_content)
        time.sleep(bcd * random.uniform(0.3, 0.6))
        pyautogui.press('end'); pyautogui.hotkey('ctrl','end')
        time.sleep(random.uniform(0.3, 0.7))
        return typed_content

    _abbreviations = {
        "mr","mrs","ms","dr","prof","sr","jr","st","ave","blvd","dept","est",
        "govt","inc","corp","ltd","co","vs","etc","approx","assn","div","gen",
        "gov","hon","fig","eq","vol","no","op","ed","rev","al","e.g","i.e",
    }

    def _is_sentence_end(self, block, i):
        char = block[i]
        if char not in '.!?': return False
        if char in '!?': return True
        ws = i-1
        while ws >= 0 and block[ws].isalpha(): ws -= 1
        word = block[ws+1:i].lower()
        if word in self._abbreviations or (len(word)==1 and word.isalpha()): return False
        if i > 0 and block[i-1] == '.': return False
        j = i+1
        while j < len(block) and block[j]==' ': j+=1
        if j >= len(block) or block[j] in '\n': return True
        return block[j].isupper()

    def _get_block_pause(self, next_block):
        if not self.smart_pausing:
            return self.block_pause + random.uniform(-self.block_pause*0.3, self.block_pause*0.3)
        n = len(next_block.strip())
        base = random.uniform(3,6) if n<100 else (random.uniform(6,12) if n<=300 else random.uniform(12,25))
        return base + random.uniform(-base*0.3, base*0.3)

    def _emit_stats(self, chars_typed, total_chars, total_blocks, block_idx, fs_done, ed_done, t0):
        elapsed = time.time() - t0
        wpm = (chars_typed/5)/(elapsed/60) if elapsed>0 and chars_typed>0 else 0
        eta = ((total_chars-chars_typed)/5)/(wpm/60) if wpm>0 else 0
        self.stats_updated.emit({
            "actual_wpm": round(wpm), "chars_typed": chars_typed,
            "total_chars": total_chars, "blocks_done": block_idx,
            "total_blocks": total_blocks, "eta_seconds": round(eta),
            "false_starts": fs_done, "corrections": ed_done,
        })

    def run(self):
        try:
            self._stop_requested = False
            pyautogui.PAUSE = 0.0
            blocks      = split_into_blocks(self.source_text)
            total_blocks= len(blocks)
            total_chars = len(self.source_text)
            bcd         = 60 / (self.wpm * 5)

            self.phase_changed.emit("countdown")
            self.status_message.emit("Switch to your target window…")
            for i in range(self.start_delay, 0, -1):
                if self._stop_requested:
                    self.phase_changed.emit("stopped"); self.finished.emit(); return
                self.countdown_updated.emit(i); time.sleep(1)
            self.countdown_updated.emit(0)
            self.phase_changed.emit("typing")

            typed_content = ""
            chars_typed   = 0
            chars_micro   = 0
            micro_thresh  = random.randint(60, 100)

            # Both false starts and edits fire only at sentence boundaries so
            # they never interrupt mid-sentence.
            sentence_ends = [m.end() for m in re.finditer(r'[.!?]\s', self.source_text)]

            fs_triggers = set()
            if self.false_starts_enabled and self.false_start_count>0 and total_chars>200:
                eligible_fs = [p for p in sentence_ends if 80 < p < total_chars - 60]
                if eligible_fs:
                    fs_triggers = set(random.sample(
                        eligible_fs, min(self.false_start_count, len(eligible_fs))))
            fs_rem, fs_done = len(fs_triggers), 0

            ed_triggers = set()
            if self.mistake_discovery_enabled and self.edit_frequency>0 and total_chars>200:
                eligible_ed = [p for p in sentence_ends
                               if 80 < p < total_chars - 40 and p not in fs_triggers]
                if eligible_ed:
                    ed_triggers = set(random.sample(
                        eligible_ed, min(self.edit_frequency, len(eligible_ed))))
            ed_rem, ed_done = len(ed_triggers), 0
            t0 = time.time()

            for bi, block in enumerate(blocks):
                if self._stop_requested: break
                self.block_updated.emit(bi+1, total_blocks)
                self.status_message.emit(f"Block {bi+1}/{total_blocks}")
                self.phase_changed.emit("typing")
                chars_micro, micro_thresh = 0, random.randint(60,100)
                just_ended = False

                for ci, char in enumerate(block):
                    if self._stop_requested: break
                    # honour manual pause — hold here until resumed
                    while self._pause_requested and not self._stop_requested:
                        time.sleep(0.1)
                    if self._stop_requested: break
                    delay = self.get_delay(chars_typed)

                    if just_ended and char not in (' ','\n','\t'):
                        time.sleep(random.uniform(0.2, 0.6)); just_ended = False

                    if char in '.!?':
                        delay += bcd*6 + random.random()*bcd*2
                        if self._is_sentence_end(block, ci):
                            delay += random.uniform(0.8,1.5) if char=='?' else \
                                     random.uniform(0.6,1.3) if char=='!' else \
                                     random.uniform(0.5,1.0)
                            just_ended = True
                    elif char in ',;:':
                        delay += bcd*3 + random.random()*bcd

                    if self.smart_pausing and chars_micro >= micro_thresh:
                        time.sleep(random.uniform(0.4, 2.0))
                        chars_micro, micro_thresh = 0, random.randint(60,100)

                    if chars_typed in fs_triggers and fs_rem > 0:
                        fs_rem -= 1; fs_done += 1
                        self.phase_changed.emit("pausing")
                        typed_content = self._perform_false_start(typed_content, bcd)
                        if self._stop_requested: break
                        self.phase_changed.emit("typing")
                        self.status_message.emit(f"Block {bi+1}/{total_blocks}")

                    if chars_typed in ed_triggers and ed_rem > 0:
                        ed_rem -= 1; ed_done += 1
                        self.phase_changed.emit("pausing")
                        typed_content = self._perform_mistake_discovery(typed_content, bcd)
                        if self._stop_requested: break
                        self.phase_changed.emit("typing")
                        self.status_message.emit(f"Block {bi+1}/{total_blocks}")

                    if random.random()*100 < self.error_rate and char not in ' \n\t':
                        typo = random.choice(self.keyboard_neighbors.get(char.lower(),'asdf'))
                        pyautogui.write(typo); typed_content += typo
                        self.text_updated.emit(typed_content)
                        time.sleep(delay*0.4 + bcd*2 + random.random()*0.1)
                        pyautogui.press('backspace'); typed_content = typed_content[:-1]
                        self.text_updated.emit(typed_content); time.sleep(bcd)

                    pyautogui.write(char); typed_content += char
                    chars_typed += 1; chars_micro += 1
                    if chars_typed%2==0 or self.wpm<100:
                        self.text_updated.emit(typed_content)
                    self.progress_updated.emit(int(chars_typed/total_chars*100))
                    if chars_typed%10==0:
                        self._emit_stats(chars_typed,total_chars,total_blocks,bi+1,fs_done,ed_done,t0)
                    time.sleep(delay)

                if bi < total_blocks-1 and not self._stop_requested:
                    self.phase_changed.emit("pausing")
                    pause = self._get_block_pause(blocks[bi+1])
                    self.status_message.emit(
                        f"Block {bi+1}/{total_blocks} done — {random.choice(self._thinking_messages)}")
                    self._responsive_sleep(pause)

            self.text_updated.emit(typed_content)
            self.progress_updated.emit(100)
            self._emit_stats(chars_typed,total_chars,total_blocks,total_blocks,fs_done,ed_done,t0)
            self.phase_changed.emit("complete")

        except pyautogui.FailSafeException:
            self.status_message.emit("Fail-safe triggered — stopped")
            self.phase_changed.emit("error")
        except Exception as e:
            self.status_message.emit(f"Error: {e}")
            self.phase_changed.emit("error")
        finally:
            self.finished.emit()


# ─────────────────────────────────────────────── shared widget factories
def _slider(mn, mx, default):
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(mn, mx); s.setValue(default)
    s.setStyleSheet("""
        QSlider::groove:horizontal {
            height:6px; background:#1e293b; border-radius:3px; border:none;
        }
        QSlider::sub-page:horizontal { background:#6366f1; border-radius:3px; }
        QSlider::handle:horizontal {
            background:#fff; width:16px; height:16px;
            margin:-5px 0; border-radius:8px; border:2px solid #6366f1;
        }
        QSlider::handle:horizontal:hover { background:#818cf8; }
        QSlider:disabled { opacity:0.35; }
    """)
    return s


def _checkbox(text, checked=True):
    cb = QCheckBox(text); cb.setChecked(checked)
    cb.setStyleSheet("""
        QCheckBox { color:#e2e8f0; font-size:13px; spacing:8px; }
        QCheckBox::indicator {
            width:17px; height:17px; border-radius:4px;
            border:1px solid #475569; background:#1e293b;
        }
        QCheckBox::indicator:checked { background:#6366f1; border-color:#6366f1; }
        QCheckBox:disabled { color:#475569; }
        QCheckBox::indicator:disabled { border-color:#334155; background:#0f172a; }
    """)
    return cb


def _section_label(text):
    l = QLabel(text)
    l.setStyleSheet("color:#64748b; font-size:10px; font-weight:700; letter-spacing:1.5px; border:none;")
    return l


def _val_label(text):
    l = QLabel(text)
    l.setStyleSheet("color:#e2e8f0; font-size:13px; border:none;")
    return l


def _hsep():
    f = QFrame(); f.setFixedHeight(1)
    f.setStyleSheet("background:#1e293b; margin:2px 0;")
    return f


# ─────────────────────────────────────────────── main window
class HumanTyperApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GhostWriter — Human Pattern Simulator")
        self.setMinimumSize(900, 640)
        self._ollama_available = False
        self._build_ui()
        self.load_settings()
        self._create_worker()

    # ══════════════════════════════════════════════ UI
    def _build_ui(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#020617; color:#e2e8f0; }
            QLabel { font-family:'Inter',-apple-system,sans-serif; font-size:13px; color:#e2e8f0; }
            QTextEdit {
                background:#0a1628; color:#f8fafc; border:1px solid #1e293b;
                border-radius:8px; padding:14px; font-size:14px;
                selection-background-color:#4f46e5;
            }
            QTextEdit:focus { border-color:#4f46e5; }
            QPushButton {
                font-family:'Inter',sans-serif; font-weight:600;
                font-size:13px; border-radius:7px; padding:9px 18px; border:none;
            }
            QProgressBar {
                background:#0f172a; border-radius:3px; border:none; height:5px;
            }
            QProgressBar::chunk {
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #6366f1,stop:1 #8b5cf6);
                border-radius:3px;
            }
            QScrollBar:vertical { background:#0a1628; width:5px; margin:0; }
            QScrollBar::handle:vertical { background:#334155; border-radius:2px; min-height:20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
            QScrollArea { border:none; background:transparent; }
        """)

        root = QWidget(); self.setCentralWidget(root)
        root_vbox = QVBoxLayout(root)
        root_vbox.setContentsMargins(0,0,0,0); root_vbox.setSpacing(0)

        # ── chrome bar ────────────────────────────────────────────
        chrome = QFrame()
        chrome.setFixedHeight(54)
        chrome.setStyleSheet("background:#0a1628; border-bottom:1px solid #1e293b;")
        ch = QHBoxLayout(chrome)
        ch.setContentsMargins(24,0,24,0); ch.setSpacing(0)

        logo = QLabel("GhostWriter")
        logo.setStyleSheet("font-size:17px; font-weight:800; color:#fff; letter-spacing:-0.5px;")
        badge = QLabel("beta")
        badge.setStyleSheet(
            "font-size:9px; font-weight:700; color:#6366f1; background:#1e1b4b;"
            "border-radius:4px; padding:1px 6px; margin-left:8px; letter-spacing:1px;")
        ch.addWidget(logo); ch.addWidget(badge); ch.addStretch()

        self._tab_btns = []
        for idx, (icon, label) in enumerate([("▶", "Simulate"), ("⚙", "Settings")]):
            btn = QPushButton(f"  {icon}  {label}  ")
            btn.setCheckable(True)
            btn.setStyleSheet(self._tab_style(False))
            btn.clicked.connect(lambda _, i=idx: self._switch_tab(i))
            ch.addWidget(btn)
            self._tab_btns.append(btn)

        root_vbox.addWidget(chrome)

        # ── stacked pages ─────────────────────────────────────────
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_simulate_page())
        self.stack.addWidget(self._build_settings_page())
        root_vbox.addWidget(self.stack, 1)
        self._switch_tab(0)

    def _tab_style(self, active):
        if active:
            return ("QPushButton { background:#6366f1; color:#fff; border-radius:6px;"
                    " font-size:13px; font-weight:600; padding:6px 16px; }")
        return ("QPushButton { background:transparent; color:#64748b; border-radius:6px;"
                " font-size:13px; font-weight:500; padding:6px 16px; }"
                "QPushButton:hover { background:#1e293b; color:#e2e8f0; }")

    def _switch_tab(self, idx):
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._tab_btns):
            btn.setChecked(i == idx)
            btn.setStyleSheet(self._tab_style(i == idx))

    # ── Simulate page ─────────────────────────────────────────────
    def _build_simulate_page(self):
        page = QWidget()
        vbox = QVBoxLayout(page)
        vbox.setContentsMargins(32, 26, 32, 26); vbox.setSpacing(14)

        # control row
        ctrl = QHBoxLayout(); ctrl.setSpacing(10)
        self.btn_start = QPushButton("▶  START SIMULATION")
        self.btn_start.setMinimumHeight(44)
        self.btn_start.setStyleSheet(
            "QPushButton { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #6366f1,stop:1 #8b5cf6); color:#fff; font-size:13px; }"
            "QPushButton:disabled { background:#1e293b; color:#475569; }")
        self.btn_start.clicked.connect(self.start_typing)

        self.btn_stop = QPushButton("■  STOP")
        self.btn_stop.setMinimumHeight(44); self.btn_stop.setMinimumWidth(100)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton { background:#7f1d1d; color:#fca5a5; border:1px solid #991b1b; }"
            "QPushButton:disabled { background:#1e293b; color:#475569; border:none; }")
        self.btn_stop.clicked.connect(self.stop_typing)

        self.btn_pause = QPushButton("⏸  PAUSE")
        self.btn_pause.setMinimumHeight(44); self.btn_pause.setMinimumWidth(110)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setStyleSheet(
            "QPushButton { background:#1e293b; color:#f8fafc; border:1px solid #334155; }"
            "QPushButton:hover { background:#334155; }"
            "QPushButton:disabled { background:#1e293b; color:#475569; border:none; }")
        self.btn_pause.clicked.connect(self.toggle_pause)

        self.status_dot = QLabel(); self.status_dot.setFixedSize(10,10)
        self._dot_color("#22c55e")
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("font-weight:700; color:#f8fafc; font-size:14px;")

        self.countdown_label = QLabel("")
        self.countdown_label.setStyleSheet(
            "font-size:48px; font-weight:900; color:#fbbf24;"
            "font-family:'JetBrains Mono','Consolas',monospace;")
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.countdown_label.setMinimumWidth(60)

        ctrl.addWidget(self.btn_start)
        ctrl.addWidget(self.btn_stop)
        ctrl.addWidget(self.btn_pause)
        ctrl.addSpacing(12)
        ctrl.addWidget(self.status_dot)
        ctrl.addWidget(self.status_label, 1)
        ctrl.addWidget(self.countdown_label)
        vbox.addLayout(ctrl)

        self.progress_bar = QProgressBar(); self.progress_bar.setTextVisible(False)
        vbox.addWidget(self.progress_bar)

        # stats bar
        sb = QFrame(); sb.setFixedHeight(38)
        sb.setStyleSheet("background:#0a1628; border:1px solid #1e293b; border-radius:7px;")
        sh = QHBoxLayout(sb); sh.setContentsMargins(14,0,14,0); sh.setSpacing(0)
        mono = "'JetBrains Mono','Consolas',monospace"
        def _sv(attr):
            v = QLabel("—")
            v.setStyleSheet(f"font-size:11px;color:#f8fafc;font-weight:700;"
                            f"font-family:{mono};border:none;background:transparent;")
            setattr(self, attr, v); return v
        def _sk(t):
            l = QLabel(t)
            l.setStyleSheet(f"font-size:11px;color:#475569;font-family:{mono};"
                            "border:none;background:transparent;")
            return l
        def _ss():
            s = QLabel("|")
            s.setStyleSheet(f"color:#1e293b;font-family:{mono};"
                            "border:none;background:transparent;padding:0 10px;")
            return s
        for lbl, attr in [("WPM","_sv_wpm"),("CHARS","_sv_chars"),("BLOCK","_sv_blk"),
                           ("ETA","_sv_eta"),("EDITS","_sv_edits"),("FALSE","_sv_false")]:
            sh.addWidget(_sk(f"{lbl} ")); sh.addWidget(_sv(attr))
            if lbl != "FALSE": sh.addWidget(_ss())
        sh.addStretch()
        vbox.addWidget(sb)

        # text panels
        panels = QHBoxLayout(); panels.setSpacing(16)

        in_v = QVBoxLayout(); in_v.setSpacing(5)
        in_hdr = QHBoxLayout()
        in_hdr.addWidget(_section_label("SOURCE TEXT")); in_hdr.addStretch()
        self.char_count_label = QLabel("0 chars · 0 words")
        self.char_count_label.setStyleSheet("color:#334155; font-size:10px;")
        in_hdr.addWidget(self.char_count_label)
        self.source_edit = QTextEdit()
        self.source_edit.setPlaceholderText("Paste the text you want GhostWriter to type…")
        self.source_edit.textChanged.connect(self._update_char_count)
        in_v.addLayout(in_hdr); in_v.addWidget(self.source_edit)

        out_v = QVBoxLayout(); out_v.setSpacing(5)
        out_v.addWidget(_section_label("LIVE TYPING LOG"))
        self.preview_edit = QTextEdit(); self.preview_edit.setReadOnly(True)
        self.preview_edit.setStyleSheet(
            "background:#020617; border-color:#1e293b; color:#475569;"
            "font-family:'JetBrains Mono','Consolas',monospace; font-size:13px;")
        out_v.addWidget(self.preview_edit)

        panels.addLayout(in_v, 1); panels.addLayout(out_v, 1)
        vbox.addLayout(panels, 1)
        return page

    # ── Settings page ─────────────────────────────────────────────
    def _build_settings_page(self):
        page = QWidget()
        outer = QVBoxLayout(page); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        inner = QWidget(); inner.setStyleSheet("background:#020617;")
        vbox = QVBoxLayout(inner)
        vbox.setContentsMargins(40, 30, 40, 40); vbox.setSpacing(0)

        # title row
        tr = QHBoxLayout()
        pg_title = QLabel("Settings")
        pg_title.setStyleSheet("font-size:22px; font-weight:800; color:#fff; letter-spacing:-0.3px;")
        self.btn_save = QPushButton("Save Configuration")
        self.btn_save.setStyleSheet(
            "QPushButton { background:#1e293b; color:#f8fafc; border:1px solid #334155; }"
            "QPushButton:hover { background:#334155; }")
        self.btn_save.clicked.connect(self.save_settings)
        tr.addWidget(pg_title); tr.addStretch(); tr.addWidget(self.btn_save)
        vbox.addLayout(tr); vbox.addSpacing(6)

        hint = QLabel("Changes take effect on the next simulation run.")
        hint.setStyleSheet("color:#475569; font-size:12px;")
        vbox.addWidget(hint); vbox.addSpacing(26)

        def _card(title_text):
            card = QFrame()
            card.setStyleSheet(
                "QFrame{background:#0a1628;border:1px solid #1e293b;border-radius:10px;}")
            cv = QVBoxLayout(card); cv.setContentsMargins(24,18,24,18); cv.setSpacing(12)
            t = QLabel(title_text)
            t.setStyleSheet("font-size:10px;font-weight:700;color:#6366f1;"
                            "letter-spacing:1.5px;border:none;")
            cv.addWidget(t); cv.addWidget(_hsep())
            return card, cv

        def _row(layout, lattr, ltext, sattr, mn, mx, default, tip=""):
            lbl = _val_label(ltext); sl = _slider(mn, mx, default)
            if tip: sl.setToolTip(tip)
            layout.addWidget(lbl); layout.addWidget(sl)
            setattr(self, lattr, lbl); setattr(self, sattr, sl)

        # ── Typing Behavior ───────────────────────────────────────
        c1, c1v = _card("TYPING BEHAVIOR")
        _row(c1v,"wpm_label","Speed: 65 WPM","wpm_slider",15,200,65,"Target words per minute")
        _row(c1v,"error_label","Error Rate: 3%","error_slider",0,20,3,
             "Chance of hitting a neighbour key then correcting")
        _row(c1v,"var_label","Variability: 40%","var_slider",0,100,40,
             "Random timing jitter — higher feels more human")
        _row(c1v,"burstiness_label","Burstiness: 50%","burstiness_slider",0,100,50,
             "Rhythmic speed swings: bursts of momentum vs. hesitation")
        vbox.addWidget(c1); vbox.addSpacing(14)

        # ── Timing ────────────────────────────────────────────────
        c2, c2v = _card("TIMING")
        _row(c2v,"stagger_label","Preparation Delay: 5s","stagger_slider",2,15,5,
             "Countdown before typing — switch to target window during this time")

        self.smart_pause_checkbox = _checkbox("Smart Pausing", True)
        self.smart_pause_checkbox.setToolTip(
            "Vary block pauses by paragraph length and add micro-breaks mid-block")
        self.smart_pause_checkbox.toggled.connect(self._on_smart_pause_toggled)
        c2v.addWidget(self.smart_pause_checkbox)

        self.block_pause_label = _val_label("Block Pause: 8s  (overridden by Smart Pausing)")
        self.block_pause_label.setStyleSheet("color:#334155; font-size:13px; border:none;")
        self.block_pause_label.setWordWrap(True)
        self.block_pause_slider = _slider(2, 30, 8)
        self.block_pause_slider.setEnabled(False)
        c2v.addWidget(self.block_pause_label); c2v.addWidget(self.block_pause_slider)
        vbox.addWidget(c2); vbox.addSpacing(14)

        # ── Mistake Discovery ─────────────────────────────────────
        c3, c3v = _card("MISTAKE DISCOVERY")
        self.mistake_discovery_checkbox = _checkbox("Simulate Mistake Discovery", True)
        self.mistake_discovery_checkbox.setToolTip(
            "Occasionally navigate back and fix word choices or spelling")
        self.mistake_discovery_checkbox.toggled.connect(
            lambda c: self.edit_freq_slider.setEnabled(c))
        c3v.addWidget(self.mistake_discovery_checkbox)
        _row(c3v,"edit_freq_label","Edit Frequency: 3 per session","edit_freq_slider",0,8,3,
             "How many times per session to go back and fix a word")
        vbox.addWidget(c3); vbox.addSpacing(14)

        # ── AI False Starts ───────────────────────────────────────
        c4, c4v = _card("AI FALSE STARTS  (requires Ollama)")
        desc = QLabel("Inserts AI-generated sentence fragments that get deleted and retyped, "
                      "mimicking second-guessing. Needs a local Ollama instance.")
        desc.setStyleSheet("color:#64748b; font-size:12px; border:none;")
        desc.setWordWrap(True); c4v.addWidget(desc)

        self.false_start_checkbox = _checkbox("Enable AI False Starts", False)
        self.false_start_checkbox.setEnabled(False)
        self.false_start_checkbox.toggled.connect(self._on_false_start_toggled)
        c4v.addWidget(self.false_start_checkbox)

        _row(c4v,"false_start_freq_label","False Starts: 3 per session",
             "false_start_freq_slider",0,10,3,"How many false starts to insert per session")
        self.false_start_freq_slider.setEnabled(False)

        or_ = QHBoxLayout(); or_.setSpacing(10)
        self.ollama_status_label = QLabel("● Ollama: Not checked")
        self.ollama_status_label.setStyleSheet("font-size:12px; color:#475569; border:none;")
        self.btn_test_ollama = QPushButton("Test Connection")
        self.btn_test_ollama.setStyleSheet(
            "QPushButton{background:#1e293b;color:#94a3b8;border:1px solid #334155;"
            "font-size:12px;padding:6px 14px;}"
            "QPushButton:hover{background:#334155;color:#f8fafc;}")
        self.btn_test_ollama.clicked.connect(self.test_ollama_connection)
        or_.addWidget(self.ollama_status_label, 1); or_.addWidget(self.btn_test_ollama)
        c4v.addLayout(or_)
        vbox.addWidget(c4); vbox.addSpacing(14)

        # ── safety tip ────────────────────────────────────────────
        tip = QFrame()
        tip.setStyleSheet("QFrame{background:#111827;border:1px solid #1f2937;border-radius:8px;}")
        th = QHBoxLayout(tip); th.setContentsMargins(16,12,16,12); th.setSpacing(10)
        warn = QLabel("⚠"); warn.setStyleSheet("font-size:16px; color:#f59e0b; border:none;")
        tip_text = QLabel(
            "<b style='color:#f59e0b'>Safety tip</b> — "
            "Move your mouse into any screen corner to stop typing immediately "
            "(PyAutoGUI fail-safe).")
        tip_text.setStyleSheet("font-size:12px; color:#9ca3af; border:none;")
        tip_text.setWordWrap(True)
        th.addWidget(warn, 0, Qt.AlignmentFlag.AlignTop); th.addWidget(tip_text, 1)
        vbox.addWidget(tip); vbox.addStretch()

        # wire sliders
        for s in (self.wpm_slider, self.error_slider, self.var_slider, self.burstiness_slider,
                  self.stagger_slider, self.block_pause_slider,
                  self.false_start_freq_slider, self.edit_freq_slider):
            s.valueChanged.connect(self.update_labels)

        scroll.setWidget(inner); outer.addWidget(scroll)
        return page

    # ══════════════════════════════════════════════ helpers
    def _create_worker(self):
        self.thread = QThread()
        self.worker = TypingWorker()
        self.worker.moveToThread(self.thread)
        self.worker.text_updated.connect(self.update_output_preview)
        self.worker.progress_updated.connect(self.progress_bar.setValue)
        self.worker.countdown_updated.connect(lambda v: self.countdown_label.setText(str(v) if v else ""))
        self.worker.status_message.connect(lambda m: self.status_label.setText(m))
        self.worker.phase_changed.connect(self._on_phase)
        self.worker.block_updated.connect(lambda c, t: self.status_label.setText(f"Block {c} / {t}"))
        self.worker.stats_updated.connect(self._on_stats)
        self.worker.finished.connect(self.on_typing_finished)
        self.thread.started.connect(self.worker.run)

    def _dot_color(self, c):
        self.status_dot.setStyleSheet(f"background:{c}; border-radius:5px; border:none;")

    def _on_phase(self, phase):
        self._dot_color({
            "ready":"#22c55e","countdown":"#fbbf24","typing":"#6366f1",
            "pausing":"#f97316","stopped":"#ef4444","error":"#ef4444","complete":"#22c55e",
        }.get(phase, "#64748b"))

    def _on_stats(self, s):
        self._sv_wpm.setText(str(s.get("actual_wpm", 0)))
        self._sv_chars.setText(f"{s.get('chars_typed',0)}/{s.get('total_chars',0)}")
        self._sv_blk.setText(f"{s.get('blocks_done',0)}/{s.get('total_blocks',0)}")
        eta = s.get("eta_seconds", 0)
        self._sv_eta.setText(f"{eta//60}m {eta%60}s" if eta>=60 else f"{eta}s")
        self._sv_edits.setText(str(s.get("corrections", 0)))
        self._sv_false.setText(str(s.get("false_starts", 0)))

    def _update_char_count(self):
        t = self.source_edit.toPlainText()
        w = len(t.split()) if t.strip() else 0
        self.char_count_label.setText(f"{len(t):,} chars · {w:,} words")

    def _on_smart_pause_toggled(self, checked):
        self.block_pause_slider.setEnabled(not checked)
        if checked:
            self.block_pause_label.setText("Block Pause: (overridden by Smart Pausing)")
            self.block_pause_label.setStyleSheet("color:#334155; font-size:13px; border:none;")
        else:
            self.block_pause_label.setText(f"Block Pause: {self.block_pause_slider.value()}s")
            self.block_pause_label.setStyleSheet("color:#e2e8f0; font-size:13px; border:none;")

    def _on_false_start_toggled(self, checked):
        self.false_start_freq_slider.setEnabled(checked and self._ollama_available)

    def update_labels(self):
        self.wpm_label.setText(f"Speed: {self.wpm_slider.value()} WPM")
        self.error_label.setText(f"Error Rate: {self.error_slider.value()}%")
        self.var_label.setText(f"Variability: {self.var_slider.value()}%")
        self.burstiness_label.setText(f"Burstiness: {self.burstiness_slider.value()}%")
        self.stagger_label.setText(f"Preparation Delay: {self.stagger_slider.value()}s")
        self.false_start_freq_label.setText(
            f"False Starts: {self.false_start_freq_slider.value()} per session")
        self.edit_freq_label.setText(
            f"Edit Frequency: {self.edit_freq_slider.value()} per session")
        if not self.smart_pause_checkbox.isChecked():
            self.block_pause_label.setText(f"Block Pause: {self.block_pause_slider.value()}s")

    # ══════════════════════════════════════════════ Ollama
    def test_ollama_connection(self):
        self.btn_test_ollama.setEnabled(False)
        self.ollama_status_label.setText("● Ollama: Checking…")
        self.ollama_status_label.setStyleSheet("font-size:12px; color:#fbbf24; border:none;")
        self._ol_thread = QThread(); self._ol_worker = OllamaCheckWorker()
        self._ol_worker.moveToThread(self._ol_thread)
        self._ol_worker.result.connect(self._on_ollama_result)
        self._ol_worker.finished.connect(self._ol_thread.quit)
        self._ol_thread.started.connect(self._ol_worker.run)
        self._ol_thread.start()

    def _on_ollama_result(self, ok, msg):
        self._ollama_available = ok
        self.btn_test_ollama.setEnabled(True)
        color = "#22c55e" if ok else "#ef4444"
        self.ollama_status_label.setText(f"● Ollama: {msg}")
        self.ollama_status_label.setStyleSheet(f"font-size:12px; color:{color}; border:none;")
        self.false_start_checkbox.setEnabled(ok)
        if not ok:
            self.false_start_checkbox.setChecked(False)
            self.false_start_freq_slider.setEnabled(False)
        else:
            self.false_start_freq_slider.setEnabled(self.false_start_checkbox.isChecked())

    # ══════════════════════════════════════════════ persistence
    def save_settings(self):
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump({
                    "wpm":                  self.wpm_slider.value(),
                    "error_rate":           self.error_slider.value(),
                    "variability":          self.var_slider.value(),
                    "burstiness":           self.burstiness_slider.value(),
                    "stagger":              self.stagger_slider.value(),
                    "block_pause":          self.block_pause_slider.value(),
                    "smart_pausing":        self.smart_pause_checkbox.isChecked(),
                    "false_starts_enabled": self.false_start_checkbox.isChecked(),
                    "false_start_count":    self.false_start_freq_slider.value(),
                    "mistake_discovery":    self.mistake_discovery_checkbox.isChecked(),
                    "edit_frequency":       self.edit_freq_slider.value(),
                }, f)
            self.btn_save.setText("✓  Saved")
            QTimer.singleShot(2200, lambda: self.btn_save.setText("Save Configuration"))
        except Exception:
            pass

    def load_settings(self):
        if not os.path.exists(SETTINGS_FILE): return
        try:
            with open(SETTINGS_FILE) as f: d = json.load(f)
            self.wpm_slider.setValue(d.get("wpm", 65))
            self.error_slider.setValue(d.get("error_rate", 3))
            self.var_slider.setValue(d.get("variability", 40))
            self.burstiness_slider.setValue(d.get("burstiness", 50))
            self.stagger_slider.setValue(d.get("stagger", 5))
            self.block_pause_slider.setValue(d.get("block_pause", 8))
            self.smart_pause_checkbox.setChecked(d.get("smart_pausing", True))
            self.false_start_checkbox.setChecked(d.get("false_starts_enabled", False))
            self.false_start_freq_slider.setValue(d.get("false_start_count", 3))
            self.mistake_discovery_checkbox.setChecked(d.get("mistake_discovery", True))
            self.edit_freq_slider.setValue(d.get("edit_frequency", 3))
            self.update_labels()
            self._on_smart_pause_toggled(self.smart_pause_checkbox.isChecked())
        except Exception:
            pass

    # ══════════════════════════════════════════════ typing control
    def start_typing(self):
        text = self.source_edit.toPlainText().strip()
        if not text:
            self.status_label.setText("Paste some text first!"); return
        self.progress_bar.setValue(0)
        self.countdown_label.setText("")
        self.preview_edit.clear()
        self.status_label.setText("Ready")
        self._dot_color("#22c55e")
        for a in ("_sv_wpm","_sv_chars","_sv_blk","_sv_eta","_sv_edits","_sv_false"):
            getattr(self, a).setText("—")
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True); self.btn_pause.setEnabled(True)

        self.worker.source_text               = text
        self.worker.wpm                       = self.wpm_slider.value()
        self.worker.error_rate                = self.error_slider.value()
        self.worker.variability               = self.var_slider.value()
        self.worker.burstiness                = self.burstiness_slider.value()
        self.worker.start_delay               = self.stagger_slider.value()
        self.worker.block_pause               = self.block_pause_slider.value()
        self.worker.smart_pausing             = self.smart_pause_checkbox.isChecked()
        self.worker.false_starts_enabled      = (
            self.false_start_checkbox.isChecked() and self.false_start_checkbox.isEnabled())
        self.worker.false_start_count         = self.false_start_freq_slider.value()
        self.worker.mistake_discovery_enabled = self.mistake_discovery_checkbox.isChecked()
        self.worker.edit_frequency            = self.edit_freq_slider.value()
        self.thread.start()

    def stop_typing(self):
        self.worker.stop()
        self.status_label.setText("Stopping…")
        self._dot_color("#ef4444")
        self.btn_stop.setEnabled(False)
        self.btn_pause.setEnabled(False)

    def toggle_pause(self):
        if self.worker._pause_requested:
            # Currently paused — resume
            self.worker.resume()
            self.btn_pause.setText("⏸  PAUSE")
            self.btn_pause.setStyleSheet(
                "QPushButton { background:#1e293b; color:#f8fafc; border:1px solid #334155; }"
                "QPushButton:hover { background:#334155; }"
                "QPushButton:disabled { background:#1e293b; color:#475569; border:none; }")
            self.status_label.setText("Resuming…")
            self._dot_color("#6366f1")
        else:
            # Currently typing — pause
            self.worker.pause()
            self.btn_pause.setText("▶  RESUME")
            self.btn_pause.setStyleSheet(
                "QPushButton { background:#065f46; color:#6ee7b7; border:1px solid #047857; }"
                "QPushButton:hover { background:#047857; }"
                "QPushButton:disabled { background:#1e293b; color:#475569; border:none; }")
            self.status_label.setText("Paused")
            self._dot_color("#f97316")

    def on_typing_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸  PAUSE")
        self.btn_pause.setStyleSheet(
            "QPushButton { background:#1e293b; color:#f8fafc; border:1px solid #334155; }"
            "QPushButton:hover { background:#334155; }"
            "QPushButton:disabled { background:#1e293b; color:#475569; border:none; }")
        self.worker._pause_requested = False
        st = self.status_label.text()
        if "Stopping" in st:
            self.status_label.setText("Stopped"); self._dot_color("#ef4444")
        elif "Error" not in st and "Fail-safe" not in st:
            self.status_label.setText("Complete ✓"); self._dot_color("#22c55e")
        self.thread.quit(); self.thread.wait()
        self._create_worker()

    def update_output_preview(self, text):
        self.preview_edit.setPlainText(text)
        sb = self.preview_edit.verticalScrollBar(); sb.setValue(sb.maximum())


# ─────────────────────────────────────────────── entry point
if __name__ == "__main__":
    pyautogui.FAILSAFE = True
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = HumanTyperApp()
    window.show()
    sys.exit(app.exec())