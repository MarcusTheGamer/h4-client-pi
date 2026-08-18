import requests
import time
from grove_rgb_lcd import *
from grovepi import *
import json
import paho.mqtt.client as mqtt

th_port = 7 # temperature and humidity port 7
light_port = 0 # light port 0
sound_port = 1 # sound port 1

def get_hardware_id():
    with open('/proc/cpuinfo', 'r') as f:
        for line in f:
            if line.startswith('Serial'):
                return line.split(':')[1].strip()
    return "unknown"

HARDWARE_ID = get_hardware_id()
#print(HARDWARE_ID)
MQTT_BROKER = "192.168.1.138"
MQTT_PORT = 1883
API_URL = "http://192.168.1.138:3000/api/sensor-data"

CONFIG_TOPIC = "devices/" + HARDWARE_ID + "/config"

current_config = {}
SENSORS = {'temperature': {'min': 23.0, 'max': 26.0, 'interval': 900}, 'light': {'min': 10.0, 'max': 200.0, 'interval': 900}, 'humidity': {'min': 45.0, 'max': 55.0, 'interval': 1800}, 'sound': {'min': None, 'max': None, 'interval': 120}}
last_sent = {sensor_type: 0 for sensor_type in SENSORS}      # {sensor_type: last_unix_time_sent}
alert_state = {}      # {sensor_type: "above" | "below" | "normal"}

mqtt_connected = False
last_post_ok = True
last_readings_text = "HARDWARE ID: \n" + HARDWARE_ID
display_toggle = 0

READINGS_DURATION = 3
ID_DURATION = 8

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

setRGB(0, 0, 255)
setText("Trying to connect to database")

def post_readings(readings):
    payload = {"readings": readings}
    headers = {
        "x-hardware-id": HARDWARE_ID,
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=5)
        response.raise_for_status()
        #print("Posted:", response.json())
        return True
    except requests.exceptions.RequestException as e:
        #print("Error:", e)
        return False

def read_sensor(sensor_type):
    if sensor_type == "temperature" or sensor_type == "humidity":
        [temp, hum] = dht(th_port, 1)
        return {"temperature": temp, "humidity": hum}
    if sensor_type == "light":
        return {"light": analogRead(light_port)}
    if sensor_type == "sound":
        return {"sound": analogRead(sound_port)}
    return {}

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

while True:
    now = time.time()
    readings = []
    alert_readings = []
    values = {}
    posted_this_tick = False

    sensors_to_read = set()
    for sensor_type, settings in SENSORS.items():
        interval = settings.get("interval")
        due = interval is not None and now - last_sent[sensor_type] >= interval
        monitored = settings.get("min") is not None or settings.get("max") is not None
        if due or monitored:
            sensors_to_read.add(sensor_type)

    for sensor_type in sensors_to_read:
        if sensor_type not in values:
            values.update(read_sensor(sensor_type))

    for sensor_type, settings in SENSORS.items():
        if sensor_type not in values:
            continue

        value = values[sensor_type]
        interval = settings.get("interval")

        if settings.get("min") is not None or settings.get("max") is not None:
            if check_threshold(sensor_type, value, settings):
                alert_readings.append({"sensor_type": sensor_type, "value": value})

        # Interval schedule is untouched by alert posts above
        if interval is not None and now - last_sent[sensor_type] >= interval:
            readings.append({"sensor_type": sensor_type, "value": value})
            last_sent[sensor_type] = now

    if alert_readings:
        ok = post_readings(alert_readings)
        last_post_ok = ok
        if ok:
            posted_this_tick = True

    if readings:
        text = ""
        for r in readings:
            text += str(r["sensor_type"]).split()[0][:2] + "=" + str(r["value"]) + ","
        last_readings_text = text

        ok = post_readings(readings)
        last_post_ok = ok
        if ok:
            posted_this_tick = True

    error_state = (not mqtt_connected) or (not last_post_ok)
    alert_active = any(state != "normal" for state in alert_state.values())

    if error_state:
        if alert_active:
            if int(now) % 2 == 0:
                setRGB(255, 0, 0)
            else:
                setRGB(255, 255, 0)
        else:
            setRGB(255, 0, 0)

        display_toggle += 1
        cycle_position = display_toggle % (READINGS_DURATION + ID_DURATION)
        if cycle_position < READINGS_DURATION:
            setText(last_readings_text)
        else:
            setText("HARDWARE ID: \n" + HARDWARE_ID)
    elif posted_this_tick:
        setRGB(0, 255, 0)
        setText(last_readings_text)
    elif alert_active:
        setRGB(255, 255, 0)
        setText(last_readings_text)
    else:
        setRGB(0, 255, 0)
        setText(last_readings_text)

    time.sleep(1)