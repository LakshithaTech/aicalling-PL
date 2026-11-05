# AIVoiceAgentAPI

AI-powered voice agent for automated calling, lead qualification, and customer interactions using Plivo, Sarvam AI, and OpenAI.

---

## Environment Setup & Dependencies (`venv` vs `requirements.txt`)

Instead of pushing the `venv/` directory directly, all dependencies are managed via `requirements.txt`.

### Why `venv/` is Never Pushed to Git:
- **Machine & OS Incompatible**: The `venv/` folder contains Windows-specific `.exe` binaries, C-extensions, and hardcoded local paths. If deployed to Linux/Docker or cloned on another computer, `venv` will fail to run.
- **Massive Size**: Pushing `venv/` uploads over 8,100 files (~95 MB) of compiled third-party code that clutters the repository.
- **Standard Python Practice**: Anyone cloning the repository simply creates a clean local environment and installs dependencies from `requirements.txt`.

---

## Getting Started

### 1. Create and Activate a Virtual Environment

- **Windows (PowerShell / CMD):**
  ```bash
  python -m venv venv
  .\venv\Scripts\activate
  ```

- **Linux / macOS:**
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  ```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create your local `.env` file using the provided `.env.example`:

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Open `.env` and fill in your actual credentials (Plivo, Sarvam AI, OpenAI, and PostgreSQL database).
