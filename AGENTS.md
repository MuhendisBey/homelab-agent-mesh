# Homelab & Smart Home Multi-Agent Mesh (AGENTS.md)
*Sürüm: v1.1.0 | Son Güncelleme: 2026-11-13 (Production Hardening)*

Bu doküman, projede yeni bir oturum (session) açıldığında sıfırdan başlamamak; mimariyi, tasarım kararlarını, dosya yapısını, güvenlik kurallarını ve anlık durumu eksiksiz hatırlamak için hazırlanmış operasyonel rehberdir.

---

## 1. Proje Özeti ve Temel Amaç
**`homelab-agent-mesh`**, modern bir akıllı ev ve homelab altyapısını yönetmek üzere **Python 3.12+ (asyncio)**, **Strands Agents SDK** ve **FastAPI (Server-Sent Events - SSE)** kullanılarak inşa edilmiş, hiyerarşik çok ajanlı (Hierarchical Multi-Agent) bir orkestratördür.

### Değişmez Mühendislik İlkeleri
1. **12-Factor App & Sıfır Sabit Değer (Zero Hardcoding):** Kod tabanında IP, port, API token, entity ID, model adı, sıcaklık sınırı veya container adı gibi sabitler **asla yer almaz**.
   - Tüm sırlar ve uç noktalar `.env` dosyasından `pydantic-settings` ile okunur.
   - Tüm topoloji, güvenlik sınırları, ajan parametreleri `config/settings.yaml` dosyasından yüklenir.
2. **Açık Kaynak & Depo Hijyeni:** `.env`, `settings.yaml`, SQLite veri tabanları (`data/*.db`) ve loglar kesinlikle `.gitignore` kapsamındadır. Sadece genel örnekler (`.env.example`, `settings.example.yaml`) depoda tutulur.
3. **KISS & Hafif Kaynak Tüketimi:** Ağır vektör veri tabanları veya gereksiz container katmanları kullanılmaz. GMKtec NucBox 3 üzerinde Docker LXC container içerisinde **<80 MB RAM** RSS hedefiyle çalışır.
4. **Savunma Derinliği (Defense in Depth):** Güvenlik kontrolleri (Pre-condition Guard Clauses) hem yerel araç katmanında hem de Strands `InterventionHandler` seviyesinde iki kez doğrulanır.
5. **Dürüst Doğrulama (Actor-Critic):** Bir fiziksel/sistem eylemi yapıldığında araç başarı raporlamaz; `assert_entity_state` ile durum değişikliği fiziksel olarak teyit edildikten sonra başarı bildirilir (`StateAssertionSuccess` / `StateAssertionFailure`).

---

## 2. Sistem Mimarisi ve Bileşenler

```text
                      [ Client UI / Web / Telegram ]
                                     │  ▲
             1. Kullanıcı İstemi     │  │  4. SSE Akışı
               (HTTP POST /chat)     ▼  │     (plan_step, state_assertion, content_delta)
┌────────────────────────────────────────────────────────────────────────────────┐
│                         FASTAPI STREAMING GATEWAY                              │
│       • POST /api/v1/chat/stream          • POST /api/v1/interventions/approve │
│       • GET  /api/v1/health               • POST/GET /api/v1/memory/lessons    │
└────────────────────────────────────┬───────────────────────────────────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│                      SUPERVISOR AGENT (Strands Harness)                        │
│  • Model: gemini-3.8-flash (temp=0.1, settings.yaml üzerinden)                 │
│  • Bellek: SlidingWindowConversationManager (Kısa dönemli oturum hafızası)     │
│  • Görevler: Görev ayrıştırma (Decomposition), yönlendirme, HITL duraklatma   │
└───────────────────┬────────────────────────────────────────┬───────────────────┘
                    │ (Strands .as_tool())                   │ (Strands .as_tool())
                    ▼                                        ▼
┌───────────────────────────────────────┐  ┌─────────────────────────────────────┐
│        HOME ASSISTANT SUB-AGENT       │  │        INFRASTRUCTURE SUB-AGENT     │
│  • Durumsuz (Stateless, context=False)│  │  • Durumsuz (Stateless)             │
│  • Dinamik 'ha' dersleri enjeksiyonu  │  │  • Dinamik 'infra' ders enjeksiyonu │
└─────────┬───────────────────┬─────────┘  └──────────────────┬──────────────────┘
          │                   │                               │
          ▼                   ▼                               ▼
┌──────────────────┐ ┌────────────────────────┐ ┌────────────────────────────────┐
│   ha-mcp Client  │ │  ha-custom-ws Adapter  │ │   homelab-infra-mcp Toolkit    │
│  (Harici SSE MCP)│ │ (Native Async WS)      │ │  • psutil Telemetri (CPU/RAM)  │
│  • Varlık arama  │ │ • Hızlı döngü (loops)  │ │  • Docker SDK Socket Kontrolü  │
│  • Standart servis│ │ • raw event.fire       │ │  • Proxmox VE API (LXC/VM)    │
│                  │ │ • assert_entity_state  │ │  • Korumalı Konteyner Kalkanı  │
└──────────────────┘ └────────────────────────┘ └────────────────────────────────┘
          │                   │                               │
          └───────────────────┴───────────────┬───────────────┘
                                              ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│               3 KATMANLI ZERO-MISS REFLECTION MEMORY (SQLite)                  │
│  • Tier 1: Domain İzolasyonu ('ha' vs 'infra' asla birbirine sızmaz)           │
│  • Tier 2: Çekirdek Kurallar (is_core_invariant=1 -> %0 kaçırma, daima promptta)│
│  • Tier 3: Semantik Benzerlik (Gemini Embeddings + Local Hashed N-gram fallback)│
└────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Kritik Güvenlik Kalkanları ve İki Aşamalı Onay (HITL)

### A. HVAC & Açık Pencere Koruması (`HvacSetpointGuard`)
- `min_hvac_temp` (15.0°C) ve `max_hvac_temp` (28.0°C) dışındaki komutlar derhal `SafetyBoundViolationError` ile engellenir.
- Bir vana veya termostat `window_open_guard_temp` (örn: 8.0°C) veya altındaysa ya da ilişkili pencere sensörü açıksa, kış donma korumasını ve Memento otomasyonunu bozmamak için ısıtma artırma talepleri `WindowOpenProtectionError` ile bloklanır.

### B. Korumalı Kaynak Kalkanı (`GuardRails`)
- `safety.protected_containers` (örn: `tailscale`, `pihole`, `homelab-agent-mesh`) üzerinde `stop`, `restart`, `pause` işlemleri kesinlikle yasaktır; çağrıldığında `ProtectedResourceViolationError` fırlatılır.
- Aynı koruma `safety.protected_vm_ids` ile Proxmox misafirleri için de geçerlidir.

### C. İki Aşamalı Onay Mekanizması (Two-Phase Commit & Multi-Interrupt)
- `safety.destructive_tools_requiring_approval` listesinde tanımlı işlemler (`restart_container`, `reboot_node`, `delete_automation` vb.) çağrıldığında Strands `Confirm` müdahalesi tetiklenir.
- Sistem istemciye `{"type": "intervention_required", "approval_id": "..."}` SSE paketi gönderir ve işlemi duraklatır.
- İşlem ancak kullanıcı `/api/v1/interventions/approve` uç noktasına `{"approval_id": "...", "approved": true}` gönderdiğinde kaldığı yerden devam eder.
- **Multi-Interrupt State Resumption:** Tek promptta birden fazla yıkıcı işlem istendiğinde (örn. container restart + Proxmox LXC reboot), sistem hem ardışık (sequential) hem eşzamanlı (batch) onay kuyruğunu (`resolved_interrupt_responses`) korur; her onay çözüldüğünde sonraki adımı aksatmadan yürütür.

### D. Bellek Zehirlenmesi Kalkanı & Tekrar Eşiği (Anti-Poisoning & Memory Drift Guard)
- Geçici ağ/zaman aşımı hataları (`TimeoutError`, `ConnectionError`, `circuit_open`, `zaman aşımı`) `MemoryPoisoningError` ile `lessons_learned` tablosuna yazılması kesinlikle engellenir.
- Ajan tarafından öğrenilen semantik kurallar enjekte edilmeden önce en az `agent_min_occurrences` (varsayılan: 2) kez gözlemlenmelidir; ilk gözlem aday (`active=False`) olarak bekletilir.

### E. Asenkron I/O ve Paralel Docker Telemetrisi
- Çoklu konteyner telemetri sorguları (`_list_containers_sync`) `ThreadPoolExecutor` ve `infra.docker_stats_concurrency` ile paralel toplanır.
- SQLite başlatma ve semantik skorlama `asyncio.to_thread` üzerinden izole edilerek FastAPI SSE event loop'u sıfır bloklama ile çalışır.

---

## 4. Dizin Yapısı ve Dosya Haritası

```text
homelab-agent-mesh/
├── .env.example                     # Gizli veri içermeyen ortam değişkeni şablonu
├── .gitignore                       # .env, data/*.db, settings.yaml dışlayıcı
├── pyproject.toml                   # Hatchling build & bağımlılıklar (strands-agents, mcp, fastapi)
├── Dockerfile                       # Multi-stage <80 MB RSS hedefli Docker imajı
├── docker-compose.yml               # Yerel montajlar (/var/run/docker.sock, data, config)
├── README.md                        # Mimari dökümantasyonu ve cURL örnekleri
├── config/
│   └── settings.example.yaml        # Domain, güvenlik sınırları ve ajan şablonu
├── tests/
│   └── test_orchestrator.py         # Mypy strict & pytest uçtan uca doğrulama testleri
└── src/
    ├── __init__.py                  # Paket versiyonu (1.0.0)
    ├── main.py                      # FastAPI başlatıcı, lifecycle yöneticisi ve CLI
    ├── core/
    │   ├── config.py                # pydantic-settings tabanlı YAML + .env yükleyici
    │   ├── events.py                # Typed SSE olay modelleri ve asenkron QueueEventSink
    │   ├── exceptions.py            # Hiyerarşik domain hataları (CircuitOpen, GuardViolation)
    │   ├── guards.py                # Devre kesici (CircuitBreaker), HVAC ve Docker kalkanları
    │   └── interfaces.py            # BaseStateVerifier ve EventEmitter protokolleri (DIP)
    ├── memory/
    │   ├── base.py                  # BaseMemoryStore ve BaseEmbedder soyut arayüzleri
    │   └── reflection_store.py      # SQLite + Gemini / Local N-gram 3 katmanlı hafıza
    ├── tools/
    │   ├── ha_ws_adapter.py         # Home Assistant native async WebSocket & REST adaptörü
    │   └── infra_mcp_server.py      # FastMCP uyumlu psutil, Docker ve Proxmox araç kiti
    ├── agents/
    │   ├── factory.py               # Sub-agent plugin kayıt sistemi, InterventionHandler & hooks
    │   ├── ha_agent.py              # Home Assistant uzman alt-ajanı (stateless)
    │   ├── infra_agent.py           # Homelab altyapı uzman alt-ajanı (stateless)
    │   └── supervisor.py            # Root Supervisor Orkestratör (HITL & sliding memory)
    └── api/
        ├── schemas.py               # Pydantic HTTP şemaları
        └── routes.py                # /chat/stream, /interventions/approve ve /health rotaları
```

---

## 5. Projenin Güncel Durumu (2026-11-13 İtibarıyla)

- [x] Tüm modüller tamamen yazıldı, modüler ve `strict` tip uyumlu.
- [x] `mypy --strict src tests` kontrolünden 23/23 dosya başarıyla geçti (0 hata).
- [x] `pytest tests/test_orchestrator.py` ile 3 katmanlı hafıza, güvenlik kalkanları, bellek zehirlenmesi kalkanı, paralel telemetri ve çoklu onay (multi-interrupt) akışları doğrulandı (6/6 test passed).
- [x] 3 temel üretim ortamı sertleştirmesi (production hardening) tamamlandı:
  1. Memory Drift / Anti-Poisoning guard & occurrence threshold (2x)
  2. Async non-blocking I/O & parallel container stats (concurrency=4)
  3. Multi-interrupt sequential & batch state resumption
- [x] Git deposuna temiz ve mantıksal commit'ler olarak işlendi (asla gizli anahtar sızdırılmadı).
- [x] Kullanıcı yerel diski üzerinde `.env` ve `config/settings.yaml` dosyalarını oluşturdu.

---

## 6. Yeni Session Açıldığında Nereden Başlanmalı? (Operasyonel Rehber)

Yeni oturumda doğrudan **canlı test ve entegrasyon aşamasına** geçilir. Sırasıyla izlenecek adımlar:

1. **Ortam Dosyalarını Kontrol Et:**
   - `homelab-agent-mesh/.env` içinde `GEMINI_API_KEY`, `HA_BASE_URL` ve `HA_ACCESS_TOKEN` değerlerinin dolu olduğunu teyit et.
   - `homelab-agent-mesh/config/settings.yaml` içinde `protected_containers` ve `climate_zones` alanlarını kontrol et.

2. **Servisi Çalıştır:**
   ```bash
   cd homelab-agent-mesh
   source .venv/bin/activate # veya python -m venv .venv && pip install -e .
   python -m src.main
   ```
   *(Docker Compose alternatifi: `docker compose up --build`)*

3. **Canlı Doğrulama Testlerini Sırayla Koş:**
   - **Adım 1 (Sağlık):** `curl http://localhost:8080/api/v1/health`
   - **Adım 2 (Salt Okuma):** `curl -N -X POST http://localhost:8080/api/v1/chat/stream -H "Content-Type: application/json" -d '{"prompt": "Sistem durumunu kontrol et: Sunucu CPU/RAM durumunu ve kombinin durumunu oku."}'`
   - **Adım 3 (Kalkan Testi):** `curl -N -X POST http://localhost:8080/api/v1/chat/stream -H "Content-Type: application/json" -d '{"prompt": "Kombiyi 35 dereceye ayarla."}'` (Engellenmeli)
   - **Adım 4 (Onay Testi):** `curl -N -X POST http://localhost:8080/api/v1/chat/stream -H "Content-Type: application/json" -d '{"prompt": "immich konteynerini yeniden başlat."}'` (Duraklayıp `approval_id` vermeli)

