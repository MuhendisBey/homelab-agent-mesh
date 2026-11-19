# Open Discussions & Mimari Notlar (`homelab-agent-mesh`)

Bu doküman, canlı kullanımda gözlemlenen performans darboğazlarını, mimari iyileştirme fikirlerini ve ileri tarihli tasarım kararlarını takip etmek için canlı kayıt defteri olarak tutulur.

> [!IMPORTANT]
> **Katı Gizlilik İlkesi:** Bu doküman public depoda yer alır. Gerçek yerel ağ (LAN) IP adresleri, API token'ları, kişisel cihaz kimlikleri veya `homelab-ops/` içerikleri burada paylaşılmaz.

---

## 1. Eylem Komutlarında Çoklu LLM Turu Gecikmesi (`> 8 sn` Senkron Timeout)

- **Durum:** Açık (Gözlemlendi — `v1.3.3` itibarıyla analiz edildi)
- **Hedef Bileşenler:** [`supervisor.py`](../src/agents/supervisor.py), [`ha_agent.py`](../src/agents/ha_agent.py), [`factory.py`](../src/agents/factory.py), `src/tools/ha_ws_adapter.py`, `src/memory/reflection_store.py`

### 1.1. Problem Özeti
`voice.sync_timeout_sec: 8.0` (sesli asistanda bekletmeme eşiği) altındayken **salt-okunur durum sorguları** yaklaşık **4.1 sn** sürerken, basit fiziksel **durum değiştirme komutları** (`"Mutfak ışığını aç"`, `"kapat"`) **11–14 sn** sürmekte ve `8.0 sn` dolduğu için istemciye senkron yanıt yerine *"Üzerinde çalışıyorum..."* arka plan teslim mesajı düşmektedir.

### 1.2. Kök Neden & Gözlemlenen Akış
- **Salt-okunur sorgu (`"Mutfak ışıklarının durumu nedir?"` — ~4.15 sn):**
  - `prepare_prompt_context` varlığı ve canlı durumu `~12 ms` içinde çözer.
  - [`_SUPERVISOR_BASE_PROMPT`](../src/agents/supervisor.py#L85-L90) kuralı gereği `Supervisor`, ön-çözümlenmiş canlı veriyle `home_assistant_agent` alt ajanını çağırmadan **tek LLM turunda** yanıt üretir.
- **Durum değiştiren eylem (`"Mutfak ışığını aç"` ~13.8 sn / `"kapat"` ~11–12 sn):**
  1. `0.00 sn` — `Supervisor` LLM Turu 1 (`~2 sn`): İsteği `home_assistant_agent` alt ajanına delege eder ([`supervisor.py`](../src/agents/supervisor.py#L91-L94)).
  2. `~2.2 sn` — Alt ajan açılış kancası ([`DomainMemoryAndCriticHooks.on_before_invocation`](../src/agents/factory.py#L262-L279)): `ReflectionStore.retrieve()` için görev metnine `gemini-embedding-2` HTTP isteği atılır + `ha-mcp` araç bağlamı hazırlanır.
  3. `~4.5–9.9 sn` — `HASubAgent` LLM Turu 2 (`~2.5 sn`, gerektiğinde ön çözümleme/`resolve_entity` ile ekstra tur): `call_ha_service` çağrılır (gözlem: ilk komutta `+9.98 sn` içinde `switch.turn_on`, ikinci komutta `+7.84 sn` içinde `switch.turn_off` tetiklendi).
  4. `~10–12 sn` — `HASubAgent` LLM Turu 3 (`~2.5 sn`): Actor-Critic zorunluluğu nedeniyle `assert_entity_state` ayrı bir model turuyla çağrılır ([`ha_agent.py`](../src/agents/ha_agent.py#L33-L39)).
  5. `~12–14 sn` — `HASubAgent` özet turu + `Supervisor` final yanıt turu (`2` ek LLM turu).

Sonuç olarak tek bir aç/kapat komutu **4–5 sıralı Gemini Flash çağrısına** yayıldığı için toplam süre `8.0 sn` bütçesini aşar.

### 1.3. Muhtemel Çözüm Seçenekleri (Hedef: `< 4 sn`)

1. **`call_ha_service` İçinde Otomatik Durum Doğrulaması (`auto_assert`):**
   - `turn_on`, `turn_off`, `toggle`, `set_temperature` gibi durum değiştiren servisler çağrıldığında, `assert_state` kontrolü **aynı tool çağrısı içinde** otomatik çalıştırılıp sonucu tool çıktısına eklenir.
   - `HASubAgent`'ın `assert_entity_state` için harcadığı **ayrı LLM turunu (~2.5 sn)** sıfırlar; Actor-Critic doğrulama garantisi korunur.
2. **Ön-Çözümlenmiş Tek Cihazlarda `Supervisor` Hızlı Yolu (`Single-Hop Fast-Path`):**
   - `kesin_eşleşme=evet` (veya oturum bağlamından tek varlık belliyse), `Supervisor` isteği alt ajana aktarıp 3 ekstra LLM turu harcamak yerine güvenlik geçidinden (`SafetyAndApprovalGate` + otomatik `assert_state`) geçerek doğrudan tek güvenli servis aracını çalıştırabilir.
   - Toplam LLM tur sayısını **4–5 turdan 2 tura (~3.5 sn)** düşürür.
   - *Dikkat edilecek nokta:* `HASubAgent` üzerindeki özel HA kural enjeksiyonu (`domain="ha"`) ve güvenlik/klima sınır korumalarının bu hızlı yolda da birebir uygulanması gerekir.
3. **`ReflectionStore` Boş/Yalnızca Çekirdek Kural Durumunda Embedding Atlaması:**
   - Veritabanında hiç anlamsal kural (`is_core_invariant=False`) yoksa (yalnızca çekirdek kurallar `is_core_invariant=True` mevcutsa), her alt ajan çağrısında `gemini-embedding-2` API'sine gidilmesini atlayıp `~500 ms` tasarruf sağlanabilir.

### 1.4. Açık Sorular & Bağlantılı Notlar
- Hızlı yol aktif edildiğinde çoklu niyet içeren karmaşık komutlar (örn. *"Mutfak ışığını aç ve 5 dakika sonra klimayı 22 dereceye ayarla"* veya *"Işığı aç ve Docker loglarına bak"*) yine mevcut `Supervisor -> SubAgent` delegasyon yoluna bırakılmalıdır.
- İleri aşamada `docs/architecture_roadmap.md` v2.0 kapsamındaki tracing altyapısı eklendiğinde her LLM turu ve tool çağrısı span bazlı ölçülerek regresyon takibi yapılabilir.

---

## 2. CoALA `entity_memory` Belirsiz İfade & Kanonik ID Öğrenme Politikası (A3 — Tartışmaya Açık)

- **Durum:** Açık (2026-11-19 Canlı Log ve `reflections.db` Denetimi)
- **Hedef Bileşen:** [`entity_resolver.py`](../src/core/entity_resolver.py) (`register_pending_resolution` & Stage 0a)

### 2.1. Problem Özeti
Canlı veritabanı incelemesinde iki tür istenmeyen kayıt gözlemlendi:
1. Oda belirtmeyen tek kelimelik belirsiz ifadeler (ör. `"güneşlik"`), farklı odalardaki birden fazla varlık için (`cover.study_shade`, `cover.kitchen_shade`, `cover.living_room_shade` benzeri çoklu adaylar) ayrı ayrı `entity_memory` tablosuna öğrenilmiştir. Gelecekte oda bağlamı olmadan aynı ifade söylendiğinde Stage 1 (`entity_memory_exact`) çakışan yüksek skorlu adaylar üretebilir.
2. Zaten kanonik `entity_id` biçiminde gelen sorgular (`light.<room>_<device>`), Stage 0a'da `match_source="registry_grounding"` etiketiyle döndüğü için `static_alias` filtresini geçip gereksiz yere `entity_memory` satırı ve embedding çağrısı oluşturmaktadır.

### 2.2. Tartışılacak Çözüm Noktaları
- `register_pending_resolution()` içine `if result.ambiguous or not result.resolved: return` ve `match_source="canonical_id"` hariç tutma bekçileri eklenmesi;
- Ancak kısa tek-kelimelik komutların (`room` bağlamı enjekte edilen sesli komutlar dahil) hangi koşullarda oda-bağımsız alias olarak öğrenilip hangilerinde engelleneceğinin kullanıcı deneyimiyle birlikte netleştirilmesi ve mevcut SQLite satırlarının kontrollü temizliği.

---

## 3. Konteyner RAM Bütçesi (`~180 MB` vs. `<80 MB` Hedef), `scipy`/`numpy` Bağımlılığı & `neonplan.json` Mount Noktası (A6 — Tartışmaya Açık)

- **Durum:** Açık (2026-11-19 Canlı Konteyner Metrik Denetimi)
- **Hedef Bileşenler:** [`thermal_engine.py`](../src/core/thermal_engine.py), [`pyproject.toml`](../pyproject.toml), [`docker-compose.yml`](../docker-compose.yml) (ve ops `compose.yaml`)

### 3.1. Problem Özeti
- Canlı konteynerde `VmRSS` yaklaşık **`180.6 MB`** (`VmPeak: ~818 MB`) ölçülmüştür (`AGENTS.md` hedefi `< 80 MB` / `< 128 MB`). `v2.0.0` ile eklenen `scipy` + `numpy`, yalnızca günde bir kez gece soğuma eğrisi ($T(t) = T_{\text{out}} + (T_0 - T_{\text{out}})e^{-k t}$) fit etmek için kullanılmakta, ancak kalıcı bellek tabanını ve Docker imaj boyutunu büyütmektedir.
- Ayrıca dışarıdan `/app/data/neonplan.json:ro` bind-mount edilmesi, kalıcı `./data/` dizini içinde 0 byte'lık sahte bir mountpoint dosyası bırakmaktadır.

### 3.2. Tartışılacak Çözüm Noktaları
- Tek parametreli Newton soğuma fitini (`_fit_newton_cooling`) `scipy.optimize.curve_fit` yerine saf Python (`math.log` tabanlı lineer regresyon + 1B golden-section refinement) ile değiştirip `scipy`/`numpy` bağımlılıklarını kaldırma fizibilitesi (~60–80 MB RSS kazancı beklentisi);
- `neonplan.json` varsayılan yolunu (`NEONPLAN_JSON_PATH`) `/app/config/neonplan.json` konumuna taşıyarak `./data/` hacmini salt veritabanı dosyalarına (`reflections.db*`) özgüleme.

---

## 4. Harici Donanım Telemetri Kesintilerinde Termal İkiz Sağlık İşaretlemesi

- **Durum:** Açık (2026-11-19 HA `error_log` & `unavailable` Varlık Denetimi)
- **Hedef Bileşenler:** [`thermal_engine.py`](../src/core/thermal_engine.py), [`ha_history_client.py`](../src/tools/ha_history_client.py)

### 4.1. Gözlem & Öneri
- Evdeki termostat/eTRV cihazlarından bazıları zaman zaman Home Assistant'ta `unavailable` durumuna düştüğünde (ör. vana pozisyonu veya aktüel sıcaklık sensörü okunamadığında), Termal İkiz varsayılan/eksik veriyle sessizce hesaplama yapmaya devam etmektedir.
- İleri adımda `sensor.thermal_twin_<zone>_stats` özniteliklerine `sensor_health: "ok" | "degraded"` ve `unavailable_entities: [...]` eklenerek Lovelace panosu ve `ThermalSubAgent` tarafında veri kalitesi şeffaf biçimde gösterilebilir.

