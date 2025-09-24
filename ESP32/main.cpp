#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>

// ----------- Wi-Fi -----------
const char* WIFI_SSID     = "3D_KAMIN";
const char* WIFI_PASSWORD = "10101010";

// ----------- MQTT -----------
const char* MQTT_HOST = "45.144.221.176";
const uint16_t MQTT_PORT = 1883;
const char* MQTT_USER = "esphkaf";
const char* MQTT_PASS = "S159357";

// ----------- ПИН для сброса привязки (длинное нажатие 5с) -----------
#define PAIR_RESET_PIN 0
#define PAIR_RESET_HOLD_MS 5000

// ----------- Серийный код устройства (пока константа) -----------
const char* DEVICE_CLAIM_CODE = "000000";  // поменяй на свой

// Если оставить пустым — сформируется как "dry-<MAC>"
String DEVICE_ID = "";

// ----------- Топики -----------
char TOPIC_CMD[128];
char TOPIC_STATE[128];
char TOPIC_AVAIL[128];
char TOPIC_PAIR[128];         // входящая привязка: {"code":"..."}
char TOPIC_PAIR_RESULT[128];  // результат привязки: {"ok":true} / {"error":"bad_code"}
char TOPIC_PAIR_INDEX[128];   // РЕТЕЙН-индекс: pair/index/<CODE> -> <DEVICE_ID>

// ----------- Состояние устройства -----------
bool onState = false;
String programState = "one"; // one|two|three|four|five|six
bool soundEnabled = false;
float currentTempC = 0.0f;
uint32_t timeLeftSeconds = 0;

// Привязка
Preferences prefs;
bool isPaired = false;

// Разрешённые программы
const char* ALLOWED_PROGRAMS[] = {"one","two","three","four","five","six"};
bool isAllowedProgram(const String& v) {
  for (auto p : ALLOWED_PROGRAMS) if (v == p) return true;
  return false;
}

// ----------- Глобальные объекты -----------
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

// ================== Утилиты ==================
String macNoColons() {
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  return mac;
}

void buildDeviceIdIfEmpty() {
  if (DEVICE_ID.length() == 0) {
    DEVICE_ID = "dry-" + macNoColons();
  }
}

void buildTopics() {
  snprintf(TOPIC_CMD,   sizeof(TOPIC_CMD),   "devices/%s/cmd",           DEVICE_ID.c_str());
  snprintf(TOPIC_STATE, sizeof(TOPIC_STATE), "devices/%s/state",         DEVICE_ID.c_str());
  snprintf(TOPIC_AVAIL, sizeof(TOPIC_AVAIL), "devices/%s/availability",  DEVICE_ID.c_str());
  snprintf(TOPIC_PAIR,  sizeof(TOPIC_PAIR),  "devices/%s/pair",          DEVICE_ID.c_str());
  snprintf(TOPIC_PAIR_RESULT, sizeof(TOPIC_PAIR_RESULT), "devices/%s/pair_result", DEVICE_ID.c_str());
  // Индекс по одному коду (ретейн): pair/index/<CODE> -> <DEVICE_ID>
  snprintf(TOPIC_PAIR_INDEX, sizeof(TOPIC_PAIR_INDEX), "pair/index/%s", DEVICE_CLAIM_CODE);
}

void publishAvailability(const char* status) {
  mqtt.publish(TOPIC_AVAIL, status, true);
}

void publishPairIndex(bool set) {
  // set=true  -> записать индекс (payload = DEVICE_ID, retained)
  // set=false -> очистить индекс (payload="", retained)
  if (set) {
    mqtt.publish(TOPIC_PAIR_INDEX, DEVICE_ID.c_str(), true);
  } else {
    mqtt.publish(TOPIC_PAIR_INDEX, "", true); // очистка ретейна
  }
}

void publishPairResultOk() {
  StaticJsonDocument<64> doc;
  doc["ok"] = true;
  char buf[64];
  serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_PAIR_RESULT, buf, false);
}

void publishPairResultError(const char* err) {
  StaticJsonDocument<96> doc;
  doc["ok"] = false;
  doc["error"] = err;
  char buf[96];
  serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_PAIR_RESULT, buf, false);
}

void publishState() {
  StaticJsonDocument<256> doc;
  doc["on"] = onState;
  doc["program"] = programState;
  doc["sound"] = soundEnabled;
  doc["temp_c"] = currentTempC;
  doc["time_left"] = timeLeftSeconds;
  doc["paired"] = isPaired;
  doc["pair_required"] = !isPaired;
  doc["ts"] = (uint32_t)(millis() / 1000);

  char buf[256];
  serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_STATE, buf, true);
}

void savePaired(bool v) {
  isPaired = v;
  prefs.putBool("paired", v);
  publishState();
  // вести индекс только пока не привязано
  if (mqtt.connected()) {
    publishPairIndex(!v);
  }
}

void handlePairJson(const char* payload, size_t len) {
  StaticJsonDocument<128> doc;
  auto err = deserializeJson(doc, payload, len);
  if (err) { publishPairResultError("bad_json"); return; }

  const char* code = doc["code"] | "";
  if (!code || strlen(code) == 0) {
    publishPairResultError("empty_code");
    return;
  }

  if (isPaired) {
    publishPairResultError("already_paired");
    return;
  }

  if (strcmp(code, DEVICE_CLAIM_CODE) == 0) {
    savePaired(true);
    publishPairResultOk();
    publishAvailability("online");
    // индекс очистится в savePaired(true)
  } else {
    publishPairResultError("bad_code");
  }
}

void applyCommandJson(const char* payload, size_t len) {
  if (!isPaired) {
    publishPairResultError("pair_required");
    return;
  }

  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, payload, len);
  if (err) {
    Serial.printf("JSON parse error: %s\n", err.c_str());
    return;
  }

  bool changed = false;

  if (doc.containsKey("on")) {
    bool v = doc["on"];
    if (onState != v) {
      onState = v;
      // TODO: управление железом
      Serial.printf("Applied on=%s\n", onState ? "true" : "false");
      changed = true;
    }
  }

  if (doc.containsKey("program")) {
    String p = String((const char*)doc["program"]);
    if (!isAllowedProgram(p)) {
      Serial.printf("Invalid program: %s\n", p.c_str());
    } else if (programState != p) {
      programState = p;
      // TODO: применить логику программы
      Serial.printf("Applied program=%s\n", programState.c_str());
      changed = true;
    }
  }

  if (doc.containsKey("sound")) {
    bool v = doc["sound"];
    if (soundEnabled != v) {
      soundEnabled = v;
      // TODO: переключить звуковую индикацию
      Serial.printf("Applied sound=%s\n", soundEnabled ? "true" : "false");
      changed = true;
    }
  }

  if (changed) publishState();
}

// ================== MQTT ==================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  Serial.printf("MQTT IN [%s]: %.*s\n", topic, length, (const char*)payload);

  if (strcmp(topic, TOPIC_CMD) == 0) {
    applyCommandJson((const char*)payload, length);
    return;
  }
  if (strcmp(topic, TOPIC_PAIR) == 0) {
    handlePairJson((const char*)payload, length);
    return;
  }
}

bool connectMqtt() {
  String clientId = DEVICE_ID + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);

  bool ok = mqtt.connect(
    clientId.c_str(),
    (MQTT_USER && strlen(MQTT_USER)) ? MQTT_USER : nullptr,
    (MQTT_PASS && strlen(MQTT_PASS)) ? MQTT_PASS : nullptr,
    TOPIC_AVAIL, 0, true, "offline"
  );
  if (!ok) return false;

  mqtt.subscribe(TOPIC_CMD);
  mqtt.subscribe(TOPIC_PAIR);

  publishAvailability("online");
  // если ещё не привязано — публикуем индекс (ретейн)
  publishPairIndex(!isPaired);
  publishState();
  return true;
}

void ensureMqtt() {
  static uint32_t lastTry = 0;
  if (mqtt.connected()) return;

  uint32_t now = millis();
  if (now - lastTry < 3000) return;
  lastTry = now;

  Serial.print("Connecting MQTT... ");
  if (connectMqtt()) Serial.println("OK");
  else Serial.printf("FAIL rc=%d\n", mqtt.state());
}

// ================== Сброс привязки кнопкой ==================
void checkPairResetButton() {
  static uint32_t pressStart = 0;
  int lvl = digitalRead(PAIR_RESET_PIN); // LOW при нажатии (BOOT)

  if (lvl == LOW) {
    if (pressStart == 0) pressStart = millis();
    if (isPaired && (millis() - pressStart > PAIR_RESET_HOLD_MS)) {
      Serial.println("[PAIR] long press -> unpair");
      savePaired(false);  // republish state и индекс
      delay(500);
      while (digitalRead(PAIR_RESET_PIN) == LOW) delay(10);
      pressStart = 0;
    }
  } else {
    pressStart = 0;
  }
}

// ================== Wi-Fi ==================
void setupWifi() {
  Serial.printf("WiFi: connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.printf("\nWiFi OK, IP=%s\n", WiFi.localIP().toString().c_str());
}

// ================== Arduino ==================
void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(PAIR_RESET_PIN, INPUT_PULLUP);

  setupWifi();

  buildDeviceIdIfEmpty();
  buildTopics();

  prefs.begin("dry", false);
  isPaired = prefs.getBool("paired", false);

  Serial.printf("DEVICE_ID: %s\n", DEVICE_ID.c_str());
  Serial.printf("TOPICS:\n  cmd=%s\n  state=%s\n  availability=%s\n  pair=%s\n  pair_result=%s\n  pair_index=%s\n",
                TOPIC_CMD, TOPIC_STATE, TOPIC_AVAIL, TOPIC_PAIR, TOPIC_PAIR_RESULT, TOPIC_PAIR_INDEX);
  Serial.printf("Paired: %s (code=%s)\n", isPaired ? "true" : "false", DEVICE_CLAIM_CODE);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(20);
  mqtt.setCallback(mqttCallback);

  ensureMqtt();
}

void loop() {
  if (!mqtt.connected()) ensureMqtt();
  mqtt.loop();

  checkPairResetButton();

  static uint32_t last = 0;
  if (millis() - last > 30000 && mqtt.connected()) {
    last = millis();
    publishState();
  }
}
