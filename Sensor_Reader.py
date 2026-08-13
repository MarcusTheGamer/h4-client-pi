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
    with open ('/proc/cpuinfo', 'r') as f:
        for line in f:
            if line.startswith('Serial'):
                return line.split(':')[1].strip()
    return "unknown"


HARDWARE_ID = get_hardware_id()
print(HARDWARE_ID)
MQTT_BROKER = "192.168.1.138"
MQTT_PORT = 1883
API_URL = "http://192.168.1.138:3000/api/sensor-data"

CONFIG_TOPIC = f"devices/{HARDWARE_ID}/config"
ACK_TOPIC = f"devices/{HARDWARE_ID}/ack"

CONNECTED = False
MAX_TIMEOUT_ATTEMPTS = 10
TIMEOUTS = 0
current_config = {}

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT broker as", HARDWARE_ID)
        client.subscribe([(CONFIG_TOPIC, 0), (ACK_TOPIC, 0)])
        print("Subscribed to config and ack topics")
    else:
        print("MQTT connect failed, rc =", rc)

def on_disconnect(client, userdata, rc):
    print("MQTT connection closed")

def on_message(client, userdata, msg):
    global current_config
    if msg.topic == CONFIG_TOPIC:
        try:
            current_config = json.loads(msg.payload.decode())
            print("Config received:", current_config)
        except json.JSONDecodeError as e:
            print("Bad config payload:", e)
    elif msg.topic == ACK_TOPIC:
        print("Ack:", msg.payload.decode())

mqtt_client = mqtt.Client(client_id=HARDWARE_ID)
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
mqtt_client.loop_start()

setRGB(0,0,255)
setText("Trying to connect to database")

def post_readings(temp, hum, light, sound):
    payload = {
        "readings": [
            {"sensor_type": "temperature", "value": temp},
            {"sensor_type": "humidity", "value": hum},
            {"sensor_type": "light", "value": light}
        ]
    }
    headers = {
        "x-hardware-id": HARDWARE_ID,
        "Content-Type": "application/json"
        }
    
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=5)
        response.raise_for_status()
        print("Posted:", response.json())
        return True
    except requests.exceptions.RequestException as e:
        print("Error:", e)
        return False

while True:
    try:
        [ temp, hum ] = dht(th_port,1)       #Get the temperature and Humidity from the DHT sensor
        light = analogRead(light_port)
        sound = analogRead(sound_port)
        
        data = "t=" + str(temp) + ",h=" + str(hum) + "%\nl=" + str(light) + ",s=" + str(sound)
        if post_readings(temp, hum, light, sound) == True:
            if CONNECTED == False:
                CONNECTED = True
            TIMEOUTS = 0
            setRGB(0,255,0)
            setText(data)
        else:
            TIMEOUTS = TIMEOUTS + 1
            CONNECTED = False
            if TIMEOUTS >= MAX_TIMEOUT_ATTEMPTS:
                setRGB(255,0,0)
                setText(data)
                time.sleep(2)
                setText("HARDWARE ID: \n" + get_hardware_id())
            
        #print("temp =", temp, "C humidity =", hum, "%", "Light =", light, "Sound =", sound)

        time.sleep(2)

    except (IOError,TypeError) as e:
        print("Error", e)
