# Homelab & Smart Home Multi-Agent Mesh (AGENTS.md)
*Sürüm: v2.0.1 | Son Güncelleme: 2026-11-19 (Production Log Audit Hardening: A1 Log Hygiene, A2 Cover Assertion Policy & Voice Concurrency, A4 Timezone/20:00 Briefing, A5 Adaptive Debounce & Advisor Report Recovery)*

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
│       • GET/POST /api/v1/entities/{resolve,refresh,memory}                     │
│       • GET/POST/DELETE /api/v1/schedules                                      │
│       • POST /api/v1/voice                 • POST /api/v1/voice/announce       │
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
│  • resolve_entity / schedule_ha_action│  │                                     │
└─────────┬───────────────────┬─────────┘  └──────────────────┬──────────────────┘
          │                   │                               │
          ▼                   ▼                               ▼
┌──────────────────┐ ┌────────────────────────┐ ┌────────────────────────────────┐
│   ha-mcp Client  │ │  ha-custom-ws Adapter  │ │   homelab-infra-mcp Toolkit    │
│  (Harici SSE MCP)│ │ (Native Async WS)      │ │  • psutil Telemetri (CPU/RAM)  │
│  • Varlık arama  │ │ • EntityResolver (<15ms)│ │  • Docker SDK Socket Kontrolü  │
│  • Standart servis│ │ • ActionScheduler      │ │  • Proxmox VE API (LXC/VM)    │
│                  │ │ • raw event.fire       │ │  • Korumalı Konteyner Kalkanı  │
│                  │ │ • assert_entity_state  │ │                                │
└──────────────────┘ └────────────────────────┘ └────────────────────────────────┘
          │                   │                               │
          └───────────────────┴───────────────┬───────────────┘
                                              ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│        SQLITE KALICI BELLEK & OLAY TABANLI DURUM KATMANI (WAL / asyncio)       │
│  • ReflectionStore (3 Katman): Domain izolasyonu, is_core_invariant, Semantik  │
│  • EntityResolver + CoALA entity_memory: HA WS registry event sync + Türkçe kök│
│  • ActionScheduler (scheduled_actions): Yeniden başlatmaya dayanıklı zamanlayıcı│
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

### F. P1 — Olay Tabanlı `EntityResolver`, Türkçe Morfoloji & CoALA `entity_memory`
- **Sıfır Harici Bağımlılıklı Türkçe Kök Bulucu (`src/core/text_tr.py`):** `İ/ı` uyumlu normalizasyon, diyaktitik katlama, sondan eklemeli çekim eki budama ve ünsüz yumuşaması geri çevirme (`mutfağındaki ışıkları` → `mutfak`, `isik`), ünlüyle biten kök koruması (`klima`, `lamba`, `perde`).
- **4 Katmanlı Tazelik (`src/core/entity_resolver.py`):** Başlangıçta SQLite `entity_registry_snapshot` önbelleğinden anında yüklenir; arka planda Home Assistant WebSocket `entity_registry_updated`, `device_registry_updated`, `area_registry_updated`, `floor_registry_updated` olaylarını dinler; kopmada exponansiyel backoff ile yeniden senkronize olur; 6 saatte bir tam uzlaştırma ve HTTP 404 self-healing uygular.
- **Çok Aşamalı Çözümleme Hattı (`EntityResolver.resolve`):**
  1. **Stage 0:** Doğrudan kanonik `entity_id` ve `settings.yaml` içindeki statik `entity_aliases` (`0.99`).
  2. **Stage 1 & 2:** CoALA `entity_memory` SQLite tablosundan Beta-Bernoulli güven skorlu tam (`0.96+`) ve bulanık eşleşme (`entity_memory_exact`, `entity_memory_fuzzy`).
  3. **Stage 3:** Yerel deterministik HA Grounding Index skorlaması (`light<->switch` uyumu, oda/alan bonusu, domain önceliği; `update.*` gürültüsü hariç).
  4. **Stage 4:** Yalnızca Stage 0–3 belirsiz veya `<0.85` kaldığında ağ tabanlı semantik embedding (`entity_memory_semantic`) yedeği.
- **Doğrulanmış Öğrenme & Çift Yönlü Senkronizasyon:** `entity_memory` yalnızca `assert_entity_state` ile fiziksel olarak doğrulanan aksiyonlardan (`verified_action`) veya `remember_entity_alias` / `POST /api/v1/entities/memory` kullanıcı düzeltmelerinden öğrenir. HA'da entity yeniden adlandırılırsa (`entity_registry_updated`) ilişkili bellek kayıtları taşınır; silinirse `stale` işaretlenir.

### G. P2 — Kalıcı `ActionScheduler` (`src/core/scheduler.py` & `src/tools/schedule_tools.py`)
- Harici cron/APScheduler bağımlılığı olmadan saf `asyncio` + SQLite (`scheduled_actions`, WAL modu, `_exec_lock` muteksi) ile ertelenmiş eylemleri yürütür (`schedule_ha_action`, `list_scheduled_actions`, `cancel_scheduled_action` araçları ve `/api/v1/schedules` API'ı).
- **Kaza Kurtarma & Güvenli Yön Misfire Politikası (`safe_direction`):** Konteyner yeniden başlatıldığında `running` kalmış kayıtları kurtarır; `misfire_grace_sec` (varsayılan 300s) geçmiş işlerde yalnızca güvenli kapatma komutlarını (`turn_off`, `close_cover`, `lock`, `hvac_mode=off`) çalıştırır, açma/ısıtma komutlarını atlar.
- **Çalışma Anı Koruma & Actor-Critic Doğrulama:** Tetikleme anında HVAC/korumalı kaynak guard'larını yeniden denetler, servisi çağırır, `assert_state` ile fiziksel durumu doğrular ve başarısız olursa otomatik 1 kez yeniden dener (`attempts / max_attempts`). SSE akışına `schedule_created`, `schedule_fired`, `schedule_result` olaylarını yayar.

### H. P4 — Sesli Komut Geçidi (`/api/v1/voice`) & HomePod AirPlay TTS `Announcer` (`src/core/announcer.py`)
- **Türkçe Uyanma Öneki & TTS Temizleyici:** `strip_wake_prefix` (`Hey Kezban`, `Kezban`), `İ/ı` uyumlu biçimde baştaki tetik kelime ve noktalama işaretlerini ayırır; `clean_for_tts`, Markdown işaretlerini doğal tek paragraf konuşma metnine dönüştürür.
- **8 Saniye Senkron Bütçe & Arka Plan Devri:** `POST /api/v1/voice`, ajanı arka plan görevi olarak başlatır ve `voice.sync_timeout_sec` (varsayılan 8.0s) boyunca `asyncio.wait` ile bekler (görevi **iptal etmez**). Süre içinde biterse doğrudan `speech` (ve `announce=true` ise HomePod TTS sonucu) döner; aşarsa derhal `status="accepted_background"` ile onay cümlesi (`voice.deferred_ack_text`) döner ve görev tamamlandığında sonucu hedef odanın HomePod'una otomatik sesli okur.
- **Oda Bazlı Hoparlör Yönlendirme & Otomatik Zamanlayıcı Duyurusu:** `Announcer`, `voice.room_speakers`, `EntityResolver` (`media_player`) ve `voice.default_speaker` üzerinden hedef hoparlörü seçer; `tts.speak` (`tts.google_translate_en_com` veya yapılandırılan TTS varlığı) hata verirse otomatik olarak `tts.google_translate_say` yedeğine düşer. `ActionScheduler` `ScheduleResultEvent` olaylarını dinleyerek tamamlanan/başarısız/atlanan zamanlanmış işleri ilgili odadan duyurur.

---

## 4. Dizin Yapısı ve Dosya Haritası

```text
homelab-agent-mesh/
├── .dockerignore                    # Gizli .env, settings.yaml ve *.db dışlayıcı (CI/build kalkanı)
├── .env.example                     # Gizli veri içermeyen ortam değişkeni şablonu
├── .gitignore                       # .env, data/*.db, settings.yaml dışlayıcı
├── pyproject.toml                   # Hatchling build & bağımlılıklar (v2.0.0)
├── Dockerfile                       # Multi-stage <128 MB RSS hedefli Docker imajı
├── docker-compose.yml               # Yerel geliştirme montajları (/var/run/docker.sock, data, config)
├── README.md                        # Mimari dokümantasyonu ve cURL örnekleri
├── docs/
│   ├── architectural_decisions.md     # Mimari & tasarım kararları kataloğu (ADR-01 .. ADR-12)
│   ├── architecture.md              # Detaylı C4 & sekans diyagramları
│   ├── architecture_roadmap.md      # v2.0 Plugin SDK, Tracing & MQTT yol haritası
│   ├── deployment.md                # P3: Çift repolu GitOps, GHCR & SOPS+age dağıtım stratejisi
│   ├── ha-integration.md            # P4/P5: HA Assist boru hattı, STT/TTS izolasyonu ve çift yönlü bağımlılık haritası
│   └── open-discussion.md           # Açık performans tartışmaları ve iyileştirme notları
├── config/
│   ├── settings.example.yaml        # Domain, güvenlik sınırları ve ajan şablonu
│   ├── thermal_overrides.example.yaml # Opsiyonel termal fizik/bina zarfı override şablonu
│   └── lovelace_thermal_dashboard.yaml # Termal Dijital İkiz Lovelace pano şablonu
├── tests/
│   ├── test_orchestrator.py         # Mypy strict & pytest uçtan uca doğrulama testleri
│   ├── test_neonplan_adapter.py     # NeonPlan 3D geometri, duvar komşuluğu ve sensör eşleme testleri
│   ├── test_thermal_engine.py       # Psikrometri, RC soğuma fit, şok/rebound ve simülasyon testleri
│   └── test_thermal_agent.py        # HAHistoryClient, ThermalTwinService ve ThermalSubAgent testleri
└── src/
    ├── __init__.py                  # Paket versiyonu (2.0.0)
    ├── main.py                      # FastAPI başlatıcı, lifecycle yöneticisi ve CLI
    ├── core/
    │   ├── announcer.py             # P4: HomePod AirPlay TTS Announcer & uyanma eki temizleyici
    │   ├── config.py                # pydantic-settings tabanlı YAML + .env yükleyici (voice & thermal dahil)
    │   ├── entity_resolver.py       # P1: Olay tabanlı HA Grounding Index & CoALA entity_memory
    │   ├── events.py                # Typed SSE olay modelleri ve asenkron QueueEventSink
    │   ├── exceptions.py            # Hiyerarşik domain hataları (CircuitOpen, GuardViolation)
    │   ├── guards.py                # Devre kesici (CircuitBreaker), HVAC ve Docker kalkanları
    │   ├── interfaces.py            # BaseStateVerifier ve EventEmitter protokolleri (DIP)
    │   ├── logging_utils.py         # v2.0.1: Gizli URL/token maskeleme, healthcheck & HTTPX log hijyeni
    │   ├── neonplan_adapter.py      # v2.0: NeonPlan 3D JSON ayrıştırıcı & ZoneTopology grafiği
    │   ├── psychrometrics.py        # v2.0: Magnus-Tetens mutlak nem, çiğlenme noktası & entalpi
    │   ├── scheduler.py             # P2: SQLite + asyncio kalıcı ActionScheduler
    │   ├── text_tr.py               # Türkçe metin normalizasyonu, kök bulucu ve domain ipucu çıkarıcı
    │   └── thermal_engine.py        # v2.0: Deterministik Grey-Box RC fizik & geçmiş analiz motoru
    ├── memory/
    │   ├── base.py                  # BaseMemoryStore ve BaseEmbedder soyut arayüzleri
    │   ├── reflection_store.py      # SQLite + Gemini / Local N-gram 3 katmanlı hafıza
    │   └── thermal_store.py         # v2.0: SQLite WAL zone modelleri ve termal gözlem deposu
    ├── tools/
    │   ├── ha_history_client.py     # v2.0: Async HA /api/history/period okuyucu & sanal sensör yayıncısı
    │   ├── ha_ws_adapter.py         # HA WebSocket/REST adaptörü, resolve_entity & assert_entity_state
    │   ├── infra_mcp_server.py      # FastMCP uyumlu psutil, Docker ve Proxmox araç kiti
    │   ├── schedule_tools.py        # P2: schedule_ha_action / list / cancel Strands araçları
    │   └── thermal_tools.py         # v2.0: ThermalTwinService & Strands termal araçları
    ├── agents/
    │   ├── factory.py               # Sub-agent plugin kayıt sistemi, InterventionHandler & hooks
    │   ├── ha_agent.py              # Home Assistant uzman alt-ajanı (stateless)
    │   ├── infra_agent.py           # Homelab altyapı uzman alt-ajanı (stateless)
    │   ├── thermal_agent.py         # v2.0: Mekânsal Termal Dijital İkiz uzman alt-ajanı (stateless)
    │   └── supervisor.py            # Root Supervisor Orkestratör (HITL & sliding memory)
    └── api/
        ├── schemas.py               # Pydantic HTTP şemaları (chat, entities, schedules, voice)
        └── routes.py                # /chat/stream, /interventions/*, /entities/*, /schedules, /voice/*, /health
```

---

## 5. Projenin Güncel Durumu (2026-11-19 İtibarıyla)

- [x] P1 (`EntityResolver` + Türkçe Kök Bulucu + CoALA `entity_memory`) ve P2 (`ActionScheduler`) modülleri yazıldı, Strands ajanlarına ve FastAPI uç noktalarına entegre edildi.
- [x] `mypy --strict src tests` kontrolünden **39/39 dosya** 0 hata ile geçti; CI `ruff check` (`E,F,I,UP`) temiz; **28/28 pytest** geçiyor.
- [x] Canlı Home Assistant (808 entity) üzerinde konteyner içi uçtan uca doğrulama tamamlandı:
  - `/api/v1/entities/resolve` ile `mutfak isigi` (`9.79 ms`, `entity_memory_exact`), `oturma odasi isik` (`16.72 ms`), `yatak odasindaki klimayi` (`15.09 ms`) sorguları harici embedding çağrısı olmadan tek hamlede çözümlendi.
  - `/api/v1/chat/stream` üzerinden `"Mutfaktaki isiklari ac, 15 saniye sonra geri kapat"` senaryosunda anında açma + `assert_entity_state` + CoALA `entity_memory` öğrenmesi + 15s sonra kalıcı zamanlayıcı ile kapatıp doğrulama (`status: "done"`, `verified: true`) uçtan uca teyit edildi.
- [x] P3 GitOps kurulumu tamamlandı ve servis LXC 900 (`MuhendisBey/homelab-ops`, `SOPS + age + justfile`, Private GHCR `read:packages` PAT) üzerine taşındı.
- [x] P4 (`v1.3.0`) & Hız Optimizasyonu (`v1.3.1`): `POST /api/v1/voice` (8 sn zaman aşımı bütçesi + arka plan devamlılığı), `POST /api/v1/voice/announce`, `Announcer` ve birleşik Pre-Grounding (`prepare_prompt_context`) + `GeminiEmbedder` LRU önbelleği canlıda doğrulandı (~14.8 sn -> ~2.6–4.0 sn).
- [x] **v2.0.0 (Spatial Thermal Digital Twin & Grey-Box RC Estimator):** `NeonPlanAdapter` (Shoelace alan/hacim, duvar komşuluğu, `<0.5m` radyatör yakınlık sınıflandırması), `psychrometrics.py`, `ThermalPhysicsEngine` (eTRV radyasyon sapması füzyonu, gece soğuma $k_{\text{loss}}$ fit, havalandırma şoku/rebound & dirsek noktası, hidrolik denge ve parametrik simülasyon), `ThermalStore` (SQLite WAL), `HAHistoryClient`, `ThermalSubAgent` ve canlı Home Assistant `thermal-twin` Lovelace panosu doğrulandı.
- [x] **v2.0.1 (Canlı Log Denetimi İyileştirmeleri — A1, A2, A4, A5):**
  - **A1:** `src/core/logging_utils.py` ile `/private_***` / `Bearer ***` / gizli query maskeleme, `200 OK` `/api/v1/health` filtresi ve `httpx`/`httpx2`/`httpcore` `WARNING` seviyesi.
  - **A2:** `home_assistant.domain_assert_policies` (`cover.*` için `15s` timeout, `1000ms` poll, `closing`/`opening` ara durumları — `expected_attributes` kontrolünü baypas etmeden) ve `/api/v1/voice` meşgul oturumlarda `409` yerine `__bg_<hex>` overflow oturumu (HITL `pending_approvals` 409 koruması saklı).
  - **A4:** `AppConfig.timezone` (`ZoneInfo` doğrulamalı, ops'ta `Europe/Amsterdam`) + `daily_briefing_time: "20:00"` (yerel saatte akşam 8; `morning_briefing_time` geriye uyumlu) + `_in_night_window` yerel saat hesabı.
  - **A5:** `EntityResolver` açılışta çift senkronizasyon eliminasyonu + HA restart sonrası 60 sn adaptif `10s` boot debounce; `thermal_advisor_reports` SQLite tablosuyla restart-dayanıklı günlük brifing tarihi ve HA restart sonrası eksik danışman raporunun otomatik yeniden yayını.
- [ ] Sonraki tartışma & geliştirme başlıkları: Ertelenen **A3** (`entity_memory` belirsiz ifade/kanonik ID öğrenme bekçisi) ve **A6** (saf Python RC soğuma fiti ile `<80 MB` RSS hedefi + `neonplan.json` mount yolu) kararları (`docs/open-discussion.md`); ayrıca P5 (`custom_components/kezban` + Apple Watch Assist: 👉 **[docs/ha-integration.md](docs/ha-integration.md)**).

---

### Operasyonel Dersler (Production Lessons Learned — 2026-11-19 Canlı Log Denetimi)
1. **Log Hijyeni (`ADR-13`):** Harici HTTP/MCP istemci kütüphaneleri (`httpx`, `httpx2`, `httpcore`) `INFO` düzeyinde gizli path/token URL'lerini basabilir; root ve uvicorn logger/handler zincirinde merkezi `SecretRedactingFilter` zorunludur ve rutin `200 OK` healthcheck logları bastırılmalıdır.
2. **Yavaş Aktüatörler & Ses Eşzamanlılığı (`ADR-14`):** Motorlu perde/güneşlik (`cover.*`) gibi 15–25 sn süren donanımlara sabit 6s/400ms doğrulama uygulamak sahte hata + LLM retry fırtınası yaratır ve sesli oturum kilidini uzatarak sonraki sesli komutları `409 Conflict`'e düşürür. Ara durumlar yalnızca config-driven `DomainAssertionPolicy` ile ve açık `expected_attributes` korunarak kabul edilmeli; meşgul ses oturumları HITL onayı beklemiyorsa izole overflow oturumuna yönlendirilmelidir.
3. **Konteyner Saat Dilimi & Çift Taraflı Restart Dayanıklılığı (`ADR-15`):** Docker konteynerleri varsayılan `UTC` çalıştığından brifing ve gece soğuma pencereleri asla OS yerel saatine güvenmemeli, `AppConfig.timezone` (`ZoneInfo`) ile açıkça yönetilmelidir. Ayrıca HA restartı sanal sensörleri (`advisor_report`) siler ve boot registry fırtınası yaratırken, ajan konteyneri restartı günlük zamanlama durumunu sıfırlar — bu nedenle hem `EntityResolver` adaptif boot debounce uygulanmalı hem son danışman raporu SQLite'ta tutulup eksikse ilk `run_live_tick`'te geri basılmalıdır.
4. **Açık/Ertelenen Konular:** `entity_memory` belirsiz tek kelime/kanonik ID kaydı (**A3**) ile `scipy`/`numpy` bellek ayak izi ve `neonplan.json` mount yolu (**A6**) ayrıntıları **[docs/open-discussion.md](docs/open-discussion.md)** içinde kayıtlıdır; onay olmadan değiştirilmemelidir.

---

## 6. Deployment & Çok Repolu GitOps Rehberi (`homelab-ops`)

- **Public vs. Private Ayrımı:**
  - Bu depo (`MuhendisBey/homelab-agent-mesh`, **public**) yalnızca uygulama kodu, testler ve container build CI/CD iş akışını (`.github/workflows/ci.yml`) içerir; konteyner imajı **private GHCR (`ghcr.io/muhendisbey/homelab-agent-mesh`)** paketine `linux/amd64` olarak basılır.
  - Üretim yapılandırması, gerçek `settings.yaml`, SOPS + `age` ile şifreli `.env` sırları (`secrets.env.sops`), `compose.yaml` ve `justfile` tarifleri özel **`MuhendisBey/homelab-ops` (private)** reposunda yaşar (yerelde çalışma alanı kökündeki gitignored `./homelab-ops/` dizini).
  - Ayrıntılı strateji, sekans diyagramları, firewall (`DOCKER-USER`) ve LXC kurulum adımları için 👉 **[docs/deployment.md](docs/deployment.md)** dosyasına başvurun.
  - Home Assistant Assist boru hattı, STT/TTS izolasyonu ve `custom_components/kezban` entegrasyon sınırı için 👉 **[docs/ha-integration.md](docs/ha-integration.md)** dosyasına başvurun.

---

## 7. Yeni Session Açıldığında Nereden Başlanmalı? (Operasyonel Rehber)

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

