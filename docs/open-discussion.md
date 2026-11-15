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
