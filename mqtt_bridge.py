# mqtt_bridge.py
import json
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt

# ===== Глобальные карты состояния (в памяти) =====
ONLINE = {}       # device_id -> bool
CODE_INDEX = {}   # code -> device_id

# ===== Настройки MQTT =====
MQTT_HOST = "5.129.222.167"
MQTT_PORT = 1883
MQTT_USER = "esphkaf"
MQTT_PASS = "S159357"
KEEPALIVE = 60

def topic_state(device_id: str) -> str:
    return f"devices/{device_id}/state"

def topic_avail(device_id: str) -> str:
    return f"devices/{device_id}/availability"

def topic_cmd(device_id: str) -> str:
    return f"devices/{device_id}/cmd"

def topic_pair(device_id: str) -> str:
    return f"devices/{device_id}/pair"

def topic_pair_result(device_id: str) -> str:
    return f"devices/{device_id}/pair_result"

# ====== Колбэк из приложения для записи состояния в БД ======
_state_handler = None  # callable(device_id:str, kind:str, payload:dict|str|None)

def register_state_handler(func):
    """Приложение регистрирует обработчик, чтобы получать апдейты и писать их в БД."""
    global _state_handler
    _state_handler = func

class MqttBridge:
    def __init__(self):
        self.client = mqtt.Client(client_id=f"flask-bridge-{int(time.time())}")
        if MQTT_USER or MQTT_PASS:
            self.client.username_pw_set(MQTT_USER or None, MQTT_PASS or None)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self._thread = None
        self._stop = threading.Event()

        # ожидатели pair_result
        self.WAITERS = {}
        self.WAITERS_LOCK = threading.Lock()

    def _get_waiter(self, device_id: str):
        with self.WAITERS_LOCK:
            w = self.WAITERS.get(device_id)
            if not w:
                w = {"event": threading.Event(), "payload": None}
                self.WAITERS[device_id] = w
            else:
                w["event"].clear()
                w["payload"] = None
            return w

    def _resolve_waiter(self, device_id: str, payload: dict):
        with self.WAITERS_LOCK:
            w = self.WAITERS.get(device_id)
            if w:
                w["payload"] = payload
                w["event"].set()

    # ---- Паблик API ----
    def start(self):
        def runner():
            while not self._stop.is_set():
                try:
                    self.client.connect(MQTT_HOST, MQTT_PORT, KEEPALIVE)
                    self.client.loop_forever()
                except Exception as e:
                    print(f"[MQTT] connect error: {e}, retry in 3s")
                    time.sleep(3)

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        print(f"[MQTT] bridge started -> {MQTT_HOST}:{MQTT_PORT}")

    def stop(self):
        self._stop.set()
        try:
            self.client.disconnect()
        except Exception:
            pass

    def publish_cmd(self, device_id: str, payload: dict):
        t = topic_cmd(device_id)
        s = json.dumps(payload, ensure_ascii=False)
        info = self.client.publish(t, s, qos=0, retain=False)
        info.wait_for_publish(timeout=2.0)
        print(f"[MQTT→] {t} {s}")

    def publish_pair_and_wait(self, device_id: str, code: str, timeout_sec: int = 10) -> dict:
        w = self._get_waiter(device_id)
        t = topic_pair(device_id)
        s = json.dumps({"code": code}, ensure_ascii=False)
        try:
            info = self.client.publish(t, s, qos=0, retain=False)
            info.wait_for_publish(timeout=2.0)
            print(f"[MQTT→] {t} {s}")
        except Exception as e:
            with self.WAITERS_LOCK:
                self.WAITERS.pop(device_id, None)
            return {"ok": False, "error": f"mqtt_publish_failed:{e}"}

        done = w["event"].wait(timeout=timeout_sec)
        with self.WAITERS_LOCK:
            self.WAITERS.pop(device_id, None)

        if not done:
            return {"ok": False, "error": "timeout_no_pair_result"}
        payload = w["payload"] or {}
        ok = bool(payload.get("ok"))
        err = payload.get("error")
        return {"ok": ok, "error": err}

    # ---- Callbacks ----
    def _on_connect(self, client, userdata, flags, rc):
        print(f"[MQTT] connected rc={rc}")
        client.subscribe("devices/+/state", qos=0)
        client.subscribe("devices/+/availability", qos=0)
        client.subscribe("devices/+/pair_result", qos=0)
        # индексация по одному коду (ретейн сообщением):
        client.subscribe("pair/index/+", qos=0)

    def _on_disconnect(self, client, userdata, rc):
        print(f"[MQTT] disconnected rc={rc}")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload_raw = msg.payload.decode("utf-8", errors="ignore")
        print(f"[MQTT←] {topic} {payload_raw}")

        # Индекс: pair/index/<CODE> -> <DEVICE_ID> (retain)
        if topic.startswith("pair/index/"):
            code = topic[len("pair/index/"):]
            device_id = (payload_raw or "").strip()
            if device_id:
                CODE_INDEX[code] = device_id
            else:
                CODE_INDEX.pop(code, None)
            return

        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "devices":
            return
        device_id = parts[1]
        kind = parts[2]

        # Ответ на pairing
        if kind == "pair_result":
            try:
                data = json.loads(payload_raw or "{}")
            except json.JSONDecodeError:
                data = {}
            self._resolve_waiter(device_id, data)
            return

        # availability/state — обновление карт и уведомление приложения
        if kind == "availability":
            ONLINE[device_id] = (payload_raw.strip().lower() == "online")
            if _state_handler:
                _state_handler(device_id, "availability", ONLINE[device_id])
            return

        if kind == "state":
            ONLINE[device_id] = True
            try:
                data = json.loads(payload_raw or "{}")
            except json.JSONDecodeError:
                data = {}
            if _state_handler:
                _state_handler(device_id, "state", data)
            return


bridge = MqttBridge()
