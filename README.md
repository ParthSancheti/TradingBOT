# Jarvis Trade 🤖📈

**Jarvis Trade** is an AI-powered commercial trading and operations system designed for wholesale and high-velocity electronics/device trade desks. It seamlessly unifies **WhatsApp Web communication** (via Chrome Extension and direct Baileys bridge), **Google Sheets / Apps Script business storage**, and an **Electron Desktop Command Center** featuring intelligent quote calculation, automated triage, voice control, and safe human-in-the-loop dispatch.

---

## 🌟 Key Features

- **Electron Desktop Command Center**:
  - Real-time trading HUD with collapsible queues, live message inspection, market watch, and FX conversion studio.
  - Native Windows UI scaling (`Ctrl +`, `Ctrl -`, `Ctrl 0`) and high-density view toggles.
  - Integrated voice control and speech feedback (Edge TTS + Web Audio API).
- **Intelligent Processing & Quote Engine**:
  - Deterministic pricing math with currency conversion (AED / USD / INR) and tiered margin structures.
  - LLM-assisted intent triage and catalog matching (Gemini API with API key vault rotation).
  - Multi-tier matching: exact model match, alternate capacity/color, or request-for-quote (RFQ).
- **Dual WhatsApp Bridge**:
  - **Chrome Extension (Manifest V3)**: Native Messaging bridge for DOM observation, active inbox scanning, and message extraction.
  - **Direct WhatsApp Bridge (Baileys)**: In-app socket transport for low-latency dispatch and status sync.
- **Enterprise Storage & Resilience**:
  - **Google Sheets & Apps Script**: HMAC-SHA256 authenticated synchronization for live inventory, transaction logs, and audit trails.
  - **OS-Level Key Vault**: Secure credential storage using OS keyring (DPAPI on Windows / Keychain on macOS).
  - **Controlled Dispatch**: Human-in-the-loop review queue, draft confirmation, customizable Turbo Mode, rate-limiting, and night-hold guards.

---

## 🏗️ Project Architecture

```
TradingBOT/
├── backend/               # Python 3.11+ async core (FastAPI, trading engine, OCR, voice)
│   ├── jarvis/            # Core business logic, desktop service, IPC, adapters
│   └── tests/             # Pytest test suites (unit, integration, adversarial gates)
├── desktop/               # Electron application
│   ├── renderer/          # HUD UI, FX studio, voice worklet, views
│   ├── scripts/           # Packaging & PyInstaller automation scripts
│   ├── boundary.cjs       # Strict CSP and IPC boundary enforcement
│   └── main.cjs           # Electron main process
├── extension/             # Google Chrome Manifest V3 Extension
│   ├── actions.mjs        # WhatsApp Web DOM interaction and message actions
│   ├── scanner.mjs        # Active inbox observation and message harvesting
│   └── background.js      # Native Messaging client & bridge lifecycle
├── apps-script/           # Google Apps Script sync handlers & test suites
├── scripts/               # Operational utilities, native helpers, and test runners
├── Start Jarvis.cmd       # Production launcher script
└── Start Jarvis Dev.cmd   # Development launcher script
```

---

## 🚀 Quick Start Guide

### Prerequisites
- **Python**: 3.11 or higher
- **Node.js**: v20 or higher (with npm)
- **Google Chrome**: Latest version (for extension bridge)
- **Operating System**: Windows 10/11 (or macOS / Linux for headless backend)

---

### 1. Clone the Repository
```bash
git clone https://github.com/ParthSancheti/TradingBOT.git
cd TradingBOT
```

---

### 2. Backend Setup
1. Create and activate a Python virtual environment:
   ```bash
   # On Windows:
   python -m venv .venv
   .venv\Scripts\activate

   # On Linux/macOS:
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install Python dependencies:
   ```bash
   pip install -r requirements.lock
   # Or in editable mode:
   pip install -e .
   ```
3. Set up environment variables:
   ```bash
   copy .env.example .env     # Windows
   # cp .env.example .env     # Linux/macOS
   ```

---

### 3. Desktop Application Setup
1. Navigate to the `desktop` directory and install Node dependencies:
   ```bash
   cd desktop
   npm install
   ```
2. Start the Desktop App in Development Mode:
   ```bash
   npm start
   ```
3. To package the standalone Windows binary (`.exe` with bundled PyInstaller engine):
   ```bash
   npm run package
   ```
   *The packaged output will be generated inside `artifacts/desktop/`.*

---

### 4. Chrome Extension Setup
1. Open Google Chrome and navigate to `chrome://extensions/`.
2. Turn ON **Developer mode** (toggle in the top-right corner).
3. Click **Load unpacked** in the top-left corner.
4. Select the `extension/` directory from this project root.
5. The **Jarvis WhatsApp Bridge** icon will appear in your Chrome toolbar.

---

### 5. Running with 1-Click Launchers (Windows)
For convenience on Windows:
- Double-click **`Start Jarvis.cmd`** to run the complete desktop app with bundled worker.
- Double-click **`Start Jarvis Dev.cmd`** for development mode with live console logs.

---

## 🧪 Testing & Verification

Comprehensive offline test suites ensure complete stability:

### Run Backend Unit & Integration Tests:
```bash
.venv\Scripts\python.exe -m pytest
```

### Run Desktop IPC & Extension Tests:
```bash
node --test desktop/*.test.cjs extension/*.test.mjs
```

---

## 🔒 Security & Safe Trading Safeguards

- **Strict Sandbox**: Electron renderer processes have zero Node.js access (`contextIsolation: true`, `nodeIntegration: false`).
- **Encrypted Secrets**: All API tokens (Gemini, Google Sheets keys, WhatsApp sessions) are stored in the OS credential vault.
- **Fail-Safe Dispatch**: Automated outbound sends require explicit user approval unless Turbo Mode is deliberately enabled. Out-of-hours messages automatically queue to Night Hold.

---

## 📄 License
Private & Proprietary. All rights reserved.
