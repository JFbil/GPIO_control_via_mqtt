#!/usr/bin/env python3
import socket
import time
from gpiozero import LED, Button
import paho.mqtt.client as mqtt
import subprocess
import socket


def get_eth_ip(prefix="192.168.1."):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip.startswith(prefix):
            return ip
    except Exception:
        pass
    return "192.168.1.40"

host_ip = get_eth_ip("192.168.1.")
host_name = socket.gethostname()
print(f"[INFO] HOSTNAME : {host_name}")



# --- MQTT configuration ---
broker = "192.168.1.10"
port = 1883
mqtt_user = "jeedom"
mqtt_password = "H63HZw20725iuqnNdRP0aDSnyFDFVR"
main_topic= "general"

ALIASES_BY_RPI_IP = {
    "SwimmingPI": {"!*porte": 26, "!pompe_sec": 19, "lampe_bassin": 20, "pompes_auxi": 21, "3voies": 16, "?volet": 17, "!douche": 6, "#hostapd": 0},
    "192.168.1.49": {"!circulateur_main": 12, "!circulateur_sec": 21, "!ecs_mazout": 20,"!chaudiere": 26, "!ecs_elec": 24, "?lampe_chaufferie": 16, "#hostapd": 0},
    "serverPI3": {"#hostapd": 0, "#tvheadend": 0},
    "Kodi-Pi5": {"#hostapd": 0, "#3cxsbc": 0, "#kodi": 0},
    # ! si 0 et 1 logique inversés sur le rpi
    # * si bouton poussoir
    # ? si pin en input
    # # si service à lancer (rien à voir avec les pins) -> topic {main_topic}/{alias}/{host_name}/set start-stop-restart (alias est le service)

}
W1_SONDES_BY_IP = {
    "SwimmingPI": {"piscine/piscine": "28-012212ada652", "piscine/poele": "28-012212c385d7", "piscine/solaire": "28-012212b4ac45", "piscine/local": "28-0621b4afc5d6"},
    "192.168.1.49": {"piscine/toit": "28-0621b448c939", "piscine/exterieur": "28-0621b50acd5c", "chaufferie/ecs": "28-3c01b55693cf", "chaufferie/sol": "28-3c01b556bcc9", "chaufferie/radiateur": "28-3c01b556ce1c", "chaufferie/chaudiere": "28-3c01b5567ec1", "chaufferie/ecs_ext": "28-3c01b5568025"},
}

# --- Local RPi config ---
this_aliases = ALIASES_BY_RPI_IP.get(host_name, {}) or ALIASES_BY_RPI_IP.get(host_ip, {})

# Structures pour gérer les comportements spéciaux
PIN_NUMBERS = set(this_aliases.values())
LED_PINS = {}
REVERSE_LOGIC = {}
PUSH_BUTTON = {}
CLEANED_ALIASES = {}
INPUT_PINS = {}     # pin → Button
INPUT_ALIASES = {}  # alias → pin
SERVICES_ALIASES = {}

for raw_alias, pin in this_aliases.items():
    alias = raw_alias.lstrip("!*?#")  # Nettoyage de l’alias pour MQTT
    is_input = raw_alias.startswith("?")
    is_service = raw_alias.startswith("#")
    if is_input:
        INPUT_PINS[pin] = Button(pin, pull_up=True)  
        INPUT_ALIASES[alias] = pin
    
    elif is_service:
        SERVICES_ALIASES[alias] = host_name
        print(f"[SERVICE] {alias}")
    else:
        CLEANED_ALIASES[alias] = pin
        LED_PINS[pin] = LED(pin, initial_value=None)
        REVERSE_LOGIC[alias] = "!" in raw_alias
        PUSH_BUTTON[alias] = "*" in raw_alias

# --- INITIALISATION DES SORTIES ---
for alias, pin in CLEANED_ALIASES.items():
    if PUSH_BUTTON.get(alias, False):
        continue  # Ne rien faire si c’est un bouton poussoir
    led = LED_PINS[pin]
    reverse = REVERSE_LOGIC.get(alias, False)
    if reverse:
        led.on()   # Logique inversée → ON = OFF réel
    else:
        led.off()  # Logique normale → OFF


GPIO_STATES = {}  # alias → "0"/"1"
SERVICES_STATES = {}  # services → "inactif 0"/"actif 1"


this_sondes = W1_SONDES_BY_IP.get(host_name, {}) or W1_SONDES_BY_IP.get(host_ip, {})
SONDE_VALUES = {}  # alias → température précédente

# --- Lecture température ---
def read_w1_temp(sensor_id):
    try:
        with open(f"/sys/bus/w1/devices/{sensor_id}/w1_slave", 'r') as f:
            lines = f.readlines()
            if "YES" not in lines[0]:
                return -999
            temp_str = lines[1].split("t=")[-1]
            temp_fl = float(temp_str) / 1000
            if temp_fl < -20 or temp_fl > 80:
                return -999
            else:
                return temp_fl 
    except Exception:
        return -999
    
def control_service(service_name, action):
    """Démarre, arrête ou redémarre un service systemd."""
    if action not in ["start", "stop", "restart"]:
        raise ValueError("Action non valide. Utilise start, stop ou restart.")

    cmd = ["systemctl", action, service_name]
    try:
        subprocess.run(cmd, check=True)
        print(f"[MQTT] Service '{service_name}' {action} avec succès.")
    except subprocess.CalledProcessError as e:
        print(f"[MQTT] Erreur lors de l'exécution de systemctl: {e}")

def is_service_active(service_name):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False
        )
        return result.stdout.strip() == "active"
    except Exception as e:
        print(f"[SERVICE] Erreur en vérifiant '{service_name}': {e}")
        return False

def has_any_network_interface():
    try:
        interfaces = os.listdir("/sys/class/net/")
        for iface in interfaces:
            if iface.startswith("eth") or iface.startswith("en") or iface.startswith("wl"):
                return True
    except Exception:
        pass
    return False

def is_ethernet_connected(interface):
    try:
        with open(f"/sys/class/net/{interface}/carrier", "r") as f:
            return f.read().strip() == "1"
    except Exception:
        return False

# --- MQTT callbacks ---
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Connecté au broker")
        client.subscribe(f"{main_topic}/#")
        client.subscribe("piscine/+")
        client.subscribe("chaufferie/+")       
    else:
        print(f"[MQTT] Erreur de connexion : {rc}")

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode().strip()
    print(f"[MQTT] {topic} --> {payload}")

     # Requête spéciale : republier tous les états
    if topic == f"{main_topic}/getallstate":
        print("[MQTT] Requête de re-publication des états reçue.")
        get_infos(client, True)

    elif topic.endswith("/set"):
        alias = topic.split("/")[1]
        if alias in CLEANED_ALIASES:

            pin = CLEANED_ALIASES[alias]
            led = LED_PINS[pin]
            reverse = REVERSE_LOGIC.get(alias, False)
            push = PUSH_BUTTON.get(alias, False)


            if payload.upper() in ("ON"):
                # Allumage (logique inversée possible)
                if reverse:
                    led.off()
                else:
                    led.on()

                # Si mode poussoir, extinction automatique après 0.25s
                if push:
                    time.sleep(0.25)
                    if reverse:
                        led.on()
                    else:
                        led.off()

            elif payload.upper() in ("OFF") and not push:
                # Extinction manuelle (hors poussoir)
                if reverse:
                    led.on()
                else:
                    led.off()
        
        elif alias in SERVICES_ALIASES:
            if topic == f"{main_topic}/{alias}/{host_name}/set":
                control_service(alias,payload.lower())

        else:
            return

# --- Boucle de surveillance ---
def monitor_loop(client):
    print("[LOOP] Surveillance GPIO et sondes active")
    while True:

        get_infos(client, False)
        time.sleep(2)

def get_infos(client, is_forced):
    # Vérifier l'état des GPIOs
    for alias, pin in CLEANED_ALIASES.items():
        #alias = raw_alias.lstrip("!*")
        reverse = REVERSE_LOGIC.get(alias, False)
        led = LED_PINS[pin]
        is_on = led.is_lit
        state = "1" if (is_on != reverse) else "0"

        if is_forced or GPIO_STATES.get(alias) != state:
            GPIO_STATES[alias] = state
            client.publish(f"{main_topic}/{alias}/state", state)
            print(f"[GPIO] {alias} = {state}")
    
    # Vérifier les entrées GPIO
    for alias, pin in INPUT_ALIASES.items():
        button = INPUT_PINS[pin]
        state = "1" if button.is_pressed else "0"
        
        if is_forced or GPIO_STATES.get(alias) != state :
            GPIO_STATES[alias] = state
            client.publish(f"{main_topic}/{alias}/state", state)
            print(f"[INPUT] {alias} = {state}")

    # Vérifier les services
    for alias, pin in SERVICES_ALIASES.items():
        state = "1" if is_service_active(alias) else "0"
        if is_forced or SERVICES_STATES.get(alias) != state :
            SERVICES_STATES[alias] = state
            client.publish(f"{main_topic}/{alias}/{host_name}/state", state)
            print(f"[SERVICE] {alias} = {state}")

    # Vérifier les sondes
    for alias, sensor_id in this_sondes.items():
        current_temp = round(read_w1_temp(sensor_id),2)
        previous_temp = SONDE_VALUES.get(alias)
        if is_forced or (previous_temp is None or abs(previous_temp - current_temp) > 0.1):
            SONDE_VALUES[alias] = current_temp
            #topic = f{main_topic}/{alias}"
            if current_temp < -20 or current_temp > 80:
                client.publish(alias, "-999")
            else:
                client.publish(alias, str(current_temp))
            print(f"[SONDE] {alias} = {current_temp}°C")

# --- Main ---
if __name__ == "__main__":
    client = mqtt.Client()
    client.username_pw_set(mqtt_user, mqtt_password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker, port, 60)

    client.loop_start()  # boucle MQTT dans un thread

    try:
        monitor_loop(client)
    except KeyboardInterrupt:
        print("\n[EXIT] Arrêt propre.")
        client.loop_stop()
