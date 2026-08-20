import requests
import time
import json
import threading

import RPi.GPIO as GPIO
from grove_rgb_lcd import *
from grovepi import *
import paho.mqtt.client as mqtt

th_port = 7
light_port = 0
sound_port = 1

# Button is on a NATIVE Pi GPIO, not a GrovePi port.
# SIG -> BCM 5 (physical pin 29), VCC -> 3.3V (pin 1), GND -> pin 6.
BUTTON_BCM = 5

# How often we actually touch the sensors. The DHT11 only updates about
# every 2s, so polling faster just burns I2C bandwidth.
SENSOR_POLL = 2.0

# How often the loop wakes up. Display writes are change-gated, so a fast
# tick costs almost nothing and makes the button feel instant.
TICK = 0.1


def get_hardware_id():
    with open('/proc/cpuinfo', 'r') as f:
        for line in f:
            if line.startswith('Serial'):
                return line.split(':')[1].strip()
    return "unknown"


HARDWARE_ID = get_hardware_id()
MQTT_BROKER = "192.168.1.138"
MQTT_PORT = 1883
API_URL = "http://192.168.1.138:3000/api/sensor-data"

CONFIG_TOPIC = "devices/" + HARDWARE_ID + "/config"

current_config = {}
SENSORS = {'temperature': {'min': 23.0, 'max': 26.0, 'interval': 900}, 'light': {'min': 10.0, 'max': 200.0, 'interval': 900}, 'humidity': {'min': 45.0, 'max': 55.0, 'interval': 1800}, 'sound': {'min': None, 'max': None, 'interval': 120}}
last_sent = {sensor_type: 0 for sensor_type in SENSORS}
alert_state = {}

mqtt_connected = False
last_post_ok = True

READINGS_DURATION = 3
ID_DURATION = 8


# ---------------------------------------------------------------------------
# Unit toggle
#
# The ONLY shared state between the button thread and the main loop is this
# one boolean. It never touches I2C, never touches `values`, never touches
# `alert_state`. Everything internal stays in Celsius; the flag is read once
# per render and applied at format time.
# ---------------------------------------------------------------------------

_unit_f = False
_unit_lock = threading.Lock()


def _on_button(channel):
    global _unit_f
    with _unit_lock:
        _unit_f = not _unit_f


def unit_is_f():
    with _unit_lock:
        return _unit_f


GPIO.setmode(GPIO.BCM)
GPIO.setup(BUTTON_BCM, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
GPIO.add_event_detect(BUTTON_BCM, GPIO.RISING, callback=_on_button, bouncetime=250)


# ---------------------------------------------------------------------------
# LCD write gating
#
# setText() sends a clear command plus 32 individual register writes. Doing
# that every tick is what was saturating the bus. Only write when the content
# actually changed.
# ---------------------------------------------------------------------------

_last_text = None
_last_rgb = None


def lcd_show(line1, line2, rgb):
    global _last_text, _last_rgb

    if rgb != _last_rgb:
        setRGB(rgb[0], rgb[1], rgb[2])
        _last_rgb = rgb

    # Exactly 32 chars, no newline: avoids the off-by-one in the library's
    # row-wrap handling and avoids needing a clear.
    text = line1[:16].ljust(16) + line2[:16].ljust(16)
    if text != _last_text:
        setText_norefresh(text)
        _last_text = text


def set_config(config):
    global SENSORS, last_sent
    SENSORS = config["sensors"]
    for sensor_type in SENSORS:
        if sensor_type not in last_sent:
            last_sent[sensor_type] = 0


def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        client.subscribe([(CONFIG_TOPIC, 0)])
    else:
        mqtt_connected = False


def on_disconnect(client, userdata, rc):
    global mqtt_connected
    mqtt_connected = False


def on_message(client, userdata, msg):
    global current_config
    if msg.topic == CONFIG_TOPIC:
        if msg.payload.decode() == "{}":
            return
        current_config = json.loads(msg.payload.decode())
        set_config(current_config)


mqtt_client = mqtt.Client(client_id=HARDWARE_ID)
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
mqtt_client.loop_start()

time.sleep(5)

lcd_show("Connecting to", "database...", (0, 0, 255))


def post_readings(readings):
    # Always Celsius on the wire. The button is a display concern only.
    payload = {"readings": readings}
    headers = {
        "x-hardware-id": HARDWARE_ID,
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=5)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        return False


def read_sensor(sensor_type):
    try:
        if sensor_type == "temperature" or sensor_type == "humidity":
            [temp, hum] = dht(th_port, 1)
            return {"temperature": temp, "humidity": hum}
        if sensor_type == "light":
            return {"light": analogRead(light_port)}
        if sensor_type == "sound":
            return {"sound": analogRead(sound_port)}
    except Exception as e:
        print("Sensor read error ({}): {}".format(sensor_type, e))
        return {}
    return {}


def fmt_value(sensor_type, value):
    if sensor_type == "temperature":
        if unit_is_f():
            return "{:.0f}F".format(value * 9.0 / 5.0 + 32.0)
        return "{:.0f}C".format(value)
    return str(value)


def build_values_text(values):
    out = ""
    for sensor_type in SENSORS:
        if sensor_type in values:
            out += sensor_type[:2] + "=" + fmt_value(sensor_type, values[sensor_type]) + ","
    return out


def check_threshold(sensor_type, value, settings):
    global alert_state

    min_v = settings.get("min")
    max_v = settings.get("max")

    if max_v is not None and value > max_v:
        new_state = "above"
    elif min_v is not None and value < min_v:
        new_state = "below"
    else:
        new_state = "normal"

    old_state = alert_state.get(sensor_type, "normal")
    alert_state[sensor_type] = new_state

    return new_state != old_state


values = {}
last_poll = 0.0

try:
    while True:
        now = time.time()

        # --- sensor work, throttled -----------------------------------------
        if now - last_poll >= SENSOR_POLL:
            last_poll = now

            readings = []
            alert_readings = []
            fresh = {}

            for sensor_type in SENSORS:
                if sensor_type not in fresh:
                    fresh.update(read_sensor(sensor_type))
            values = fresh

            for sensor_type, settings in SENSORS.items():
                if sensor_type not in values:
                    continue

                value = values[sensor_type]
                interval = settings.get("interval")

                if settings.get("min") is not None or settings.get("max") is not None:
                    if check_threshold(sensor_type, value, settings):
                        alert_readings.append({"sensor_type": sensor_type, "value": value})

                if interval is not None and now - last_sent[sensor_type] >= interval:
                    readings.append({"sensor_type": sensor_type, "value": value})
                    last_sent[sensor_type] = now

            if alert_readings:
                last_post_ok = post_readings(alert_readings)

            if readings:
                last_post_ok = post_readings(readings)

        # --- display, every tick, but write-gated ---------------------------
        values_text = build_values_text(values)

        error_state = (not mqtt_connected) or (not last_post_ok)
        alert_active = any(state != "normal" for state in alert_state.values())

        if error_state:
            if alert_active and int(now) % 2 == 0:
                rgb = (255, 255, 0)
            else:
                rgb = (255, 0, 0)

            # Time-based cycle, so it no longer depends on the tick rate.
            cycle_position = int(now) % (READINGS_DURATION + ID_DURATION)
            if cycle_position < READINGS_DURATION:
                lcd_show(values_text, "", rgb)
            else:
                lcd_show("HARDWARE ID:", HARDWARE_ID, rgb)
        elif alert_active:
            lcd_show(values_text, "", (255, 255, 0))
        else:
            lcd_show(values_text, "", (0, 255, 0))

        time.sleep(TICK)

except KeyboardInterrupt:
    pass
finally:
    GPIO.remove_event_detect(BUTTON_BCM)
    GPIO.cleanup()
    mqtt_client.loop_stop()