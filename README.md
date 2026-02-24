# GhostWriter 👻
> Simulate realistic human typing patterns — with natural speed variation, 
> typos, self-corrections, and thinking pauses.

## What it does
GhostWriter takes any text and types it for you like a human would — 
inconsistent speed, occasional mistakes, pauses between paragraphs, 
and even mid-sentence second-guessing. Built with a clean desktop UI.

## Features
- Variable WPM with natural burstiness and rhythm
- Realistic typos using keyboard-neighbor logic with auto-correction
- Smart paragraph pauses that scale with content length
- Mistake discovery — navigates back to fix word choices mid-session
- AI false starts via local Ollama (optional)
- Pause / resume / stop at any point
- Settings saved between sessions

## Install
```bash
pip install pyqt6 pyautogui
python main.py
```

## How to use
1. Paste your text in the left panel
2. Set your speed and behavior in Settings
3. Hit **START**, switch to your target window before countdown ends
4. Move mouse to any screen corner to emergency stop

## Optional: AI False Starts
Install [Ollama](https://ollama.ai) locally with `llama3.2` or `phi3`.
Enables AI-generated fragments that get typed and deleted mid-session.

## Built with
Python · PyQt6 · PyAutoGUI · Ollama
