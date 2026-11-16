# Vienna Agent — Web & Mobile Backend

> **Vienna** adalah asisten AI pribadi berbasis LLM yang didukung oleh arsitektur **RAG (Retrieval-Augmented Generation)**, **MCP (Model Context Protocol)**, dan multi-agent backend. Proyek ini merupakan **backend** untuk aplikasi web dan mobile yang menyediakan layanan chat AI, manajemen session, dan integrasi dengan berbagai layanan eksternal.

---

## 📋 Tentang Proyek

Proyek ini adalah **backend server** untuk platform **Vienna Agent** — sebuah ekosistem asisten AI yang dirancang untuk membantu pengguna melalui berbagai keahlian (Skills), mulai dari chat conversational, analisis dokumen, pencarian anime, integrasi Spotify, hingga kontrol perangkat Android via WebSocket.

Proyek ini dibangun oleh **Amamiya (Maireza)**, seorang **AI Application & Automation Engineer** dengan filosofi:

> **"Build once, useful forever"** — setiap proyek yang dibangun harus punya nilai jangka panjang dan mudah digunakan ulang.

---

## 🏗️ Arsitektur & Teknologi

| Komponen | Teknologi |
|---|---|
| **Framework Backend** | FastAPI (Python 3.12+) |
| **LLM Integration** | Gemini SDK, OpenAI SDK, Google GenAI, Inception Labs Mercury (OpenAI-compatible) |
| **RAG Pipeline** | Qdrant Vector Database |
| **Database Utama** | MongoDB (Atlas) |
| **Cache / Queue** | Redis |
| **Authentication** | JWT (HS256) |
| **Real-time Communication** | WebSocket (FastAPI) |
| **Push Notification** | Firebase Cloud Messaging (FCM) |
| **Supabase** | Database & Auth tambahan |
| **Dependency Management** | Poetry (`pyproject.toml` + `uv.lock`) |
| **MCP** | Model Context Protocol (agent skills) |
| **GitHub Sync** | Repository synchronization service |

---

## 🎯 Fitur Utama

### 1. **AI Chat Engine (Streaming)**
- Chat real-time dengan response streaming via SSE (Server-Sent Events)
- Dukungan multi-agent: **Vienna** (asisten AI pribadi) dan **Cristina** (asisten lab penelitian)
- RAG semantic search menggunakan Qdrant untuk konteks yang relevan
- Manajemen session dan riwayat percakapan tersimpan di MongoDB
- Dukungan multi-bahasa (Indonesia & English)
- Role-based access: `customer`, `superadmin`, `sales`

**Engine yang tersedia:**

| Engine | Endpoint | Model default | API key |
|---|---|---|---|
| Gemini | `POST /v1/gemini/chat` | `gemini-3.1-flash-lite` (fallback `gemini-3.5-flash-lite`) | `GEMINI_API_KEY` |
| Mercury (Inception Labs) | `POST /v1/mercury/chat` | `mercury-3` (fallback `mercury-2.5`) | `MERCURY_API_KEY` / `INCEPTION_API_KEY` |
| DeepSeek (LARP Router) | `POST /v1/deepseek/chat` | `deepseek-v4.1-flash` | `LARPROUTER_API_KEY` |

Mercury memakai REST API OpenAI-compatible (`https://api.inceptionlabs.ai/v1`), jadi
tidak menambah dependency. Engine ini mendukung streaming SSE, MCP tool calling,
device tool, dan mengirim `usage` token asli ke client. Pilih engine per request
lewat field `engine` (`"gemini"`/`"mercury"`/`"deepseek"`) saat membuat session, lalu kirim chat
ke endpoint yang sesuai. Cek kesiapan key via `GET /v1/engines`.

DeepSeek menggunakan REST API OpenAI-compatible LARP Router
(`https://larprouter.com/v1`). Konfigurasikan `LARPROUTER_API_KEY` dan, bila perlu,
override `LARPROUTER_MODEL`. Endpoint alias provider tersedia di
`POST /v1/larprouter/chat`.

### 2. **WebSocket Device API**
- Komunikasi real-time dengan perangkat mobile Android
- Device registration dengan FCM token
- Sync inventory aplikasi dari device pengguna
- Relay tool/action results antar device dan server
- Connection manager untuk handle banyak client simultan

### 3. **Router & API Endpoints**
- **`/v1/assistant/chat`** — Chat utama dengan agent Vienna
- **`/v1/gemini/chat`** — Chat via Gemini API
- **`/v1/mercury/chat`** — Chat via Inception Labs Mercury (alias `/v1/inception/chat`)
- **`/v1/deepseek/chat`** — Chat via DeepSeek melalui LARP Router (alias `/v1/larprouter/chat`)
- **`/v1/engines`** — Daftar engine chat + status konfigurasi API key
- **`/v1/auth`** — Autentikasi JWT
- **`/v1/pdf`** — Analisis dokumen PDF (PyMuPDF + AI summary)
- **`/v1/anime`** — Pencarian dan download link anime
- **`/v1/spotify`** — Integrasi musik Spotify
- **`/v1/admin`** — Endpoint admin/internal
- **`/v1/github/sync`** — Sinkronisasi repository GitHub
- **`/v1/device/register`** — Registrasi device mobile
- **`/v1/assistant/ws`** — WebSocket untuk komunikasi device

### 4. **RAG & Document Processing**
- Pipeline ingestion dokumen ke Qdrant vector DB
- Semantic search untuk retrieving konteks yang relevan
- Dukungan analisis PDF dengan ekstraksi metadata dan AI summary
- Chunking dan embedding otomatis

### 5. **Memory System**
- Manajemen memory percakapan pengguna
- Penyimpanan dan pengambilan session history
- Format percakapan terstruktur dengan timestamp

### 6. **GitHub Repository Sync**
- Background task untuk sinkronisasi repository GitHub
- API trigger manual sync

### 7. **Firebase Integration**
- Firebase Admin SDK untuk push notification
- Device token management via FCM

### 8. **Supabase Integration**
- Database dan autentikasi tambahan via Supabase

---

## 📁 Struktur Proyek

```
myAgentic-apps/
├── main.py                     # Entry point — FastAPI application
├── pyproject.toml              # Poetry dependency config
├── uv.lock                     # Locked dependency versions
├── .env                        # Environment variables (DIABAINKAN)
├── .gitignore                  # File/folder yang diabaikan Git
│
├── agents/                     # Agent persona configurations
│   ├── vienna.json            # Vienna AI Assistant persona
│   └── kurisu.json            # Cristina AI Lab Assistant persona
│
├── routers/                    # API route handlers
│   ├── auth_api.py            # JWT authentication
│   ├── pdf_api.py             # PDF analysis endpoints
│   ├── anime_api.py           # Anime search & download
│   ├── spotify_api.py         # Spotify integration
│   ├── admin.py               # Admin/internal endpoints
│   └── gemini_live.py         # Gemini live streaming
│
├── services/                   # Core business logic & integrations
│   ├── agent_service.py       # MCP tools & agent orchestration (Gemini/OpenAI/Mercury)
│   ├── mongo_service.py       # MongoDB database operations
│   ├── redis_service.py       # Redis cache & session
│   ├── qdrant_service.py      # Qdrant vector search
│   ├── rag_service.py         # RAG retrieval logic
│   ├── rag_ingestion_service.py # Document ingestion pipeline
│   ├── connection_manager.py  # WebSocket connection manager
│   ├── github_service.py      # GitHub repository sync
│   ├── supabase_service.py    # Supabase integration
│   ├── firebase_service.py    # Firebase push notifications
│   └── __init__.py
│
├── utils/                      # Utility functions
│   └── prompt_builder.py      # Dynamic prompt construction
│
├── models/                     # AI/ML model files (DIABAIKAN)
│
├── memory/                     # Memory management system
│   ├── memory_manager.py      # Conversation memory handler
│   └── __init__.py
│
├── public/                     # Static files
│
├── document/                   # Documentation (DIABAIKAN)
│   ├── amamiya_profile.md
│   ├── project_summary.md
│   └── project_summary.txt
│
├── deploy_pkg/                 # Deployment packages (DIABAIKAN)
│
└── __pycache__/               # Python cache (DIABAIKAN)
```

---

## 🤖 Agent Profil

### Vienna (`agents/vienna.json`)
- **Role**: Asisten AI Pribadi Amamiya
- **Personality**: Ramah, cerdas, profesional, teknis, berdiskusi mendalam tentang rekayasa teknologi
- **Languages**: Indonesia, English
- **Capabilities**: Coding, debugging, technical explanation, web search, anime search, WhatsApp integration
- **Philosophy**: *"Build once, useful forever"*

### Cristina (`agents/kurisu.json`)
- **Role**: Asisten Lab Penelitian
- **Personality**: Tsundere — cuek, sinis, suka nyelekit, tapi perhatian tersembunyi
- **Languages**: Indonesia, Jepang, English
- **Capabilities**: Coding, technical Q&A, benchmark research, kontak management

---

## ⚙️ Instalasi & Menjalankan

### Prasyarat
- Python 3.12+
- Poetry
- MongoDB (Atlas atau lokal)
- Redis
- Qdrant Vector DB
- Firebase project credentials

### 1. Clone Repository
```bash
git clone <repository-url>
cd myAgentic-apps
```

### 2. Install Dependencies
```bash
poetry install
```

### 3. Konfigurasi Environment
Salin dan edit file `.env`:
```bash
cp .env.example .env
# Edit .env dengan credentials Anda
```

### 4. Jalankan Server
```bash
poetry run python main.py
```

Server akan berjalan di `http://localhost:8000`.

### 5. Akses API Docs
FastAPI menyediakan automatic API documentation:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

---

## 🔌 API Endpoints

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `GET` | `/` | Health check — "Hello World" |
| `POST` | `/v1/assistant/chat` | Chat dengan agent Vienna (streaming) |
| `POST` | `/v1/gemini/chat` | Chat dengan Gemini API (streaming) |
| `POST` | `/v1/mercury/chat` | Chat dengan Inception Labs Mercury (streaming, alias `/v1/inception/chat`) |
| `POST` | `/v1/deepseek/chat` | Chat dengan DeepSeek via LARP Router (streaming) |
| `GET` | `/v1/engines` | Daftar engine chat + status API key |
| `GET` | `/v1/models` | OpenAI-compatible discovery endpoint (alias `/models`, `/v1/models/{id}`) |
| `POST` | `/v1/device/register` | Registrasi device mobile |
| `WS` | `/v1/assistant/ws` | WebSocket device communication |
| `POST` | `/v1/github/sync` | Trigger GitHub sync |
| `GET` | `/v1/auth/...` | Autentikasi JWT |
| `POST` | `/v1/pdf/analyze` | Analisis dokumen PDF |
| `GET` | `/v1/anime/search` | Pencarian anime |
| `GET` | `/v1/spotify/...` | Spotify integration |

---

## 📊 Database & Infrastruktur

### MongoDB
- Menyimpan user data, session history, device registrations, app inventory
- Koleksi utama: `users`, `sessions`, `messages`, `devices`, `device_app_inventory`

### Redis
- Cache untuk tool results dan action relays
- Session management dan real-time data relay via WebSocket
- TTL 60 detik untuk tool result caching

### Qdrant Vector DB
- Semantic search untuk RAG pipeline
- Koleksi: `document_chunk`, `portfolio_document_chunk` (768-dim Gemini, Cosine)
- Retrieval konteks yang relevan untuk chat response
- Default lokal: `QDRANT_URL=http://localhost:6333` (API key boleh kosong). Qdrant Cloud: isi URL https + API key.
- Bila Qdrant mati, startup menampilkan error jelas dan chat tetap jalan tanpa konteks RAG.

### Firebase
- Push notification via FCM
- Device token management

---

## 🔒 Keamanan

- **JWT Authentication** untuk semua endpoint API
- **CORS Middleware** dengan origin whitelist (localhost, Vercel, Cloudflare)
- **Environment variables** untuk semua credential (tidak pernah di-commit)
- **Firebase credentials** dan **API keys** di `.env` (di-abaikan oleh Git)
- **Global exception handlers** untuk logging error tanpa expose sensitive data
- **WebSocket authentication** dengan registration flow

---

## 🧪 Testing

```bash
poetry run pytest
```

---

## 📦 Deployment

Proyek ini bisa di-deploy ke:
- **Vercel** (frontend + serverless functions)
- **Railway / Render** (Full-stack deployment)
- **Hugging Face Spaces** (seperti yang sedang berjalan)
- **Docker container** untuk self-hosted deployment

---

## 👤 Developer

**Amamiya (Maireza)** — AI Application & Automation Engineer

- 📱 WhatsApp: `083863450720`
- 📸 Instagram: `_agonystic_`
- 💻 GitHub: `amamiyakun02`
- 🌐 Website: [amamiyakun02.github.io](https://amamiyakun02.github.io)

> *"Build once, useful forever"*

---
